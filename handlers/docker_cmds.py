from aiogram import Router, F
from aiogram.types import Message, BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from services.docker_service import docker_engine
from services.monitor_service import monitor_service
from middlewares.totp_auth import require_2fa
from utils.msg_cleaner import delete_command, delete_after, auto_clean
import asyncio

docker_router = Router()

@docker_router.message(Command("start"))
@docker_router.message(Command("help"))
async def cmd_welcome_menu(message: Message):
    menu_text = (
        "🤖 <b>Telegram Docker Management Engine</b>\n\n"
        "<b>🔐 2FA Authentication:</b>\n"
        "• /verify <code>&lt;code&gt;</code> - Authenticate with Google Authenticator\n"
        "• /2fa_status - Show session time remaining\n"
        "• /lock - Revoke current 2FA session\n\n"
        "<b>Core Docker Controls:</b>\n"
        "• /docker_ps - List running containers\n"
        "• /docker_logs <code>&lt;name&gt;</code> - View container logs\n"
        "• /docker_restart <code>&lt;name&gt;</code> - Restart a container\n"
        "• /docker_stop <code>&lt;name&gt;</code> - Stop a container\n\n"
        "<b>Compose Stack Controls</b> <i>(requires 2FA)</i><b>:</b>\n"
        "• /compose_up - Bring a stack up (<code>up -d</code>)\n"
        "• /compose_down - Bring a stack down\n\n"
        "<b>Git Operations:</b>\n"
        "• /gitclone <code>&lt;url&gt; [path]</code> - Clone a repository\n"
        "• /gitpull - Pull latest changes (picker or path)\n\n"
        "<b>🌐 Tunnel Management</b> <i>(requires 2FA)</i><b>:</b>\n"
        "• /zrok_setup - Install/enroll zrok + update DuckDNS\n"
        "• /expose - Wizard: expose a local port via zrok\n"
        "• /tunnel_status - Active tunnels + DuckDNS IP status\n"
        "• /revoke <code>&lt;id&gt;</code> - Kill a tunnel instantly\n\n"
        "<b>System Operations</b> <i>(requires 2FA)</i><b>:</b>\n"
        "• /shell <code>&lt;command&gt;</code> - Execute CLI directives\n"
        "• /health - Infrastructure Hardware Dashboard\n"
        "• /ls - Quick directory index listing\n"
        "• /cat <code>&lt;filename&gt;</code> - Print file text contents\n"
        "• /download <code>&lt;path&gt;</code> - Pull file over Telegram\n"
        "• /upload - Push any file to a WSL path\n\n"
        "⚡ <i>Perimeter secured. Awaiting instruction parameters...</i>\n\n"
        "📖 For detailed usage of new features: /help_guide"
    )
    reply = await message.answer(menu_text, parse_mode="HTML")
    # Delete the /help command, keep the menu visible for 60s then clean up
    await delete_command(message)
    await delete_after(reply, delay=60)

@docker_router.message(Command("docker_ps"))
async def cmd_docker_ps(message: Message):
    await delete_command(message)
    status_msg = await message.answer("🔄 Compiling system matrix state variables...")
    try:
        containers = await docker_engine.get_running_containers()
        if not containers:
            await status_msg.edit_text("ℹ️ No actively operating container clusters observed running globally.")
            await delete_after(status_msg, delay=15)
            return
        response = "<b>📦 Active Container Footprints:</b>\n\n"
        for c in containers:
            tag = c.image.tags[0] if c.image.tags else 'Untagged'
            response += f"• <code>{c.name}</code> | {tag} | 🟢 <code>{c.status}</code>\n"
        await status_msg.edit_text(response, parse_mode="HTML")
        await delete_after(status_msg, delay=30)
    except Exception as err:
        await status_msg.edit_text(f"❌ Structural exception: {str(err)}")
        await delete_after(status_msg, delay=20)

@docker_router.message(Command("health"))
async def cmd_health(message: Message):
    await delete_command(message)
    status_msg = await message.answer("📊 <i>Compiling physical cluster metrics...</i>", parse_mode="HTML")
    try:
        metrics = await monitor_service.compile_system_health_dashboard()
        dashboard = (
            f"🖥️ <b>WSL Node Health Infrastructure</b>\n\n"
            f"• <b>Uptime:</b> <code>{metrics['uptime']}</code>\n"
            f"• <b>CPU Load:</b> <code>{metrics['cpu']}</code>\n"
            f"• <b>RAM State:</b> <code>{metrics['ram']}</code>\n"
            f"• <b>Storage Space:</b> <code>{metrics['disk']}</code>\n\n"
            f"🐳 <b>Container Engine Topology:</b>\n"
            f"• <b>Docker Socket:</b> {metrics['docker']}\n"
            f"• <b>OS Layer Subsystem:</b> {metrics['wsl']}\n"
        )
        await status_msg.edit_text(dashboard, parse_mode="HTML")
        await delete_after(status_msg, delay=30)
    except Exception as e:
        await status_msg.edit_text(f"❌ Failed to extract hardware vectors: {e}")
        await delete_after(status_msg, delay=20)

