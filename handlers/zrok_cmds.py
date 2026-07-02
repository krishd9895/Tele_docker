"""
Zrok + DuckDNS tunnel management commands.

/zrok_setup          — guided install + enroll wizard
/zrok_install_local  — install zrok from an uploaded .tar.gz file
/expose              — interactive wizard: port → health check → auth → tunnel
/tunnel_status       — list active tunnels + DuckDNS domain info
/revoke <id>         — kill a tunnel instantly

All commands require a valid 2FA session.
"""

import re
import asyncio
import os
import paramiko
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from services.zrok_service import zrok_engine
from services.duckdns_service import duckdns_service
from config.settings import runtime_settings
from middlewares.totp_auth import require_2fa
from utils.msg_cleaner import delete_after

zrok_router = Router()


# ── FSM States ────────────────────────────────────────────────────────────────

class ExposeStates(StatesGroup):
    waiting_for_target   = State()
    waiting_for_auth     = State()
    waiting_for_username = State()
    waiting_for_password = State()

class SetupStates(StatesGroup):
    waiting_for_token        = State()
    waiting_for_zrok_tarball = State()   # waiting for .tar.gz upload


# ── SSH helper ────────────────────────────────────────────────────────────────

def _ssh_exec(cmd: str, timeout: int = 60) -> tuple[int, str]:
    ssh_user = os.getenv("HOST_SSH_USER")
    ssh_pass = os.getenv("HOST_SSH_PASSWORD")
    if not ssh_user or not ssh_pass:
        return -1, "HOST_SSH_USER / HOST_SSH_PASSWORD not set"
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect("127.0.0.1", username=ssh_user, password=ssh_pass, timeout=15)
        _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
        code = stdout.channel.recv_exit_status()
        out = stdout.read().decode(errors="ignore") + stderr.read().decode(errors="ignore")
        return code, out.strip()
    except Exception as e:
        return -1, str(e)
    finally:
        ssh.close()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalise_target(raw: str) -> str:
    raw = raw.strip()
    if re.fullmatch(r'\d{2,5}', raw):
        return f"http://localhost:{raw}"
    if raw.startswith(":") and re.fullmatch(r':\d{2,5}', raw):
        return f"http://localhost{raw}"
    if not raw.startswith("http"):
        return f"http://{raw}"
    return raw


# ── /zrok_install_local ───────────────────────────────────────────────────────

@zrok_router.message(Command("zrok_install_local"))
@require_2fa
async def cmd_zrok_install_local(message: Message, state: FSMContext):
    """
    Manual zrok install from a downloaded binary.

    Usage:
      1. Download zrok_x.x.x_linux_amd64.tar.gz from GitHub releases
         https://github.com/openziti/zrok/releases/latest
      2. Send /zrok_install_local
      3. Send the .tar.gz file to this chat
      Bot extracts and installs it automatically.
    """
    await state.set_state(SetupStates.waiting_for_zrok_tarball)
    reply = await message.answer(
        "📦 <b>Manual zrok Install</b>\n\n"
        "<b>Step 1:</b> Download the Linux binary from GitHub:\n"
        "<a href='https://github.com/openziti/zrok/releases/latest'>"
        "github.com/openziti/zrok/releases/latest</a>\n\n"
        "Download: <code>zrok_x.x.x_linux_amd64.tar.gz</code>\n\n"
        "<b>Step 2:</b> Send that file to this chat now.\n\n"
        "<i>The bot will upload it to your WSL host, extract it, and install "
        "the binary to <code>/usr/local/bin/zrok</code> automatically.</i>",
        parse_mode="HTML",
        disable_web_page_preview=True
    )


