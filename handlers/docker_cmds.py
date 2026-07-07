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
        "<b>🌐 Tunnel Management</b>\n"
        "• /expose - Temporary public URL (no account needed)\n"
        "• /expose_perm - Permanent URL (free Cloudflare account)\n"
        "• /expose_setup - Install cloudflared manually\n"
        "• /tunnel_status - Active tunnels + DuckDNS status\n"
        "• /revoke <code>&lt;id&gt;</code> - Kill a specific tunnel\n"
        "• /revoke_all - Kill ALL tunnels (cleanup after restart)\n\n"
        "<b>🦆 DuckDNS:</b>\n"
        "• /duckdns - Configure domain, update IP, manage auto-update\n\n"
        "<b>⚙️ Settings:</b>\n"
        "• /showenv - View current .env settings\n"
        "• /setenv <code>&lt;KEY&gt; &lt;value&gt;</code> - Update any .env key\n\n"
        "<b>System Operations</b> <i>(requires 2FA)</i><b>:</b>\n"
        "• /shell <code>&lt;command&gt;</code> - Execute CLI directives\n"
        "• /health - Infrastructure Hardware Dashboard\n"
        "• /download - Browse host folders and download a file\n"
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
    status_msg = await message.answer("📊 <i>Compiling comprehensive system metrics...</i>", parse_mode="HTML")
    try:
        metrics = await monitor_service.compile_system_health_dashboard()
        
        # Build disk info
        disk_lines = []
        for d in metrics['disks']:
            free_gb = d['free'] // (1024**3)
            used_percent = d['percent']
            emoji = "🟢" if used_percent < 70 else "🟡" if used_percent < 90 else "🔴"
            disk_lines.append(f"{emoji} <code>{d['mountpoint']}</code>: {used_percent}% used ({free_gb} GB free)")
        
        # Build container list
        container_lines = []
        if metrics['docker']['running_containers']:
            for c in metrics['docker']['running_containers']:
                container_lines.append(f"• 🟢 <code>{c}</code>")
        else:
            container_lines.append("• No running containers")
        
        dashboard = (
            f"🖥️ <b>System Health Dashboard</b>\n\n"
            
            f"<b>🤖 Bot Status</b>\n"
            f"• Uptime: <code>{metrics['bot']['uptime']}</code>\n\n"
            
            f"<b>💻 System Info</b>\n"
            f"• OS: <code>{metrics['system']['os']}</code>\n"
            f"• Kernel: {metrics['system']['wsl']}\n"
            f"• System Uptime: <code>{metrics['system']['uptime']}</code>\n"
            f"• Processes: <code>{metrics['system']['processes']}</code>\n\n"
            
            f"<b>⚡ CPU</b>\n"
            f"• Usage: <code>{metrics['cpu']['usage']}</code>\n"
            f"• Cores: <code>{metrics['cpu']['cores']}</code>\n"
            f"• Frequency: <code>{metrics['cpu']['freq']}</code>\n"
            f"• Load Avg: <code>{metrics['cpu']['load_avg']}</code>\n\n"
            
            f"<b>🧠 Memory</b>\n"
            f"• RAM: <code>{metrics['ram']['percent']}</code> ({metrics['ram']['used']} / {metrics['ram']['total']})\n"
            f"• Swap: <code>{metrics['swap']['percent']}</code> ({metrics['swap']['used']} / {metrics['swap']['total']})\n\n"
            
            f"<b>💾 Storage</b>\n"
            + "\n".join(disk_lines) + "\n\n"
            
            f"<b>🌐 Network</b>\n"
            f"• Sent: <code>{metrics['network']['bytes_sent']}</code>\n"
            f"• Received: <code>{metrics['network']['bytes_recv']}</code>\n"
            f"• Interfaces: {', '.join(f'<code>{iface}</code>' for iface in metrics['network']['interfaces'])}\n\n"
            
            f"<b>🐳 Docker</b>\n"
            f"• Status: {metrics['docker']['status']}\n"
            + "\n".join(container_lines)
        )
        
        await status_msg.edit_text(dashboard, parse_mode="HTML")
        await delete_after(status_msg, delay=45)
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
        # Cap log bytes to avoid huge messages
        if len(logs_bytes) > 512 * 1024:
            logs_bytes = logs_bytes[-512 * 1024:]
        logs_text = logs_bytes.decode('utf-8', errors='replace')
        import html as _html
        safe_logs = _html.escape(logs_text)
        if len(safe_logs) > 4000:
            file_payload = BufferedInputFile(logs_bytes, filename=f"{target}_logs.txt")
            await message.answer_document(file_payload, caption=f"📄 Logs for: {target}")
            await status_msg.delete()
        else:
            await status_msg.edit_text(f"📝 <b>Logs for {target}:</b>\n\n<pre>{safe_logs}</pre>", parse_mode="HTML")
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
    import html as _html
    trimmed = _html.escape(output[-3000:] if len(output) > 3000 else output)
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
    import html as _html
    trimmed = _html.escape(output[-3000:] if len(output) > 3000 else output)
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
