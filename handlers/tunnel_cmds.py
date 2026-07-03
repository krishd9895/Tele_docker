"""
Tunnel management via cloudflared.

Commands:
  /expose_setup    — install cloudflared (auto on first /expose)
  /expose          — temporary tunnel wizard (no account needed)
  /expose_perm     — permanent tunnel using Cloudflare account token
  /tunnel_status   — list active tunnels + DuckDNS info
  /revoke <id>     — kill a specific tunnel
  /revoke_all      — kill ALL tunnels (cleanup after restart)

Temporary tunnels: no account, random URL, changes each run
Permanent tunnels: free Cloudflare account, fixed URL, your own subdomain
"""

import re
import asyncio
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from services.tunnel_service import tunnel_engine, _ssh_exec
from services.duckdns_service import duckdns_service
from config.settings import runtime_settings
from middlewares.totp_auth import require_2fa
from utils.msg_cleaner import delete_after
from utils.env_manager import save_setting

tunnel_router = Router()


# ── FSM States ────────────────────────────────────────────────────────────────

class ExposeStates(StatesGroup):
    waiting_for_target   = State()
    waiting_for_auth     = State()
    waiting_for_username = State()
    waiting_for_password = State()

class PermExposeStates(StatesGroup):
    waiting_for_token    = State()
    waiting_for_hostname = State()
    waiting_for_target   = State()


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


def _tunnel_summary(tunnels: list[dict]) -> str:
    lines = []

    if runtime_settings.DUCKDNS_DOMAIN:
        duck = duckdns_service.get_status()
        domain = duck["domain"]
        auto = "🟢 Running" if duck["auto_update"] else "🔴 Stopped"
        lines.append(
            f"🦆 <b>DuckDNS Domain</b>\n"
            f"  ↳ <code>{domain}</code>\n"
            f"  ↳ <b>IP:</b> <code>{duck['last_ip']}</code>\n"
            f"  ↳ <b>Updated:</b> <code>{duck['last_updated']}</code>\n"
            f"  ↳ <b>Auto-update:</b> {auto}\n"
            f"  ↳ <i>Direct access (needs router port forward):\n"
            f"     http://{domain}:&lt;port&gt;</i>\n"
        )
    else:
        lines.append("🦆 <b>DuckDNS:</b> Not configured — use /duckdns to set up\n")

    if not tunnels:
        lines.append(
            "📭 <b>No active cloudflared tunnels.</b>\n\n"
            "• /expose — temporary URL (no account)\n"
            "• /expose_perm — permanent URL (free CF account)"
        )
    else:
        lines.append(f"🌐 <b>Active Tunnels ({len(tunnels)})</b>\n")
        for t in tunnels:
            auth_badge = f"🔒 <code>{t['auth_user']}</code>" if t["auth"] else "🔓 Open"
            perm_badge = "♾️ Permanent" if t.get("permanent") else "⏱ Temporary"
            lines.append(
                f"<b>ID:</b> <code>{t['id']}</code>  {perm_badge}\n"
                f"  ↳ <b>Local:</b>  <code>{t['target']}</code>\n"
                f"  ↳ <b>Public:</b> <code>{t['public_url']}</code>\n"
                f"  ↳ <b>Auth:</b>   {auth_badge}\n"
                f"  ↳ <b>Since:</b>  <code>{t['created_at']}</code>\n"
            )
    return "\n".join(lines)


# ── /expose_setup ─────────────────────────────────────────────────────────────

@tunnel_router.message(Command("expose_setup"))
@require_2fa
async def cmd_expose_setup(message: Message):
    installed = await tunnel_engine.is_installed()
    if installed:
        reply = await message.answer(
            "✅ <b>cloudflared is already installed.</b>\n\nUse /expose or /expose_perm.",
            parse_mode="HTML"
        )
        await delete_after(reply, delay=15)
        return

    status = await message.answer(
        "📥 <b>Installing cloudflared on WSL host...</b>\n"
        "<i>May take 30 seconds.</i>",
        parse_mode="HTML"
    )
    success, output = await tunnel_engine.install()
    trimmed = output[-1500:] if len(output) > 1500 else output

    if success:
        await status.edit_text(
            f"✅ <b>cloudflared Installed</b>\n\n<pre>{trimmed}</pre>\n\n"
            "• /expose — temporary URL\n"
            "• /expose_perm — permanent URL with your domain",
            parse_mode="HTML"
        )
    else:
        await status.edit_text(
            f"❌ <b>Installation Failed</b>\n\n<pre>{trimmed}</pre>",
            parse_mode="HTML"
        )