@zrok_router.message(SetupStates.waiting_for_zrok_tarball, F.document)
async def handle_zrok_tarball(message: Message, state: FSMContext):
    """Receive the .tar.gz, upload to host via SFTP, extract, and install."""
    await state.clear()

    doc = message.document
    filename = doc.file_name or "zrok.tar.gz"

    if not (filename.endswith(".tar.gz") or filename.endswith(".tgz")):
        reply = await message.answer(
            "⚠️ That doesn't look like a <code>.tar.gz</code> file.\n"
            "Please send the correct zrok tarball.",
            parse_mode="HTML"
        )
        await delete_after(reply, delay=15)
        return

    status = await message.answer(
        f"📥 <b>Received:</b> <code>{filename}</code>\n"
        f"⏳ Uploading to WSL host...",
        parse_mode="HTML"
    )

    bot = message.bot
    ssh_user = os.getenv("HOST_SSH_USER")
    ssh_pass = os.getenv("HOST_SSH_PASSWORD")
    tmp_path = f"/tmp/{filename}"

    # ── 1. Download from Telegram and SFTP to host ────────────────────────
    try:
        tg_file = await bot.get_file(doc.file_id)
        file_bytes_io = await bot.download_file(tg_file.file_path)
        file_bytes = file_bytes_io.read()

        def _sftp_upload():
            import io
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect("127.0.0.1", username=ssh_user, password=ssh_pass, timeout=15)
            try:
                sftp = ssh.open_sftp()
                sftp.putfo(io.BytesIO(file_bytes), tmp_path)
                sftp.close()
            finally:
                ssh.close()

        await asyncio.to_thread(_sftp_upload)
    except Exception as e:
        await status.edit_text(f"❌ <b>Upload to host failed:</b>\n<code>{e}</code>", parse_mode="HTML")
        return

    await status.edit_text(
        f"✅ Uploaded to host at <code>{tmp_path}</code>\n"
        f"⏳ Extracting and installing...",
        parse_mode="HTML"
    )

    # ── 2. Extract + install on host ──────────────────────────────────────
    install_cmds = [
        f'tar -xzf {tmp_path} -C /tmp/',
        'sudo mv /tmp/zrok /usr/local/bin/zrok 2>/dev/null || mv /tmp/zrok /usr/local/bin/zrok',
        'chmod +x /usr/local/bin/zrok',
        f'rm -f {tmp_path}',
        'zrok version',
    ]

    output_lines = []
    success = True
    for cmd in install_cmds:
        code, out = await asyncio.to_thread(_ssh_exec, cmd)
        if out:
            output_lines.append(f"$ {cmd}\n{out}")
        if code != 0 and "zrok version" not in cmd:
            # mv might fail with sudo if not in sudoers — try without sudo
            if "sudo mv" in cmd:
                code2, out2 = await asyncio.to_thread(
                    _ssh_exec, cmd.replace("sudo mv", "mv")
                )
                if code2 == 0:
                    continue
            success = False
            output_lines.append(f"⚠️ Command failed (exit {code})")
            break

    trimmed = "\n".join(output_lines)[-2000:]

    if success:
        await status.edit_text(
            f"✅ <b>zrok Installed Successfully</b>\n\n"
            f"<pre>{trimmed}</pre>\n\n"
            f"Now tap /zrok_setup → <b>Enroll Account</b> to connect to your controller.",
            parse_mode="HTML"
        )
    else:
        await status.edit_text(
            f"⚠️ <b>Install may have partially failed</b>\n\n"
            f"<pre>{trimmed}</pre>\n\n"
            f"Try manually:\n"
            f"<code>/host mv /tmp/zrok /usr/local/bin/zrok</code>\n"
            f"<code>/host chmod +x /usr/local/bin/zrok</code>",
            parse_mode="HTML"
        )


def _tunnel_summary(tunnels: list[dict], duck_status: dict) -> str:
    lines = []

    # DuckDNS block
    if runtime_settings.DUCKDNS_TOKEN and runtime_settings.DUCKDNS_DOMAIN:
        auto = "🟢 Active" if duck_status["auto_update"] else "🔴 Off"
        lines.append(
            f"🦆 <b>DuckDNS</b>\n"
            f"  ↳ <b>Domain:</b> <code>{duck_status['domain']}</code>\n"
            f"  ↳ <b>Public IP:</b> <code>{duck_status['last_ip']}</code>\n"
            f"  ↳ <b>Last Update:</b> <code>{duck_status['last_updated']}</code>\n"
            f"  ↳ <b>Auto-Update:</b> {auto}\n"
        )
    else:
        lines.append("🦆 <b>DuckDNS:</b> Not configured — add DUCKDNS_TOKEN + DUCKDNS_DOMAIN to .env\n")

    # Tunnels block
    if not tunnels:
        lines.append("📭 <b>No active zrok tunnels.</b>")
    else:
        lines.append(f"🌐 <b>Active Tunnels ({len(tunnels)})</b>\n")
        for t in tunnels:
            auth_badge = f"🔒 <code>{t['auth_user']}</code>" if t["auth"] else "🔓 Open"
            lines.append(
                f"<b>ID:</b> <code>{t['id']}</code>\n"
                f"  ↳ <b>Local:</b>  <code>{t['target']}</code>\n"
                f"  ↳ <b>Public:</b> <code>{t['public_url']}</code>\n"
                f"  ↳ <b>Auth:</b>   {auth_badge}\n"
                f"  ↳ <b>Since:</b>  <code>{t['created_at']}</code>\n"
            )
    return "\n".join(lines)


