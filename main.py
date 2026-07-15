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

from handlers.pydeploy_cmds import pydeploy_router
from database.pydeploy_models import init_pydeploy_db
from services import pydeploy_service

from handlers.runpy_cmds import runpy_router

from utils.watchdog import ResiliencyWatchdogEngine
from utils.ssh_helper import force_kill_remote as _force_kill_remote, exec_streaming as execute_on_host_machine_streaming

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

def _bootstrap_passwordless_sudo() -> tuple[str, bool]:
    """
    One-time self-provisioning: if HOST_SSH_USER doesn't already have
    passwordless sudo, grant it using the same password the bot already
    authenticates over SSH with.

    This works non-interactively because `sudo -S` reads the password from
    stdin instead of needing a real TTY prompt — we feed it the password via
    ssh_exec's `input_data`. It relies on that WSL user already being a sudo
    group member (true by default for the first user on Ubuntu/WSL); this
    only removes the password prompt, it does not grant sudo membership
    itself. Safe to call on every startup — it's a fast no-op once the
    drop-in file already exists.

    Returns (message, changed) where `changed` is True only if this run
    actually created the drop-in (used to decide whether to notify the user).
    """
    from utils.ssh_helper import ssh_exec, get_ssh_creds

    user, password = get_ssh_creds()
    if not user or not password:
        return "skipped — HOST_SSH_USER/HOST_SSH_PASSWORD not configured", False

    # Already passwordless? Nothing to do.
    code, _ = ssh_exec("sudo -n true", timeout=15)
    if code == 0:
        return "passwordless sudo already configured", False

    dropin = "/etc/sudoers.d/99-teledocker"
    sudoers_line = f"{user} ALL=(ALL) NOPASSWD:ALL"
    inner = (
        f'tmp=$(mktemp); '
        f'printf "%s\\n" "{sudoers_line}" > "$tmp"; '
        f'visudo -cf "$tmp" && install -m 440 -o root -g root "$tmp" {dropin}; '
        f'rc=$?; rm -f "$tmp"; exit $rc'
    )
    provision_cmd = f"sudo -S bash -c '{inner}'"

    code, output = ssh_exec(provision_cmd, timeout=20, input_data=password + "\n")
    if code != 0:
        return f"could not provision passwordless sudo: {output.strip()[:300] or 'unknown error'}", False

    # Confirm it actually took effect before declaring success.
    verify_code, _ = ssh_exec("sudo -n true", timeout=15)
    if verify_code == 0:
        return f"provisioned passwordless sudo for '{user}' at {dropin}", True
    return "ran provisioning script but verification still failed", False