# ── /expose (temporary) ───────────────────────────────────────────────────────

@tunnel_router.message(Command("expose"))
@require_2fa
async def cmd_expose_start(message: Message, state: FSMContext):
    installed = await tunnel_engine.is_installed()
    if not installed:
        status = await message.answer(
            "📥 <b>First-time setup: Installing cloudflared...</b>\n"
            "<i>Runs once automatically. ~30 seconds.</i>",
            parse_mode="HTML"
        )
        success, output = await tunnel_engine.install()
        if not success:
            await status.edit_text(
                f"❌ <b>Auto-install failed</b>\n\n<pre>{output[-1000:]}</pre>\n\n"
                "Run /expose_setup to retry.",
                parse_mode="HTML"
            )
            return
        await status.edit_text("✅ <b>cloudflared installed.</b>", parse_mode="HTML")
        await delete_after(status, delay=4)

    await state.set_state(ExposeStates.waiting_for_target)
    await message.answer(
        "🌐 <b>Temporary Tunnel — Step 1/4</b>\n\n"
        "Enter the <b>local port or URL</b> to expose:\n\n"
        "• <code>8080</code>\n"
        "• <code>3000</code>\n"
        "• <code>http://localhost:8181</code>\n\n"
        "<i>⏱ URL will be temporary (random, changes on restart)</i>\n"
        "<i>For permanent URL use /expose_perm</i>",
        parse_mode="HTML"
    )


@tunnel_router.message(ExposeStates.waiting_for_target)
async def expose_got_target(message: Message, state: FSMContext):
    target = _normalise_target(message.text)
    status_msg = await message.answer(
        f"🔍 <b>Step 2/4 — Health Check</b>\n\nPinging <code>{target}</code>...",
        parse_mode="HTML"
    )
    reachable, info = await tunnel_engine.ping_target(target)
    if not reachable:
        await state.clear()
        await status_msg.edit_text(
            f"❌ <b>Health Check Failed</b>\n\n"
            f"<code>{target}</code> is unreachable.\n"
            f"<b>Reason:</b> <code>{info}</code>\n\n"
            "Start your service first, then /expose again.",
            parse_mode="HTML"
        )
        return
    await status_msg.edit_text(
        f"✅ <b>Health Check Passed</b>  ({info})\n<code>{target}</code> is reachable.",
        parse_mode="HTML"
    )
    await state.update_data(target=target)
    await state.set_state(ExposeStates.waiting_for_auth)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔒 Yes, protect it", callback_data="tun_auth:yes"),
        InlineKeyboardButton(text="🔓 No, open access", callback_data="tun_auth:no"),
    ]])
    await message.answer(
        "🔐 <b>Step 3/4 — HTTP Basic Auth</b>\n\n"
        "Protect this tunnel with username and password?",
        parse_mode="HTML", reply_markup=keyboard
    )


@tunnel_router.callback_query(F.data.startswith("tun_auth:"))
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
        await _launch_temp_tunnel(call.message, data["target"], "", "")


@tunnel_router.message(ExposeStates.waiting_for_auth)
async def expose_auth_text(message: Message, state: FSMContext):
    choice = message.text.strip().lower()
    if choice in ("yes", "y"):
        await state.update_data(wants_auth=True)
        await state.set_state(ExposeStates.waiting_for_username)
        await message.answer("👤 <b>Step 3a — Enter Username</b>", parse_mode="HTML")
    elif choice in ("no", "n"):
        data = await state.get_data()
        await state.clear()
        await _launch_temp_tunnel(message, data["target"], "", "")
    else:
        await message.answer("⚠️ Tap a button or reply <b>yes</b> / <b>no</b>.", parse_mode="HTML")