# ── /zrok_setup ───────────────────────────────────────────────────────────────

@zrok_router.message(Command("zrok_setup"))
@require_2fa
async def cmd_zrok_setup(message: Message):
    """Show setup menu with inline buttons."""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Check Status",    callback_data="zrok:status")],
        [InlineKeyboardButton(text="📥 Install zrok",    callback_data="zrok:install")],
        [InlineKeyboardButton(text="🔑 Enroll Account",  callback_data="zrok:enroll")],
        [InlineKeyboardButton(text="🦆 Update DuckDNS",  callback_data="zrok:duckdns")],
    ])
    await message.answer(
        "⚙️ <b>Zrok Setup Panel</b>\n\n"
        "Use the buttons below to install and configure zrok from your phone.\n"
        "No PC needed — everything runs on your WSL host via the SSH bridge.\n\n"
        "<b>Order for first-time setup:</b>\n"
        "1️⃣ Check Status\n"
        "2️⃣ Install zrok (if not installed)\n"
        "3️⃣ Enroll Account (uses ZROK_PRIVATE_TOKEN from .env, or paste manually)\n"
        "4️⃣ Done — use /expose to create tunnels\n\n"
        "💡 Set <code>ZROK_PRIVATE_TOKEN</code> in .env to skip step 3 on every restart.",
        parse_mode="HTML",
        reply_markup=keyboard
    )


@zrok_router.callback_query(F.data == "zrok:status")
async def cb_zrok_status(call: CallbackQuery):
    await call.answer()
    msg = await call.message.answer("🔍 Checking zrok status on host...", parse_mode="HTML")

    installed = await zrok_engine.is_installed()
    if not installed:
        await msg.edit_text(
            "📦 <b>zrok:</b> ❌ Not installed\n\n"
            "Use the <b>Install zrok</b> button to install it.",
            parse_mode="HTML"
        )
        return

    enrolled = await zrok_engine.is_enrolled()
    raw_status = await zrok_engine.get_zrok_status()
    trimmed = raw_status[-2000:] if len(raw_status) > 2000 else raw_status

    await msg.edit_text(
        f"📦 <b>zrok:</b> ✅ Installed\n"
        f"🔑 <b>Enrollment:</b> {'✅ Enrolled' if enrolled else '❌ Not enrolled'}\n\n"
        f"<pre>{trimmed}</pre>",
        parse_mode="HTML"
    )


@zrok_router.callback_query(F.data == "zrok:install")
async def cb_zrok_install(call: CallbackQuery):
    await call.answer()
    msg = await call.message.answer(
        "📥 <b>Installing zrok on WSL host...</b>\n"
        "<i>This may take up to 60 seconds.</i>",
        parse_mode="HTML"
    )
    success, output = await zrok_engine.install_zrok()
    trimmed = output[-2000:] if len(output) > 2000 else output
    if success:
        await msg.edit_text(
            f"✅ <b>zrok Installed Successfully</b>\n\n<pre>{trimmed}</pre>\n\n"
            f"Next: tap <b>Enroll Account</b> and paste your <code>accountToken</code>\n"
            f"from your self-hosted zrok controller.",
            parse_mode="HTML"
        )
    else:
        await msg.edit_text(
            f"❌ <b>Installation Failed</b>\n\n<pre>{trimmed}</pre>",
            parse_mode="HTML"
        )


