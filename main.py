import asyncio
import os
import logging
import threading
import queue
import time
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from config.settings import runtime_settings
from database.models import init_db
from middlewares.auth import HardenedAuthMiddleware
from middlewares.totp_auth import require_2fa

from handlers.docker_cmds import docker_router
from handlers.shell import shell_router
from handlers.project import project_router
from handlers.file_manager import file_router
from handlers.auth_cmds import auth_router
from handlers.help_cmds import help_router
from handlers.tunnel_cmds import tunnel_router
from handlers.duckdns_cmds import duckdns_router
from handlers.env_cmds import env_router
from services.duckdns_service import duckdns_service

from utils.watchdog import ResiliencyWatchdogEngine

# --- Setup System Logging Profiles ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()]
)

# --- Host Command Configuration ---
HOST_COMMAND_UPDATE_INTERVAL = 30  # Seconds between status updates for /host command

# --- Define Hidden Host Execution Core Logic ---
# Store active command info: user_id -> { 'ssh_client': paramiko.SSHClient, 'stdout', 'stderr', 'stop_event': threading.Event }
_active_commands: dict[int, dict] = {}

class HostCommandStates(StatesGroup):
    running = State()

def execute_on_host_machine(command: str, cwd: str | None = None) -> str:
    """Run a command on the WSL host via SSH loopback, optionally with a working directory."""
    from utils.ssh_helper import ssh_exec
    full_cmd = command
    if cwd:
        full_cmd = f'cd "{cwd}" && {command}'
    code, output = ssh_exec(full_cmd, timeout=120)
    return output if output.strip() else "[Command executed with empty output]"

def execute_on_host_machine_streaming(command: str, cwd: str | None = None, stop_event: threading.Event = None, output_line_queue: queue.Queue = None) -> tuple[int, str, bool]:
    """Run a command on the host with streaming output, returns (exit_code, output, was_stopped)."""
    import paramiko
    from utils.ssh_helper import get_ssh_creds, get_ssh_host

    user, password = get_ssh_creds()
    if not user or not password:
        return -1, "HOST_SSH_USER or HOST_SSH_PASSWORD not configured in .env", False

    host, port = get_ssh_host()
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    full_cmd = command
    if cwd:
        full_cmd = f'cd "{cwd}" && {command}'
    # Wrap command to get PID and allow killing it
    wrapped_cmd = f"sh -c 'echo $$; exec {full_cmd}'"
    output_lines = []
    was_stopped = False
    exit_code = -1
    process_pid = None

    try:
        ssh.connect(host, port=port, username=user, password=password, timeout=15)
        stdin, stdout, stderr = ssh.exec_command(wrapped_cmd)

        # Read output in a loop
        first_line = True
        while True:
            # Check stop event FIRST every time and kill immediately
            if stop_event.is_set():
                was_stopped = True
                # Try multiple ways to kill the process RIGHT AWAY
                if process_pid:
                    try:
                        # Kill the PID and its children
                        kill_cmd = f'pkill -TERM -P {process_pid} 2>/dev/null || kill -TERM {process_pid} 2>/dev/null || kill -9 {process_pid} 2>/dev/null; true'
                        ssh.exec_command(kill_cmd)
                    except:
                        pass
                # Also try sending Ctrl+C
                try:
                    stdin.write(chr(3))  # Send Ctrl+C
                    stdin.flush()
                except:
                    pass
                break

            # Read available data
            if stdout.channel.recv_ready():
                chunk = stdout.read(4096)
                if chunk:
                    text = chunk.decode(errors="ignore")
                    lines = text.splitlines(True)  # Keep newlines
                    for line in lines:
                        if first_line and line.strip().isdigit():
                            process_pid = line.strip()
                            first_line = False
                            continue
                        first_line = False
                        output_lines.append(line)
                        if output_line_queue:
                            output_line_queue.put(line)
            if stderr.channel.recv_ready():
                chunk = stderr.read(4096)
                if chunk:
                    text = chunk.decode(errors="ignore")
                    lines = text.splitlines(True)
                    output_lines.extend(lines)
                    if output_line_queue:
                        for line in lines:
                            output_line_queue.put(line)
            # Check if command is done
            if stdout.channel.exit_status_ready():
                exit_code = stdout.channel.recv_exit_status()
                # Read remaining data
                remaining_stdout = stdout.read()
                if remaining_stdout:
                    remaining_lines = remaining_stdout.decode(errors="ignore").splitlines(True)
                    output_lines.extend(remaining_lines)
                    if output_line_queue:
                        for line in remaining_lines:
                            output_line_queue.put(line)
                remaining_stderr = stderr.read()
                if remaining_stderr:
                    remaining_lines = remaining_stderr.decode(errors="ignore").splitlines(True)
                    output_lines.extend(remaining_lines)
                    if output_line_queue:
                        for line in remaining_lines:
                            output_line_queue.put(line)
                break
            time.sleep(0.01)  # Shorter sleep for faster stop event check
    except Exception as e:
        error_line = f"\nError: {e}"
        output_lines.append(error_line)
        if output_line_queue:
            output_line_queue.put(error_line)
    finally:
        try:
            ssh.close()
        except:
            pass

    return exit_code, "".join(output_lines), was_stopped