@tunnel_router.message(ExposeStates.waiting_for_username)
async def expose_got_username(message: Message, state: FSMContext):
    username = message.text.strip()
    if not username or ":" in username:
        await message.answer("⚠️ Username cannot be empty or contain <code>:</code>", parse_mode="HTML")
        return
    await state.update_data(auth_user=username)
    await state.set_state(ExposeStates.waiting_for_password)
    await message.answer(
        "🔑 <b>Step 3b — Enter Password</b>\n\n<i>Deleted immediately after reading.</i>",
        parse_mode="HTML"
    )


@tunnel_router.message(ExposeStates.waiting_for_password)
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
    await _launch_temp_tunnel(message, data["target"], data["auth_user"], password)


async def _launch_temp_tunnel(message: Message, target: str, auth_user: str, auth_pass: str):
    has_auth = bool(auth_user and auth_pass)
    status_msg = await message.answer(
        f"🚀 <b>Step 4/4 — Launching Tunnel</b>\n\n"
        f"<code>{target}</code>\n"
        + ("🔒 Setting up auth proxy...\n" if has_auth else "")
        + "<i>Up to 40 seconds...</i>",
        parse_mode="HTML"
    )
    success, result, tunnel_id = await tunnel_engine.create_tunnel(
        target, auth_user=auth_user, auth_pass=auth_pass
    )
    if not success:
        await status_msg.edit_text(
            f"❌ <b>Tunnel Failed</b>\n\n<code>{result}</code>",
            parse_mode="HTML"
        )
        return

    record = tunnel_engine.get_tunnel(tunnel_id)
    auth_ok = record and record.get("auth", False)
    auth_line = (
        f"🔒 <b>Auth:</b> <code>{auth_user}</code> — browser will prompt for password\n"
        if auth_ok else
        ("⚠️ <b>Auth proxy failed</b> — tunnel is open. Retry /expose.\n"
         if has_auth else "🔓 <b>Auth:</b> Open access\n")
    )

    duck_line = ""
    if runtime_settings.DUCKDNS_DOMAIN:
        d = runtime_settings.DUCKDNS_DOMAIN
        if not d.endswith(".duckdns.org"):
            d = f"{d}.duckdns.org"
        duck_line = f"\n🦆 <b>DuckDNS:</b> <code>{d}</code> (needs router port forward for direct access)"

    await status_msg.edit_text(
        f"✅ <b>Temporary Tunnel Active</b>\n\n"
        f"🆔 <b>ID:</b> <code>{tunnel_id}</code>\n"
        f"🏠 <b>Local:</b> <code>{target}</code>\n"
        f"🌐 <b>Public URL:</b> <code>{result}</code>\n"
        f"⏱ <b>Note:</b> URL is <b>temporary</b> — changes on restart\n"
        f"{auth_line}"
        f"{duck_line}\n\n"
        f"💡 For a <b>permanent</b> URL, use /expose_perm\n"
        f"Use /revoke <code>{tunnel_id}</code> to stop it.",
        parse_mode="HTML"
    )


# ── /expose_perm (permanent tunnel) ──────────────────────────────────────────