@docker_router.message(Command("docker_logs"))
async def cmd_docker_logs(message: Message):
    args = message.text.split()
    if len(args) < 2:
        reply = await message.answer("⚠️ Usage format: <code>/docker_logs &lt;container_name&gt;</code>", parse_mode="HTML")
        await auto_clean(message, reply, reply_delay=10)
        return
    await delete_command(message)
    target = args[1]
    status_msg = await message.answer(f"🔍 Extracting logs out of <code>{target}</code>...", parse_mode="HTML")
    try:
        containers = await docker_engine.get_all_containers()
        matched = [c for c in containers if c.name == target]
        if not matched:
            await status_msg.edit_text("❌ Container not found.")
            await delete_after(status_msg, delay=10)
            return
        logs_bytes = matched[0].logs(tail=200)
        logs_text = logs_bytes.decode('utf-8', errors='replace')
        if len(logs_text) > 4000:
            file_payload = BufferedInputFile(logs_bytes, filename=f"{target}_logs.txt")
            await message.answer_document(file_payload, caption=f"📄 Logs for: {target}")
            await status_msg.delete()
        else:
            await status_msg.edit_text(f"📝 <b>Logs for {target}:</b>\n\n<pre>{logs_text}</pre>", parse_mode="HTML")
            # Logs are useful — keep for 2 minutes
            await delete_after(status_msg, delay=120)
    except Exception as trace_err:
        await status_msg.edit_text(f"❌ Error compiling logs sequence: {trace_err}")
        await delete_after(status_msg, delay=20)

@docker_router.message(Command("docker_restart"))
async def cmd_docker_restart(message: Message):
    args = message.text.split()
    if len(args) < 2:
        reply = await message.answer("⚠️ Usage: <code>/docker_restart &lt;name&gt;</code>", parse_mode="HTML")
        await auto_clean(message, reply, reply_delay=10)
        return
    await delete_command(message)
    target = args[1]
    status_msg = await message.answer(f"🔄 Power-cycling container: <code>{target}</code>...", parse_mode="HTML")
    try:
        containers = await docker_engine.get_all_containers()
        matched = [c for c in containers if c.name == target]
        if not matched:
            await status_msg.edit_text("❌ Container not found.")
        else:
            await asyncio.to_thread(matched[0].restart)
            await status_msg.edit_text(f"✅ <b>Container power-cycled:</b> <code>{target}</code>", parse_mode="HTML")
        await delete_after(status_msg, delay=12)
    except Exception as err:
        await status_msg.edit_text(f"❌ Cycle interruption fault: {err}")
        await delete_after(status_msg, delay=20)

@docker_router.message(Command("docker_stop"))
async def cmd_docker_stop(message: Message):
    args = message.text.split()
    if len(args) < 2:
        reply = await message.answer("⚠️ Usage: <code>/docker_stop &lt;name&gt;</code>", parse_mode="HTML")
        await auto_clean(message, reply, reply_delay=10)
        return
    await delete_command(message)
    target = args[1]
    status_msg = await message.answer(f"🛑 Stopping container: <code>{target}</code>...", parse_mode="HTML")
    try:
        containers = await docker_engine.get_all_containers()
        matched = [c for c in containers if c.name == target]
        if matched:
            await asyncio.to_thread(matched[0].stop)
            await status_msg.edit_text(f"🛑 <b>Container stopped safely:</b> <code>{target}</code>", parse_mode="HTML")
        else:
            await status_msg.edit_text("❌ Container not found.")
        await delete_after(status_msg, delay=12)
    except Exception as err:
        await status_msg.edit_text(f"❌ Stop failed: {err}")
        await delete_after(status_msg, delay=20)


# ── Shared helper: build project picker keyboard ──────────────────────────────

