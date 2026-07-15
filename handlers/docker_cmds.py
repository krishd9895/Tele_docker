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
        "• /docker_ps - List containers, grouped by status\n"
        "• /docker_images - List images, grouped by usage\n"
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
        "• /docker_build - Pull + rebuild + restart a project (one command)\n\n"
        "<b>System Operations</b> <i>(requires 2FA)</i><b>:</b>\n"
        "• /shell <code>&lt;command&gt;</code> - Execute CLI directives (inside container)\n"
        "• /host <code>&lt;command&gt;</code> - Execute CLI directives (on WSL host)\n"
        "• /host_cd <code>&lt;path&gt; &lt;command&gt;</code> - Run command in specific directory on host\n"
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

# Ordered so the most "actionable" states surface first, with a fallback
# bucket for any status Docker reports that isn't explicitly listed here.
_CONTAINER_STATUS_GROUPS = [
    ("running", "🟢 Running"),
    ("restarting", "🔁 Restarting"),
    ("paused", "🟡 Paused"),
    ("created", "⚪ Created"),
    ("exited", "🔴 Exited"),
    ("dead", "⚫ Dead"),
    ("removing", "🗑 Removing"),
]


def _group_containers_by_status(containers: list) -> str:
    """Group containers by their live status into labeled sections."""
    buckets: dict[str, list] = {}
    for c in containers:
        buckets.setdefault(c.status, []).append(c)

    lines = []
    seen_statuses = set()

    def render_bucket(label: str, items: list):
        lines.append(f"<b>{label} ({len(items)})</b>")
        for c in items:
            tag = c.image.tags[0] if c.image.tags else "Untagged"
            lines.append(f"• <code>{c.name}</code> | {tag} | <code>{c.status}</code>")
        lines.append("")

    for status_key, label in _CONTAINER_STATUS_GROUPS:
        items = buckets.get(status_key)
        if items:
            render_bucket(label, items)
            seen_statuses.add(status_key)

    # Anything Docker reports that we didn't explicitly account for above
    for status_key, items in buckets.items():
        if status_key not in seen_statuses:
            render_bucket(f"❔ {status_key.title()}", items)

    return "\n".join(lines).rstrip()


@docker_router.message(Command("docker_ps"))
async def cmd_docker_ps(message: Message):
    await delete_command(message)
    status_msg = await message.answer("🔄 Compiling system matrix state variables...")
    try:
        containers = await docker_engine.get_all_containers()
        if not containers:
            await status_msg.edit_text("ℹ️ No container clusters observed on this engine.")
            await delete_after(status_msg, delay=15)
            return

        grouped = _group_containers_by_status(containers)
        response = f"<b>📦 Container Footprints ({len(containers)} total):</b>\n\n{grouped}"
        await status_msg.edit_text(response, parse_mode="HTML")
        await delete_after(status_msg, delay=30)
    except Exception as err:
        await status_msg.edit_text(f"❌ Structural exception: {str(err)}")
        await delete_after(status_msg, delay=20)


def _format_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


@docker_router.message(Command("docker_images"))
async def cmd_docker_images(message: Message):
    await delete_command(message)
    status_msg = await message.answer("🔄 Compiling image registry state...")
    try:
        images, containers = await asyncio.gather(
            docker_engine.get_all_images(),
            docker_engine.get_all_containers(),
        )
        if not images:
            await status_msg.edit_text("ℹ️ No images present on this engine.")
            await delete_after(status_msg, delay=15)
            return

        # Group images by whether some container (running or not) currently
        # references them — that's the operational split that matters for
        # deciding whether an image is safe to prune.
        in_use_ids = {c.image.id for c in containers}

        in_use, unused, dangling = [], [], []
        for img in images:
            if not img.tags:
                dangling.append(img)
            elif img.id in in_use_ids:
                in_use.append(img)
            else:
                unused.append(img)

        def render_group(label: str, items: list) -> list[str]:
            out = [f"<b>{label} ({len(items)})</b>"]
            for img in items:
                tags = ", ".join(img.tags) if img.tags else "&lt;none&gt;:&lt;none&gt;"
                size = _format_bytes(img.attrs.get("Size", 0))
                short_id = img.short_id.replace("sha256:", "")
                out.append(f"• <code>{tags}</code> | {short_id} | {size}")
            out.append("")
            return out

        lines: list[str] = []
        if in_use:
            lines += render_group("📌 In Use", in_use)
        if unused:
            lines += render_group("📦 Unused (tagged, no container)", unused)
        if dangling:
            lines += render_group("🗑 Dangling (untagged)", dangling)

        body = "\n".join(lines).rstrip()
        response = f"<b>🖼️ Images ({len(images)} total):</b>\n\n{body}"
        if len(response) > 4000:
            response = response[:4000] + "\n\n<i>… truncated</i>"
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