@zrok_router.callback_query(F.data == "zrok:enroll")
async def cb_zrok_enroll(call: CallbackQuery, state: FSMContext):
    await call.answer()

    # Token already in .env — enroll directly
    token = runtime_settings.ZROK_PRIVATE_TOKEN
    if token and token != "your_account_token_here":
        msg = await call.message.answer(
            "🔑 <b>Enrolling with token from .env...</b>\n"
            "<i>Using ZROK_PRIVATE_TOKEN already configured.</i>",
            parse_mode="HTML"
        )
        success, output = await zrok_engine.enroll_zrok(token)
        trimmed = output[-1500:] if len(output) > 1500 else output
        if success:
            await msg.edit_text(
                f"✅ <b>Enrollment Successful</b>\n\n<pre>{trimmed}</pre>\n\n"
                "You can now use /expose to create public tunnels.",
                parse_mode="HTML"
            )
        else:
            await msg.edit_text(
                f"❌ <b>Enrollment Failed</b>\n\n<pre>{trimmed}</pre>",
                parse_mode="HTML"
            )
        return

    # No token — auto-create account and enroll in one shot
    msg = await call.message.answer(
        "🔑 <b>No token found — Auto-creating zrok account...</b>\n\n"
        f"Using:\n"
        f"• Email: <code>{runtime_settings.ZROK_ACCOUNT_EMAIL}</code>\n"
        f"• Password: set in <code>ZROK_ACCOUNT_PASSWORD</code>\n\n"
        "<i>Running docker compose exec zrok-controller on your host...</i>",
        parse_mode="HTML"
    )
    success, result = await zrok_engine.create_account_and_get_token()
    if not success:
        trimmed = result[-1500:] if len(result) > 1500 else result
        await msg.edit_text(
            f"❌ <b>Account Creation Failed</b>\n\n<pre>{trimmed}</pre>\n\n"
            f"You can also paste the token manually — tap the button again after "
            f"running the command yourself.",
            parse_mode="HTML"
        )
        # Fall through to manual paste
        await state.set_state(SetupStates.waiting_for_token)
        await call.message.answer(
            "📋 Or paste your <code>accountToken</code> manually here:\n\n"
            "<i>Message deleted immediately after reading.</i>",
            parse_mode="HTML"
        )
        return

    # Got token — now enroll
    await msg.edit_text(
        f"✅ <b>Account created.</b> Now enrolling...",
        parse_mode="HTML"
    )
    enroll_ok, enroll_out = await zrok_engine.enroll_zrok(result)
    trimmed = enroll_out[-1500:] if len(enroll_out) > 1500 else enroll_out
    if enroll_ok:
        await msg.edit_text(
            f"✅ <b>Enrolled Successfully</b>\n\n<pre>{trimmed}</pre>\n\n"
            "💡 Add <code>ZROK_PRIVATE_TOKEN</code> to your .env to skip this next time:\n"
            f"<code>ZROK_PRIVATE_TOKEN={result[:20]}...</code>\n\n"
            "You can now use /expose to create public tunnels.",
            parse_mode="HTML"
        )
    else:
        await msg.edit_text(
            f"❌ <b>Enrollment Failed</b>\n\n<pre>{trimmed}</pre>",
            parse_mode="HTML"
        )


@zrok_router.message(SetupStates.waiting_for_token)
async def setup_got_token(message: Message, state: FSMContext):
    token = message.text.strip()

    try:
        await message.delete()
    except Exception:
        pass

    if not token:
        await message.answer("⚠️ Token cannot be empty.")
        return

    await state.clear()
    status_msg = await message.answer(
        "⏳ <b>Enrolling with zrok...</b>",
        parse_mode="HTML"
    )

    success, output = await zrok_engine.enroll_zrok(token)
    trimmed = output[-1500:] if len(output) > 1500 else output

    if success:
        await status_msg.edit_text(
            f"✅ <b>Enrollment Successful</b>\n\n<pre>{trimmed}</pre>\n\n"
            f"You can now use /expose to create public tunnels.",
            parse_mode="HTML"
        )
    else:
        await status_msg.edit_text(
            f"❌ <b>Enrollment Failed</b>\n\n<pre>{trimmed}</pre>",
            parse_mode="HTML"
        )


@zrok_router.callback_query(F.data == "zrok:duckdns")
async def cb_duckdns_update(call: CallbackQuery):
    await call.answer()

    if not runtime_settings.DUCKDNS_TOKEN or not runtime_settings.DUCKDNS_DOMAIN:
        await call.message.answer(
            "⚠️ <b>DuckDNS not configured.</b>\n\n"
            "Add these to your <code>.env</code> file and rebuild:\n"
            "<code>DUCKDNS_TOKEN=your-token-here</code>\n"
            "<code>DUCKDNS_DOMAIN=yourname</code>  <i>(just the subdomain, no .duckdns.org)</i>",
            parse_mode="HTML"
        )
        return

    msg = await call.message.answer("🦆 <b>Updating DuckDNS...</b>", parse_mode="HTML")
    success, result = await duckdns_service.update_ip()

    if success:
        domain = runtime_settings.DUCKDNS_DOMAIN
        if not domain.endswith(".duckdns.org"):
            domain = f"{domain}.duckdns.org"
        await msg.edit_text(
            f"✅ <b>DuckDNS Updated</b>\n\n"
            f"<b>Domain:</b> <code>{domain}</code>\n"
            f"<b>IP:</b> <code>{result}</code>",
            parse_mode="HTML"
        )
    else:
        await msg.edit_text(
            f"❌ <b>DuckDNS Update Failed</b>\n\n<code>{result}</code>",
            parse_mode="HTML"
        )