@tunnel_router.message(Command("expose_perm"))
@require_2fa
async def cmd_expose_perm(message: Message, state: FSMContext):
    """
    Guide user to set up a permanent Cloudflare Tunnel using their account.
    Uses `cloudflared tunnel run --token <token>` — the remotely-managed approach.
    """
    # Check if a saved token exists
    saved_token = getattr(runtime_settings, "CF_TUNNEL_TOKEN", None)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 How to get a token", callback_data="cfperm:guide")],
        [InlineKeyboardButton(text="🔑 I have a token — enter it", callback_data="cfperm:token")],
    ])
    if saved_token:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="▶ Start saved tunnel", callback_data="cfperm:start")],
            [InlineKeyboardButton(text="🔑 Use different token", callback_data="cfperm:token")],
            [InlineKeyboardButton(text="📋 Setup guide", callback_data="cfperm:guide")],
        ])

    await message.answer(
        "♾️ <b>Permanent Tunnel Setup</b>\n\n"
        "Uses your free Cloudflare account to get a <b>fixed public URL</b> "
        "that never changes — even after restarts.\n\n"
        "No port forwarding. No router settings. Free forever.\n\n"
        + ("✅ <b>Saved token found.</b> You can start the tunnel directly.\n"
           if saved_token else
           "You'll need a <b>free Cloudflare account</b> to get started.\n"),
        parse_mode="HTML",
        reply_markup=keyboard
    )


@tunnel_router.callback_query(F.data == "cfperm:guide")
async def cb_cf_guide(call: CallbackQuery):
    await call.answer()
    await call.message.answer(
        "📋 <b>How to get a Cloudflare Tunnel token</b>\n\n"
        "<b>Step 1 — Create free account</b>\n"
        "Open <code>cloudflare.com</code> → Sign up (free)\n\n"
        "<b>Step 2 — Go to Zero Trust</b>\n"
        "Dashboard → <b>Zero Trust</b> (left sidebar)\n"
        "If first time: choose free plan, pick any team name\n\n"
        "<b>Step 3 — Create a tunnel</b>\n"
        "Zero Trust → <b>Networks</b> → <b>Tunnels</b>\n"
        "→ <b>Create a tunnel</b>\n"
        "→ Choose <b>Cloudflared</b> → Next\n"
        "→ Give it a name (e.g. <code>myserver</code>) → Save\n\n"
        "<b>Step 4 — Copy the token</b>\n"
        "On the install page, you'll see a command like:\n"
        "<code>cloudflared service install eyJhIjoiMTIz...</code>\n"
        "Copy the long token string after <code>install</code>\n\n"
        "<b>Step 5 — Add a public hostname</b>\n"
        "In tunnel settings → <b>Public Hostname</b> → Add\n"
        "• Subdomain: <code>myapp</code>\n"
        "• Domain: your domain (or <code>*.workers.dev</code>)\n"
        "• Service: <code>http://localhost:8080</code>\n\n"
        "<b>Step 6 — Come back here</b>\n"
        "Tap /expose_perm → <b>I have a token</b>\n"
        "Paste your token → tunnel starts permanently\n\n"
        "✅ Your URL will be <code>myapp.yourdomain.com</code> — permanent!",
        parse_mode="HTML"
    )