async def _compose_project_keyboard(action: str):
    """Returns (text, keyboard) listing all compose projects, or (None, None) if empty."""
    projects = await docker_engine.list_compose_projects()
    if not projects:
        return None, None

    buttons = []
    for p in projects:
        buttons.append([InlineKeyboardButton(
            text=f"📦 {p['name']}  ({p['manifest']})",
            callback_data=f"{action}:{p['path']}:{p['manifest']}"
        )])

    lines = [f"🐳 <b>Select a project to <code>{action}</code>:</b>\n"]
    for i, p in enumerate(projects, 1):
        loc = "🖥️ host" if p.get("location") == "host" else "🐳 container"
        lines.append(f"{i}. <code>{p['name']}</code>  —  <code>{p['manifest']}</code>  <i>({loc})</i>")
        lines.append(f"   📂 <code>{p['path']}</code>\n")

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


# ── /compose_up ───────────────────────────────────────────────────────────────

@docker_router.message(Command("compose_up"))
@require_2fa
async def cmd_compose_up(message: Message):
    """
    /compose_up              — pick a project from workspace
    /compose_up <path>       — run up -d directly on given path
    """
    args = message.text.split(maxsplit=1)

    if len(args) == 2:
        await _do_compose_up(message, args[1].strip())
        return

    text, keyboard = await _compose_project_keyboard("cup")
    if not keyboard:
        await message.answer(
            "📭 <b>No compose projects found</b> in <code>data/workspaces</code>.\n\n"
            "Clone a project first with /gitclone, or pass the path directly:\n"
            "<code>/compose_up /path/to/project</code>",
            parse_mode="HTML"
        )
        return
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@docker_router.callback_query(F.data.startswith("cup:"))
async def callback_compose_up(call: CallbackQuery):
    _, path, manifest = call.data.split(":", 2)
    await call.message.edit_reply_markup(reply_markup=None)
    await _do_compose_up(call.message, path, manifest)


async def _do_compose_up(message: Message, project_path: str, manifest: str = None):
    status_msg = await message.answer(
        f"🚀 <b>Bringing stack up...</b>\n<code>{project_path}</code>",
        parse_mode="HTML"
    )
    success, output = await docker_engine.compose_up(project_path, manifest)
    trimmed = output[-3000:] if len(output) > 3000 else output
    if success:
        await status_msg.edit_text(
            f"✅ <b>Compose Up Successful</b>\n\n"
            f"<b>Path:</b> <code>{project_path}</code>\n\n"
            f"<pre>{trimmed}</pre>",
            parse_mode="HTML"
        )
        await delete_after(status_msg, delay=60)
    else:
        await status_msg.edit_text(
            f"❌ <b>Compose Up Failed</b>\n\n<pre>{trimmed}</pre>",
            parse_mode="HTML"
        )
        await delete_after(status_msg, delay=30)


# ── /compose_down ─────────────────────────────────────────────────────────────

@docker_router.message(Command("compose_down"))
@require_2fa
async def cmd_compose_down(message: Message):
    """
    /compose_down              — pick a project from workspace
    /compose_down <path>       — run down directly on given path
    """
    args = message.text.split(maxsplit=1)

    if len(args) == 2:
        await _do_compose_down(message, args[1].strip())
        return

    text, keyboard = await _compose_project_keyboard("cdown")
    if not keyboard:
        await message.answer(
            "📭 <b>No compose projects found</b> in <code>data/workspaces</code>.\n\n"
            "Pass the path directly:\n"
            "<code>/compose_down /path/to/project</code>",
            parse_mode="HTML"
        )
        return
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@docker_router.callback_query(F.data.startswith("cdown:"))
async def callback_compose_down(call: CallbackQuery):
    _, path, manifest = call.data.split(":", 2)
    await call.message.edit_reply_markup(reply_markup=None)
    await _do_compose_down(call.message, path, manifest)


async def _do_compose_down(message: Message, project_path: str, manifest: str = None):
    status_msg = await message.answer(
        f"🛑 <b>Bringing stack down...</b>\n<code>{project_path}</code>",
        parse_mode="HTML"
    )
    success, output = await docker_engine.compose_down(project_path, manifest)
    trimmed = output[-3000:] if len(output) > 3000 else output
    if success:
        await status_msg.edit_text(
            f"🛑 <b>Compose Down Successful</b>\n\n"
            f"<b>Path:</b> <code>{project_path}</code>\n\n"
            f"<pre>{trimmed}</pre>",
            parse_mode="HTML"
        )
        await delete_after(status_msg, delay=30)
    else:
        await status_msg.edit_text(
            f"❌ <b>Compose Down Failed</b>\n\n<pre>{trimmed}</pre>",
            parse_mode="HTML"
        )
        await delete_after(status_msg, delay=30)