# ── /expose ───────────────────────────────────────────────────────────────────

@zrok_router.message(Command("expose"))
@require_2fa
async def cmd_expose_start(message: Message, state: FSMContext):
    await state.set_state(ExposeStates.waiting_for_target)
    await message.answer(
        "🌐 <b>Expose Wizard — Step 1/4</b>\n\n"
        "Enter the <b>local port or URL</b> to expose publicly:\n\n"
        "• <code>8080</code>\n"
        "• <code>3000</code>\n"
        "• <code>http://localhost:8181/api</code>",
        parse_mode="HTML"
    )


@zrok_router.message(ExposeStates.waiting_for_target)
async def expose_got_target(message: Message, state: FSMContext):
    target = _normalise_target(message.text)
    status_msg = await message.answer(
        f"🔍 <b>Step 2/4 — Health Check</b>\n\nPinging <code>{target}</code>...",
        parse_mode="HTML"
    )
    reachable, info = await zrok_engine.ping_target(target)

    if not reachable:
        await state.clear()
        await status_msg.edit_text(
            f"❌ <b>Health Check Failed</b>\n\n"
            f"<code>{target}</code> is unreachable.\n"
            f"<b>Reason:</b> <code>{info}</code>\n\n"
            "Make sure the service is running, then try /expose again.",
            parse_mode="HTML"
        )
        return

    await status_msg.edit_text(
        f"✅ <b>Health Check Passed</b>  ({info})\n<code>{target}</code> is reachable.",
        parse_mode="HTML"
    )
    await state.update_data(target=target)
    await state.set_state(ExposeStates.waiting_for_auth)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔒 Yes, protect it", callback_data="expose_auth:yes"),
            InlineKeyboardButton(text="🔓 No, open access", callback_data="expose_auth:no"),
        ]
    ])
    await message.answer(
        "🔐 <b>Step 3/4 — HTTP Basic Auth</b>\n\n"
        "Protect this tunnel with a username and password?",
        parse_mode="HTML",
        reply_markup=keyboard
    )


@zrok_router.callback_query(F.data.startswith("expose_auth:"))
async def expose_auth_button(call: CallbackQuery, state: FSMContext):
    choice = call.data.split(":")[1]
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer()

    if choice == "yes":
        await state.update_data(wants_auth=True)
        await state.set_state(ExposeStates.waiting_for_username)
        await call.message.answer("👤 <b>Step 3a — Enter Username</b>", parse_mode="HTML")
    else:
        data = await state.get_data()
        await state.clear()
        await _launch_tunnel(call.message, data["target"], auth_str=None)


@zrok_router.message(ExposeStates.waiting_for_auth)
async def expose_auth_text(message: Message, state: FSMContext):
    """Fallback: user typed yes/no instead of using buttons."""
    choice = message.text.strip().lower()
    if choice in ("yes", "y"):
        await state.update_data(wants_auth=True)
        await state.set_state(ExposeStates.waiting_for_username)
        await message.answer("👤 <b>Step 3a — Enter Username</b>", parse_mode="HTML")
    elif choice in ("no", "n"):
        data = await state.get_data()
        await state.clear()
        await _launch_tunnel(message, data["target"], auth_str=None)
    else:
        await message.answer("⚠️ Please tap a button or reply <b>yes</b> / <b>no</b>.", parse_mode="HTML")


@zrok_router.message(ExposeStates.waiting_for_username)
async def expose_got_username(message: Message, state: FSMContext):
    username = message.text.strip()
    if not username or ":" in username:
        await message.answer("⚠️ Username cannot be empty or contain <code>:</code>", parse_mode="HTML")
        return
    await state.update_data(auth_user=username)
    await state.set_state(ExposeStates.waiting_for_password)
    await message.answer(
        "🔑 <b>Step 3b — Enter Password</b>\n\n"
        "<i>Message deleted immediately after reading.</i>",
        parse_mode="HTML"
    )