async def main():
    logging.info("Validating configuration matrices and initializing SQLite metadata layers...")
    init_db()

    # Load and apply mutable settings from MongoDB or settings.json
    from utils.env_manager import load_mutable_settings, apply_to_runtime
    mutable = load_mutable_settings()
    if mutable:
        apply_to_runtime(mutable)
        logging.info(f"Applied {len(mutable)} mutable settings from persistent store.")

    # Kill any orphaned cloudflared / auth proxy processes from previous bot runs
    logging.info("Cleaning up orphaned tunnel processes from previous session...")
    from services.tunnel_service import _ssh_exec as _tun_ssh
    await asyncio.to_thread(
        _tun_ssh,
        "pkill -f 'cloudflared tunnel' 2>/dev/null; "
        "pkill -f 'tele_auth_proxy' 2>/dev/null; "
        "echo cleaned",
        30
    )

    # Start DuckDNS auto-updater if configured
    if runtime_settings.DUCKDNS_TOKEN and runtime_settings.DUCKDNS_DOMAIN:
        asyncio.create_task(
            duckdns_service.start_auto_updater(runtime_settings.DUCKDNS_UPDATE_INTERVAL)
        )
        logging.info(f"DuckDNS auto-updater scheduled every {runtime_settings.DUCKDNS_UPDATE_INTERVAL}s")

    os.makedirs("/app/data", exist_ok=True)
    health_marker = "/app/data/healthy"
    with open(health_marker, "w") as f:
        f.write("OK")

    bot = Bot(token=runtime_settings.TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()

    # Apply global authentication safeguards
    dp.message.outer_middleware(HardenedAuthMiddleware())

    # Create a dedicated inline router for hidden administrative tasks
    hidden_host_router = Router()

    @hidden_host_router.message(Command("host"))
    @require_2fa
    async def handle_hidden_host_execution(message: Message, state: FSMContext):
        """Hidden /host command — runs commands on the WSL host via SSH with real-time updates."""
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            await message.answer("⚠️ Missing command. Usage: <code>/host &lt;command&gt;</code>", parse_mode="HTML")
            return

        target_cmd = args[1]
        import html as _html
        safe_cmd = _html.escape(target_cmd)

        # Create stop button
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏹️ Stop Command", callback_data="stop_host_cmd")]
        ])
        status_message = await message.answer(
            f"⏳ <b>Running</b> <code>{safe_cmd}</code>...\n\nOutput will update every {HOST_COMMAND_UPDATE_INTERVAL} seconds.\n<i>(Click Stop to end the command)</i>",
            parse_mode="HTML",
            reply_markup=keyboard
        )

        # Setup stop event and shared state
        user_id = message.from_user.id
        stop_event = threading.Event()
        output_queue = queue.Queue()
        output_line_queue = queue.Queue()
        done_event = threading.Event()
        recent_output_lines = []

        def _run_command():
            try:
                code, output, was_stopped = execute_on_host_machine_streaming(target_cmd, stop_event=stop_event, output_line_queue=output_line_queue)
                output_queue.put(("done", code, output, was_stopped))
            except Exception as e:
                output_queue.put(("error", str(e)))
            finally:
                done_event.set()

        thread = threading.Thread(target=_run_command, daemon=True)
        thread.start()

        # Store stop event in the in-memory dict (not FSM — threading.Event can't be serialized)
        _active_commands[user_id] = {"stop_event": stop_event}
        await state.set_state(HostCommandStates.running)
        await state.update_data(
            target_cmd=target_cmd,
            start_time=time.time()
        )

        # Monitor the command
        last_update_time = time.time()

        try:
            while not done_event.is_set():
                await asyncio.sleep(0.5)

                # Collect new output lines
                while True:
                    try:
                        line = output_line_queue.get_nowait()
                        recent_output_lines.append(line)
                        # Keep only last 20 lines
                        if len(recent_output_lines) > 20:
                            recent_output_lines.pop(0)
                    except queue.Empty:
                        break

                # Check if we need to update
                current_time = time.time()
                if current_time - last_update_time >= HOST_COMMAND_UPDATE_INTERVAL:
                    state_data = await state.get_data()
                    elapsed = int(current_time - state_data.get("start_time", time.time()))
                    elapsed_min = elapsed // 60
                    elapsed_sec = elapsed % 60
                    header = f"⏳ <b>Running</b> <code>{safe_cmd}</code>...\n<i>Elapsed: {elapsed_min}m {elapsed_sec}s</i>\n\n"
                    
                    # Show last 10 lines
                    if recent_output_lines:
                        last_10 = recent_output_lines[-10:]
                        header += "<b>Last 10 lines:</b>\n<pre>"
                        header += _html.escape("".join(last_10))
                        header += "</pre>"
                    else:
                        header += "<i>No output yet...</i>"
                    
                    try:
                        await status_message.edit_text(
                            header,
                            parse_mode="HTML",
                            reply_markup=keyboard
                        )
                    except:
                        pass  # Ignore edit errors
                    last_update_time = current_time

                # Check queue for results
                try:
                    result = output_queue.get_nowait()
                    if result[0] in ("done", "error"):
                        final_result = result
                        break
                except queue.Empty:
                    pass

            # Command is done — use cached result or wait briefly
            if 'final_result' not in dir():
                try:
                    final_result = output_queue.get(timeout=5.0)
                except Exception:
                    final_result = ("done", -1, "(no output captured)", False)

            if final_result[0] == "done":
                code, result_payload, was_stopped = final_result[1], final_result[2], final_result[3]
                status_text = "✅ <b>Finished</b>" if not was_stopped else "⏹️ <b>Stopped</b>"
            else:
                code = -1
                result_payload = str(final_result[1]) if len(final_result) > 1 else "(error)"
                status_text = "❌ <b>Error</b>"
        finally:
            stop_event.set()
            await state.clear()
            if user_id in _active_commands:
                del _active_commands[user_id]

        # Show final output
        await status_message.delete()
        safe_output = _html.escape(result_payload)
        header = f"{status_text}\n<b>$</b> <code>{safe_cmd}</code>\n<b>Exit code:</b> <code>{code}</code>\n\n"
        full_text = header + f"<pre>{safe_output}</pre>"

        if len(full_text) <= 4096:
            await message.answer(full_text, parse_mode="HTML")
        else:
            # Show tail inline + attach full output as file
            from aiogram.types import BufferedInputFile
            tail = safe_output[-3500:]
            if '\n' in tail:
                tail = tail[tail.index('\n') + 1:]

            truncated = (
                header
                + "<i>⚠️ Output too long — showing last 3500 chars. Full output attached.</i>\n\n"
                + f"<pre>{tail}</pre>"
            )
            await message.answer(truncated, parse_mode="HTML")

            raw_bytes = result_payload.encode('utf-8', errors='replace')
            if len(raw_bytes) > 10 * 1024 * 1024:
                raw_bytes = raw_bytes[:10 * 1024 * 1024] + b"\n[CAPPED AT 10MB]"
            doc = BufferedInputFile(raw_bytes, filename="host_output.txt")
            await message.answer_document(doc, caption=f"📄 Full output of: {target_cmd[:80]}")

    @hidden_host_router.callback_query(F.data == "stop_host_cmd")
    async def handle_stop_host_cmd(callback: CallbackQuery):
        """Handle stop button press — looks up the stop event by user_id."""
        user_id = callback.from_user.id
        cmd_info = _active_commands.get(user_id)
        if cmd_info and "stop_event" in cmd_info:
            cmd_info["stop_event"].set()
            await callback.answer("⏹️ Stop signal sent — command will end shortly.", show_alert=False)
            # Update button to show it was clicked
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
        else:
            await callback.answer("No active command found for your user.", show_alert=True)

    @hidden_host_router.message(Command("host_cd"))
    @require_2fa
    async def handle_host_cd(message: Message):
        """Run a command on the WSL host in a specific directory (transient, no persistence). Usage: /host_cd <path> <command>"""
        args = message.text.split(maxsplit=2)
        
        if len(args) < 3:
            await message.reply(
                "⚠️ Missing arguments. Usage:\n"
                "<code>/host_cd &lt;path&gt; &lt;command&gt;</code> — run command in specified directory",
                parse_mode="HTML"
            )
            return

        target_path = args[1]
        target_cmd = args[2]
        
        # First, resolve/validate the path on the host
        status_update = await message.reply("⏳ Validating path and running...", parse_mode="HTML")
        
        # Resolve path
        resolve_cmd = f'cd "{target_path}" 2>&1 && pwd'
        resolve_output = await asyncio.to_thread(execute_on_host_machine, resolve_cmd)
        
        # Check if cd failed
        if "cd:" in resolve_output or resolve_output.startswith("bash:"):
            await status_update.edit_text(
                f"❌ <b>Failed to change directory:</b>\n<code>{resolve_output}</code>",
                parse_mode="HTML"
            )
            return
        
        new_cwd = resolve_output.strip()
        
        # Run the command in the resolved directory
        result_payload = await asyncio.to_thread(execute_on_host_machine, target_cmd, new_cwd)
        
        await status_update.delete()
        
        import html as _html
        safe_cmd = _html.escape(target_cmd)
        safe_output = _html.escape(result_payload)
        
        header = f"<b>$</b> <code>{safe_cmd}</code>\n<b>📂</b> <code>{_html.escape(new_cwd)}</code>\n\n"
        full_text = header + f"<pre>{safe_output}</pre>"
        
        if len(full_text) <= 4096:
            await message.reply(full_text, parse_mode="HTML")
        else:
            from aiogram.types import BufferedInputFile
            tail = safe_output[-3500:]
            if '\n' in tail:
                tail = tail[tail.index('\n') + 1:]
            
            truncated = (
                header
                + "<i>⚠️ Output too long — showing last 3500 chars. Full output attached.</i>\n\n"
                + f"<pre>{tail}</pre>"
            )
            await message.reply(truncated, parse_mode="HTML")
            
            raw_bytes = result_payload.encode('utf-8', errors='replace')
            if len(raw_bytes) > 10 * 1024 * 1024:
                raw_bytes = raw_bytes[:10 * 1024 * 1024] + b"\n[CAPPED AT 10MB]"
            doc = BufferedInputFile(raw_bytes, filename="host_output.txt")
            await message.answer_document(doc, caption=f"📄 Full output of: {target_cmd[:80]}")

    # --- Attach Routers to the Dispatcher Engine ---
    # Register the hidden router first so it has immediate structural evaluation priority
    dp.include_router(hidden_host_router)
    
    # Standard imported functional routers
    dp.include_router(auth_router)
    dp.include_router(docker_router)
    dp.include_router(shell_router)
    dp.include_router(project_router)
    dp.include_router(file_router)
    dp.include_router(tunnel_router)
    dp.include_router(duckdns_router)
    dp.include_router(env_router)
    dp.include_router(help_router)

    sentinel = ResiliencyWatchdogEngine(bot, runtime_settings.ALLOWED_USER_ID)
    asyncio.create_task(sentinel.initialize_sentinel_loop())

    logging.info("Starting safe long-polling system link sequences with Telegram APIs...")
    try:
        await bot.send_message(
            runtime_settings.ALLOWED_USER_ID, 
            "🚀 <b>System Bootstrapped Successfully.</b> System online and monitoring containers.", 
            parse_mode="HTML"
        )
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        if os.path.exists(health_marker):
            os.remove(health_marker)
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("System gracefully exiting operational execution cycles.")