@tunnel_router.callback_query(F.data == "cfperm:token")
async def cb_cf_enter_token(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(PermExposeStates.waiting_for_token)
    await call.message.answer(
        "🔑 <b>Enter your Cloudflare Tunnel token</b>\n\n"
        "Paste the token from the Cloudflare Zero Trust dashboard.\n"
        "It looks like: <code>eyJhIjoiMTIzND...</code>\n\n"
        "<i>Message deleted immediately after reading.</i>",
        parse_mode="HTML"
    )


@tunnel_router.callback_query(F.data == "cfperm:start")
async def cb_cf_start_saved(call: CallbackQuery):
    await call.answer()
    token = getattr(runtime_settings, "CF_TUNNEL_TOKEN", None)
    if not token:
        await call.message.answer("⚠️ No saved token found. Tap 'I have a token' to enter one.")
        return
    await _start_perm_tunnel(call.message, token)


@tunnel_router.message(PermExposeStates.waiting_for_token)
async def perm_got_token(message: Message, state: FSMContext):
    token = message.text.strip()
    try:
        await message.delete()
    except Exception:
        pass
    if not token or len(token) < 20:
        await message.answer("⚠️ That doesn't look like a valid token. Try again.")
        return
    await state.clear()

    # Save token for future restarts
    save_setting("CF_TUNNEL_TOKEN", token)
    runtime_settings.__dict__["CF_TUNNEL_TOKEN"] = token

    await _start_perm_tunnel(message, token)


async def _start_perm_tunnel(message: Message, token: str):
    status = await message.answer(
        "🚀 <b>Starting permanent tunnel...</b>\n\n"
        "<i>Connecting to Cloudflare network...</i>",
        parse_mode="HTML"
    )

    tunnel_id = await asyncio.to_thread(
        _ssh_exec,
        f"pkill -f 'cloudflared.*tunnel.*run' 2>/dev/null; echo ok",
        10
    )

    # Start permanent tunnel — token contains all routing config set in CF dashboard
    start_cmd = (
        f'nohup cloudflared tunnel run --token {token} '
        f'--no-autoupdate > /tmp/cf_perm_tunnel.log 2>&1 & echo $!'
    )
    code, pid_out = await asyncio.to_thread(_ssh_exec, start_cmd, 15)

    if code != 0:
        await status.edit_text(
            f"❌ <b>Failed to start tunnel</b>\n\n<code>{pid_out}</code>",
            parse_mode="HTML"
        )
        return

    pid = pid_out.strip()

    # Wait a moment then check logs for confirmation
    await asyncio.sleep(4)
    _, log_out = await asyncio.to_thread(
        _ssh_exec, "tail -20 /tmp/cf_perm_tunnel.log 2>/dev/null", 10
    )

    # Store in tunnel registry
    from datetime import datetime, timezone
    tid = "perm01"
    tunnel_engine._tunnels[tid] = {
        "id":          tid,
        "target":      "cloudflare-managed",
        "public_url":  "see Cloudflare dashboard",
        "auth":        False,
        "auth_user":   "",
        "proxy_port":  None,
        "proxy_pid":   pid,
        "ssh":         None,
        "channel":     None,
        "created_at":  datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "permanent":   True,
    }

    # Check if tunnel connected
    connected = "registered" in log_out.lower() or "connected" in log_out.lower()
    status_icon = "✅" if connected else "⏳"

    await status.edit_text(
        f"{status_icon} <b>Permanent Tunnel {'Running' if connected else 'Starting'}</b>\n\n"
        f"<b>PID:</b> <code>{pid}</code>\n"
        f"<b>URL:</b> Defined in Cloudflare Dashboard\n"
        f"   Zero Trust → Networks → Tunnels → Public Hostnames\n\n"
        f"<b>Recent log:</b>\n<pre>{log_out[-800:]}</pre>\n\n"
        f"The tunnel runs permanently on your host.\n"
        f"Use /revoke_all to stop it.",
        parse_mode="HTML"
    )


# ── /tunnel_status ────────────────────────────────────────────────────────────

@tunnel_router.message(Command("tunnel_status"))
@require_2fa
async def cmd_tunnel_status(message: Message):
    tunnels = tunnel_engine.list_tunnels()
    await message.answer(_tunnel_summary(tunnels), parse_mode="HTML")


# ── /revoke_all ───────────────────────────────────────────────────────────────

@tunnel_router.message(Command("revoke_all"))
@require_2fa
async def cmd_revoke_all(message: Message):
    status = await message.answer("🔍 <b>Checking for tunnel processes...</b>", parse_mode="HTML")

    # Find all running cloudflared and proxy processes before killing
    _, cf_procs = await asyncio.to_thread(
        _ssh_exec,
        "ps aux | grep -E 'cloudflared|tele_auth_proxy' | grep -v grep 2>/dev/null",
        10
    )

    # Build a readable list of what we found
    found = []
    for line in cf_procs.strip().splitlines():
        parts = line.split()
        if len(parts) < 11:
            continue
        pid = parts[1]
        cmd_parts = parts[10:]
        cmd_preview = " ".join(cmd_parts)[:80]
        if "cloudflared" in cmd_preview:
            if "tunnel run" in cmd_preview or "--token" in cmd_preview:
                found.append(f"♾️ <code>PID {pid}</code> — permanent tunnel")
            elif "tunnel --url" in cmd_preview or "--url" in cmd_preview:
                # Extract the URL target from the command
                url_match = re.search(r'--url\s+(\S+)', cmd_preview)
                target = url_match.group(1) if url_match else "unknown"
                found.append(f"⏱ <code>PID {pid}</code> — temp tunnel → <code>{target}</code>")
            else:
                found.append(f"🌐 <code>PID {pid}</code> — <code>{cmd_preview[:60]}</code>")
        elif "tele_auth_proxy" in cmd_preview:
            found.append(f"🔒 <code>PID {pid}</code> — auth proxy")

    # Also report what the bot knows about in memory
    known = tunnel_engine.list_tunnels()
    known_lines = []
    for t in known:
        perm = "♾️" if t.get("permanent") else "⏱"
        known_lines.append(
            f"{perm} <code>{t['id']}</code> → <code>{t['target']}</code> "
            f"({'🔒' if t['auth'] else '🔓'})"
        )

    if not found and not known:
        await status.edit_text(
            "📭 <b>No tunnel processes found.</b>\n\nNothing to revoke.",
            parse_mode="HTML"
        )
        await delete_after(status, delay=10)
        return

    # Now kill everything
    await asyncio.to_thread(
        _ssh_exec,
        "pkill -f 'cloudflared tunnel' 2>/dev/null; "
        "pkill -f 'cloudflared.*run' 2>/dev/null; "
        "pkill -f 'tele_auth_proxy' 2>/dev/null; "
        "echo done",
        30
    )
    tunnel_engine._tunnels.clear()

    lines = ["🛑 <b>All Tunnels Revoked</b>\n"]

    if found:
        lines.append("<b>Processes killed on host:</b>")
        lines.extend(found)

    if known_lines:
        lines.append("\n<b>Bot registry cleared:</b>")
        lines.extend(known_lines)

    lines.append(f"\n✅ <b>{len(found)} process(es)</b> stopped, "
                 f"<b>{len(known)}</b> registry entries cleared.")

    await status.edit_text("\n".join(lines), parse_mode="HTML")
    await delete_after(status, delay=30)


# ── /revoke ───────────────────────────────────────────────────────────────────

@tunnel_router.message(Command("revoke"))
@require_2fa
async def cmd_revoke(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        tunnels = tunnel_engine.list_tunnels()
        if not tunnels:
            await message.answer(
                "📭 No active tunnels.\n\n<b>Usage:</b> <code>/revoke &lt;id&gt;</code>",
                parse_mode="HTML"
            )
            return
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


@tunnel_router.callback_query(F.data.startswith("revoke:"))
async def callback_revoke(call: CallbackQuery):
    tunnel_id = call.data.split("revoke:", 1)[1]
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer()
    await _do_revoke(call.message, tunnel_id)


async def _do_revoke(message: Message, query: str):
    record = tunnel_engine.get_tunnel(query) or tunnel_engine.find_by_url(query)
    if not record:
        reply = await message.answer(
            f"❌ No tunnel found matching <code>{query}</code>.", parse_mode="HTML"
        )
        await delete_after(reply, delay=10)
        return
    tunnel_id = record["id"]
    status_msg = await message.answer(f"🛑 Revoking <code>{tunnel_id}</code>...", parse_mode="HTML")

    # For permanent tunnels, use pkill
    if record.get("permanent"):
        await asyncio.to_thread(
            _ssh_exec, "pkill -f 'cloudflared.*tunnel.*run' 2>/dev/null; echo ok", 10
        )
        tunnel_engine._tunnels.pop(tunnel_id, None)
        await status_msg.edit_text("🛑 <b>Permanent tunnel stopped.</b>", parse_mode="HTML")
    else:
        success, info = await tunnel_engine.revoke_tunnel(tunnel_id)
        if success:
            await status_msg.edit_text(
                f"🛑 <b>Tunnel Revoked</b>\n\n<code>{info}</code> is now offline.",
                parse_mode="HTML"
            )
        else:
            await status_msg.edit_text(f"❌ <b>Revoke Failed:</b> {info}", parse_mode="HTML")

    await delete_after(status_msg, delay=15)