@zrok_router.message(ExposeStates.waiting_for_password)
async def expose_got_password(message: Message, state: FSMContext):
    password = message.text.strip()
    try:
        await message.delete()
    except Exception:
        pass
    if not password:
        await message.answer("⚠️ Password cannot be empty.")
        return
    data = await state.get_data()
    await state.clear()
    await _launch_tunnel(message, data["target"], auth_str=f"{data['auth_user']}:{password}")


async def _launch_tunnel(message: Message, target: str, auth_str: str | None):
    status_msg = await message.answer(
        f"🚀 <b>Step 4/4 — Launching Tunnel</b>\n\n"
        f"Starting zrok for <code>{target}</code>...\n"
        f"<i>May take up to 40 seconds.</i>",
        parse_mode="HTML"
    )
    success, result, tunnel_id = await zrok_engine.create_share(target, basic_auth=auth_str)

    if not success:
        await status_msg.edit_text(
            f"❌ <b>Tunnel Launch Failed</b>\n\n<code>{result}</code>",
            parse_mode="HTML"
        )
        return

    auth_line = (
        f"🔒 <b>Auth:</b> <code>{auth_str.split(':')[0]}</code> / <i>[password hidden]</i>\n"
        if auth_str else "🔓 <b>Auth:</b> Open access\n"
    )

    # Show DuckDNS domain alongside zrok URL if configured
    duck_note = ""
    if runtime_settings.DUCKDNS_DOMAIN:
        domain = runtime_settings.DUCKDNS_DOMAIN
        if not domain.endswith(".duckdns.org"):
            domain = f"{domain}.duckdns.org"
        duck_note = (
            f"\n🦆 <b>DuckDNS:</b> <code>{domain}</code> points to your public IP.\n"
            f"   <i>Note: DuckDNS → your IP, zrok URL → this specific tunnel.</i>"
        )

    await status_msg.edit_text(
        f"✅ <b>Tunnel Active</b>\n\n"
        f"🆔 <b>ID:</b> <code>{tunnel_id}</code>\n"
        f"🏠 <b>Local:</b> <code>{target}</code>\n"
        f"🌐 <b>Public URL:</b> <code>{result}</code>\n"
        f"{auth_line}"
        f"{duck_note}\n\n"
        f"Use /revoke <code>{tunnel_id}</code> to shut it down.",
        parse_mode="HTML"
    )


# ── /tunnel_status ────────────────────────────────────────────────────────────

@zrok_router.message(Command("tunnel_status"))
@require_2fa
async def cmd_tunnel_status(message: Message):
    tunnels = zrok_engine.list_tunnels()
    duck_status = duckdns_service.get_status()
    await message.answer(_tunnel_summary(tunnels, duck_status), parse_mode="HTML")


# ── /revoke ───────────────────────────────────────────────────────────────────

@zrok_router.message(Command("revoke"))
@require_2fa
async def cmd_revoke(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        tunnels = zrok_engine.list_tunnels()
        if not tunnels:
            await message.answer(
                "📭 No active tunnels.\n\n<b>Usage:</b> <code>/revoke &lt;id&gt;</code>",
                parse_mode="HTML"
            )
            return
        # Show as inline buttons
        buttons = [
            [InlineKeyboardButton(
                text=f"🛑 {t['id']} — {t['target']}",
                callback_data=f"revoke:{t['id']}"
            )]
            for t in tunnels
        ]
        await message.answer(
            "🛑 <b>Select tunnel to revoke:</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
        )
        return

    await _do_revoke(message, args[1].strip())


@zrok_router.callback_query(F.data.startswith("revoke:"))
async def callback_revoke(call: CallbackQuery):
    tunnel_id = call.data.split("revoke:", 1)[1]
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer()
    await _do_revoke(call.message, tunnel_id)


async def _do_revoke(message: Message, query: str):
    record = zrok_engine.get_tunnel(query) or zrok_engine.find_by_url(query)
    if not record:
        await message.answer(
            f"❌ No tunnel found matching <code>{query}</code>.",
            parse_mode="HTML"
        )
        return
    tunnel_id = record["id"]
    status_msg = await message.answer(
        f"🛑 Revoking <code>{tunnel_id}</code>...", parse_mode="HTML"
    )
    success, info = await zrok_engine.revoke_share(tunnel_id)
    if success:
        await status_msg.edit_text(
            f"🛑 <b>Tunnel Revoked</b>\n\n"
            f"<code>{info}</code> is now offline.",
            parse_mode="HTML"
        )
    else:
        await status_msg.edit_text(f"❌ <b>Revoke Failed:</b> {info}", parse_mode="HTML")