# ── /docker_build ─────────────────────────────────────────────────────────────
# Full pipeline: git pull → compose down → compose build → compose up -d
# Shows the gitpull repo picker so user taps a project, bot handles the rest.

@docker_router.message(Command("docker_build"))
@require_2fa
async def cmd_docker_build(message: Message):
    """
    One-command full rebuild pipeline:
      1. Show repo picker (same as /gitpull)
      2. User taps a project
      3. git pull
      4. docker compose down
      5. docker compose build
      6. docker compose up -d
    """
    from services.git_service import git_engine

    repos = await git_engine.list_repos()

    if not repos:
        reply = await message.answer(
            "📭 <b>No repositories found.</b>\n\n"
            "Use /gitclone to clone a project first.",
            parse_mode="HTML"
        )
        await delete_after(reply, delay=15)
        return

    buttons = []
    for repo in repos:
        loc = "🖥️" if repo.get("location") == "host" else "🐳"
        buttons.append([InlineKeyboardButton(
            text=f"{loc} {repo['name']}",
            callback_data=f"dbuild:{repo['path']}"
        )])

    lines = ["🔨 <b>Select project to rebuild:</b>\n"]
    for i, repo in enumerate(repos, 1):
        loc = "🖥️ host" if repo.get("location") == "host" else "🐳 container"
        lines.append(f"{i}. <code>{repo['name']}</code>  <i>({loc})</i>")
        lines.append(f"   📂 <code>{repo['path']}</code>\n")

    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@docker_router.callback_query(F.data.startswith("dbuild:"))
async def callback_docker_build(call: CallbackQuery):
    repo_path = call.data.split("dbuild:", 1)[1]
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer()
    await _do_docker_build(call.message, repo_path)