async def main():
    logging.info("Validating configuration matrices and initializing SQLite metadata layers...")
    init_db()
    init_pydeploy_db()

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

    # Self-provision passwordless sudo on the host if it isn't already set up,
    # so /host <sudo ...> and other sudo-dependent features (tunnel install,
    # pydeploy) work without a manual `visudo` step on every new machine.
    logging.info("Checking host sudo configuration...")
    sudo_bootstrap_msg, sudo_bootstrap_changed = await asyncio.to_thread(_bootstrap_passwordless_sudo)
    logging.info(f"Sudo bootstrap: {sudo_bootstrap_msg}")

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
        stopping_notice_shown = False

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

                # Once Stop has been requested, don't flash a stale "Running"
                # update back onto the message — show a one-time "Stopping..."
                # notice instead and just wait for done_event.
                if stop_event.is_set():
                    if not stopping_notice_shown:
                        try:
                            await status_message.edit_text(
                                f"⏹️ <b>Stopping</b> <code>{safe_cmd}</code>...\n"
                                f"<i>Forcing the remote process to terminate — this may take a couple of seconds.</i>",
                                parse_mode="HTML"
                            )
                        except:
                            pass
                        stopping_notice_shown = True
                else:
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
                was_stopped = stop_event.is_set()
                status_text = "❌ <b>Error</b>" if not was_stopped else "⏹️ <b>Stopped</b>"
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

        from aiogram.types import BufferedInputFile

        def _build_doc() -> BufferedInputFile:
            raw_bytes = result_payload.encode('utf-8', errors='replace')
            if len(raw_bytes) > 10 * 1024 * 1024:
                raw_bytes = raw_bytes[:10 * 1024 * 1024] + b"\n[CAPPED AT 10MB]"
            return BufferedInputFile(raw_bytes, filename="host_output.txt")

        if was_stopped:
            # Whenever the command was force-stopped, always hand the user a
            # .txt file of whatever output was captured before termination —
            # regardless of length — since that's the whole point of stopping
            # a runaway command like a bare `ping`.
            tail = safe_output[-3500:]
            if '\n' in tail and len(safe_output) > 3500:
                tail = tail[tail.index('\n') + 1:]
            inline = (
                header
                + (f"<i>Showing last captured lines — full output attached as a file.</i>\n\n<pre>{tail}</pre>"
                   if tail else "<i>No output was captured before the process was killed.</i>")
            )
            await message.answer(inline, parse_mode="HTML")
            await message.answer_document(
                _build_doc(),
                caption=f"📄 Captured output before stop: {target_cmd[:80]}"
            )
        elif len(full_text) <= 4096:
            await message.answer(full_text, parse_mode="HTML")
        else:
            # Show tail inline + attach full output as file
            tail = safe_output[-3500:]
            if '\n' in tail:
                tail = tail[tail.index('\n') + 1:]

            truncated = (
                header
                + "<i>⚠️ Output too long — showing last 3500 chars. Full output attached.</i>\n\n"
                + f"<pre>{tail}</pre>"
            )
            await message.answer(truncated, parse_mode="HTML")
            await message.answer_document(
                _build_doc(),
                caption=f"📄 Full output of: {target_cmd[:80]}"
            )

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
    dp.include_router(pydeploy_router)
    dp.include_router(runpy_router)

    sentinel = ResiliencyWatchdogEngine(bot, runtime_settings.ALLOWED_USER_ID)
    asyncio.create_task(sentinel.initialize_sentinel_loop())
    asyncio.create_task(pydeploy_service.supervisor_loop(bot, runtime_settings.ALLOWED_USER_ID))

    @dp.error()
    async def _global_error_handler(event, exception=None):
        """
        Last-resort safety net: catch anything an individual handler raises
        so ONE bad command (bad SSH creds, a malformed argument, a transient
        network blip) can never take the whole bot down. aiogram already
        isolates most handler exceptions per-update, but this makes sure
        nothing slips through silently and, where possible, tells the admin
        what broke instead of just going quiet.
        """
        exc = exception if exception is not None else getattr(event, "exception", None)
        logging.exception(f"Unhandled exception while processing an update: {exc}")
        try:
            update = getattr(event, "update", None)
            chat_id = None
            if update is not None:
                if getattr(update, "message", None):
                    chat_id = update.message.chat.id
                elif getattr(update, "callback_query", None):
                    chat_id = update.callback_query.message.chat.id
            if chat_id:
                await bot.send_message(
                    chat_id,
                    f"⚠️ <b>Internal error</b> — the command failed but the bot is still running.\n<code>{type(exc).__name__}: {str(exc)[:300]}</code>",
                    parse_mode="HTML"
                )
        except Exception:
            pass
        return True  # mark as handled so aiogram doesn't re-raise into polling

    logging.info("Starting safe long-polling system link sequences with Telegram APIs...")
    try:
        boot_msg = "🚀 <b>System Bootstrapped Successfully.</b> System online and monitoring containers."
        if sudo_bootstrap_changed:
            import html as _html
            boot_msg += f"\n\n🔐 <i>{_html.escape(sudo_bootstrap_msg)}</i>"
        await bot.send_message(
            runtime_settings.ALLOWED_USER_ID, 
            boot_msg, 
            parse_mode="HTML"
        )
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        if os.path.exists(health_marker):
            os.remove(health_marker)
        await bot.session.close()

if __name__ == "__main__":
    # The bot must survive at (almost) any cost: docker-compose already sets
    # `restart: always` on the container, but that only helps once the whole
    # Python process has died. This loop is a second, faster line of defense
    # — if main() ever raises something that escapes the in-process error
    # handler (e.g. a crash during startup/reconnect, before the dispatcher
    # is even running), log it and restart main() in-place with a short
    # backoff instead of letting the process exit and waiting on Docker.
    _consecutive_crashes = 0
    while True:
        try:
            asyncio.run(main())
            # main() only returns normally on a clean shutdown path; treat
            # that as intentional and stop looping.
            break
        except (KeyboardInterrupt, SystemExit):
            logging.info("System gracefully exiting operational execution cycles.")
            break
        except Exception as e:
            _consecutive_crashes += 1
            logging.exception(f"Fatal error in main() (crash #{_consecutive_crashes}) — restarting bot process in-place: {e}")
            # Back off a bit more each time so a persistent failure (e.g. bad
            # token, unreachable Telegram) doesn't spin the CPU/log flood.
            backoff = min(60, 5 * _consecutive_crashes)
            time.sleep(backoff)