async def _do_docker_build(message: Message, repo_path: str):
    """Execute the full pipeline: pull → down → build → up."""
    from services.git_service import git_engine
    import html as _html

    project_name = repo_path.rstrip("/").split("/")[-1]

    status = await message.answer(
        f"🔨 <b>Build Pipeline: {project_name}</b>\n\n"
        f"📂 <code>{repo_path}</code>\n\n"
        f"⏳ Step 1/3 — Pulling latest changes...",
        parse_mode="HTML"
    )

    # ── Step 1: git pull ──────────────────────────────────────────────────────
    pull_ok, pull_result = await git_engine.pull(repo_path)

    if not pull_ok:
        await status.edit_text(
            f"❌ <b>Build aborted — git pull failed</b>\n\n"
            f"<pre>{_html.escape(pull_result)}</pre>",
            parse_mode="HTML"
        )
        return

    pull_summary = pull_result[:300] if len(pull_result) > 300 else pull_result

    await status.edit_text(
        f"🔨 <b>Build Pipeline: {project_name}</b>\n\n"
        f"✅ Step 1/3 — Pull: <code>{_html.escape(pull_summary)}</code>\n\n"
        f"⏳ Step 2/3 — Building images...\n"
        f"<i>This may take a few minutes for large images.</i>",
        parse_mode="HTML"
    )

    # ── Step 2: compose build ─────────────────────────────────────────────────
    build_ok, build_out = await docker_engine.compose_build(repo_path)

    if not build_ok:
        trimmed = _html.escape(build_out[-2000:] if len(build_out) > 2000 else build_out)        
        await status.edit_text(
            f"❌ <b>Build failed at Step 2/3</b>\n\n"
            f"<pre>{trimmed}</pre>",
            parse_mode="HTML"
        )
        return

    await status.edit_text(
        f"🔨 <b>Build Pipeline: {project_name}</b>\n\n"
        f"✅ Step 1/3 — Pull done\n"
        f"✅ Step 2/3 — Build complete\n\n"  
        f"⏳ Step 3/3 — Starting stack...",  
        parse_mode="HTML"
    )

    # ── Step 3: compose up -d ─────────────────────────────────────────────────
    # IMPORTANT: If this is the bot's own project, compose up will kill this
    # process. Send the message FIRST, then trigger restart as fire-and-forget.
    from utils.ssh_helper import get_ssh_creds
    user, _ = get_ssh_creds()
    path_not_in_container = not __import__('pathlib').Path(repo_path).exists()

    # Check if this is our own TeleDocker repo (so we can only restart tg-manager-bot).
    # This is the ONLY case where the bot's own process is at risk of being killed
    # mid-command — every other project (even ones that live on the host and are
    # therefore driven over SSH) is safe to just await normally.
    is_teledocker_repo = project_name == "Tele_docker" or project_name == "Tele_docker-main"
    is_self_restart = is_teledocker_repo

    if is_self_restart:
        # Auto-detect manifest on host first
        manifest_name = None
        for cf in ["docker-compose.yml", "docker-compose.yaml", "compose.yaml", "compose.yml"]:
            check_cmd = f'test -f "{repo_path}/{cf}" && echo "{cf}"'
            from utils.ssh_helper import ssh_exec
            code, out = await asyncio.to_thread(ssh_exec, check_cmd)
            if code == 0 and out.strip():
                manifest_name = out.strip()
                break
        
        if not manifest_name:
            await status.edit_text(
                f"❌ <b>Error: No compose file found in {project_name}</b>",
                parse_mode="HTML"
            )
            return

        # Send completion message BEFORE triggering restart
        await status.edit_text(
            f"✅ <b>Build Pipeline Complete: {project_name}</b>\n\n"
            f"✅ git pull\n"
            f"✅ docker compose build\n"
            f"🔄 docker compose up -d  ← <i>restarting now via host SSH...</i>\n\n"
            f"<i>This process will be killed by its own restart, so I can't wait on it "
            f"directly. A follow-up message confirming whether tg-manager-bot actually "
            f"came back up will arrive in a few seconds.</i>",
            parse_mode="HTML"
        )
        # Small delay to ensure message is delivered before the bot dies
        await asyncio.sleep(2)

        # This process (tg-manager-bot) is about to be killed by the very command
        # we're issuing below, so anything we try to read back from the SSH channel
        # in-process is unreliable — the channel dies with the container. Instead,
        # build a single self-contained host-side script that:
        #   1. runs the restart,
        #   2. waits for the new container to come up,
        #   3. checks its actual running/health state via `docker inspect`,
        #   4. reports the real result straight to Telegram via curl (bypassing
        #      this process entirely).
        # It's launched fully detached (nohup + setsid) so it keeps running even
        # if the SSH session/container is torn down mid-flight.
        from utils.ssh_helper import ssh_exec
        from config.settings import runtime_settings
        bot_token = runtime_settings.TELEGRAM_BOT_TOKEN
        chat_id = runtime_settings.ALLOWED_USER_ID

        verify_script = (
            f'cd "{repo_path}" && '
            f'docker compose -f "{manifest_name}" up -d --no-deps tg-manager-bot > /tmp/tdk_restart.log 2>&1; '
            'ok=0; '
            'for i in $(seq 1 15); do '
            '  sleep 2; '
            '  running=$(docker inspect -f "{{.State.Running}}" tg_manager_bot 2>/dev/null); '
            '  if [ "$running" = "true" ]; then ok=1; break; fi; '
            'done; '
            'if [ "$ok" = "1" ]; then '
            '  health=$(docker inspect -f "{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}" tg_manager_bot 2>/dev/null); '
            f'  text="\u2705 tg-manager-bot restarted successfully and is running (health: $health)."; '
            'else '
            f'  text="\u26a0\ufe0f tg-manager-bot did NOT come back up after the restart. Check with docker logs tg_manager_bot on the host. Last compose output: $(tail -c 500 /tmp/tdk_restart.log | tr -d "\\n" | head -c 500)"; '
            'fi; '
            f'curl -s -X POST "https://api.telegram.org/bot{bot_token}/sendMessage" '
            f'--data-urlencode "chat_id={chat_id}" --data-urlencode "text=$text" >/dev/null 2>&1'
        )
        # setsid detaches the script from the SSH session so it survives even if
        # the container (and its SSH channel) dies mid-command; nohup covers shells
        # where job control / disown isn't available.
        detached_cmd = f"nohup setsid sh -c '{verify_script}' > /dev/null 2>&1 < /dev/null &"

        # Fire-and-forget from this process's point of view — the script above is
        # self-sufficient and reports its own result directly to the user.
        import threading
        def _run_in_background():
            try:
                ssh_exec(detached_cmd, timeout=10)
            except:
                pass
        threading.Thread(target=_run_in_background, daemon=True).start()
    else:
        # Any project other than the bot's own repo — safe to await, even if it
        # lives on the host and has to be driven over SSH, since restarting some
        # unrelated stack can't kill this process. compose_up() already knows how
        # to route host-path projects over SSH internally.
        up_ok, up_out = await docker_engine.compose_up(repo_path)
        up_trimmed = _html.escape(up_out[-1000:] if len(up_out) > 1000 else up_out)

        if up_ok:
            await status.edit_text(
                f"✅ <b>Build Pipeline Complete: {project_name}</b>\n\n"
                f"✅ git pull\n"
                f"✅ docker compose build\n"
                f"✅ docker compose up -d\n\n"
                f"<pre>{up_trimmed}</pre>",
                parse_mode="HTML"
            )
        else:
            await status.edit_text(
                f"⚠️ <b>Build done but stack failed to start</b>\n\n"
                f"<pre>{up_trimmed}</pre>",
                parse_mode="HTML"
            )
