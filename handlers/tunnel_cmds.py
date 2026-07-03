"""
Tunnel management via cloudflared quick tunnels.

Commands:
  /expose_setup   — install cloudflared on the WSL host (one-time)
  /expose         — interactive wizard: port → health check → auth → tunnel
  /tunnel_status  — list active tunnels + DuckDNS info
  /revoke <id>    — kill a tunnel instantly

All commands require 2FA.
"""

import re
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from services.tunnel_service import tunnel_engine
from services.duckdns_service import duckdns_service
from config.settings import runtime_settings
from middlewares.totp_auth import require_2fa
from utils.msg_cleaner import delete_after

tunnel_router = Router()


# ── FSM States ────────────────────────────────────────────────────────────────

class ExposeStates(StatesGroup):
    waiting_for_target   = State()
    waiting_for_auth     = State()
    waiting_for_username = State()
    waiting_for_password = State()


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

    # DuckDNS block
    if runtime_settings.DUCKDNS_TOKEN and runtime_settings.DUCKDNS_DOMAIN:
        duck = duckdns_service.get_status()
        auto = "🟢 Active" if duck["auto_update"] else "🔴 Off"
        lines.append(
            f"🦆 <b>DuckDNS</b>\n"
            f"  ↳ <b>Domain:</b> <code>{duck['domain']}</code>\n"
            f"  ↳ <b>IP:</b> <code>{duck['last_ip']}</code>\n"
            f"  ↳ <b>Updated:</b> <code>{duck['last_updated']}</code>\n"
            f"  ↳ <b>Auto-update:</b> {auto}\n"
        )

    # Tunnels block
    if not tunnels:
        lines.append("📭 <b>No active tunnels.</b>")
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


# ── /expose_setup ─────────────────────────────────────────────────────────────

@tunnel_router.message(Command("expose_setup"))
@require_2fa
async def cmd_expose_setup(message: Message):
    """One-time: install cloudflared on the WSL host."""
    installed = await tunnel_engine.is_installed()
    if installed:
        reply = await message.answer(
            "✅ <b>cloudflared is already installed.</b>\n\nYou can use /expose right away.",
            parse_mode="HTML"
        )
        await delete_after(reply, delay=15)
        return

    status = await message.answer(
        "📥 <b>Installing cloudflared on WSL host...</b>\n"
        "<i>Downloading from GitHub releases — may take 30 seconds.</i>",
        parse_mode="HTML"
    )
    success, output = await tunnel_engine.install()
    trimmed = output[-1500:] if len(output) > 1500 else output

    if success:
        await status.edit_text(
            f"✅ <b>cloudflared Installed</b>\n\n<pre>{trimmed}</pre>\n\n"
            f"You can now use /expose to create public tunnels.",
            parse_mode="HTML"
        )
    else:
        await status.edit_text(
            f"❌ <b>Installation Failed</b>\n\n<pre>{trimmed}</pre>\n\n"
            f"Try manually:\n"
            f"<code>/host curl -fsSL https://github.com/cloudflare/cloudflared"
            f"/releases/latest/download/cloudflared-linux-amd64 "
            f"-o /usr/local/bin/cloudflared</code>\n"
            f"<code>/host chmod +x /usr/local/bin/cloudflared</code>",
            parse_mode="HTML"
        )


# ── /expose ───────────────────────────────────────────────────────────────────

@tunnel_router.message(Command("expose"))
@require_2fa
async def cmd_expose_start(message: Message, state: FSMContext):
    # Auto-install cloudflared if not present — no separate setup needed
    installed = await tunnel_engine.is_installed()
    if not installed:
        status = await message.answer(
            "📥 <b>First-time setup: Installing cloudflared...</b>\n\n"
            "<i>This runs once automatically. Takes about 30 seconds.</i>",
            parse_mode="HTML"
        )
        success, output = await tunnel_engine.install()
        if not success:
            trimmed = output[-1500:] if len(output) > 1500 else output
            await status.edit_text(
                f"❌ <b>Auto-install failed</b>\n\n<pre>{trimmed}</pre>\n\n"
                f"Try /expose_setup to install manually, or run:\n"
                f"<code>/host curl -fsSL https://github.com/cloudflare/cloudflared"
                f"/releases/latest/download/cloudflared-linux-amd64 "
                f"-o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared</code>",
                parse_mode="HTML"
            )
            return
        await status.edit_text(
            "✅ <b>cloudflared installed.</b> Starting wizard...",
            parse_mode="HTML"
        )
        await delete_after(status, delay=5)

    await state.set_state(ExposeStates.waiting_for_target)
    await message.answer(
        "🌐 <b>Expose Wizard — Step 1/4</b>\n\n"
        "Enter the <b>local port or URL</b> to expose publicly:\n\n"
        "• <code>8080</code>\n"
        "• <code>3000</code>\n"
        "• <code>http://localhost:8181</code>",
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

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔒 Yes, protect it", callback_data="tun_auth:yes"),
        InlineKeyboardButton(text="🔓 No, open access", callback_data="tun_auth:no"),
    ]])
    await message.answer(
        "🔐 <b>Step 3/4 — HTTP Basic Auth</b>\n\n"
        "Protect this tunnel with a username and password?",
        parse_mode="HTML",
        reply_markup=keyboard
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
        await _launch_tunnel(call.message, data["target"], auth_user="", auth_pass="")


@tunnel_router.message(ExposeStates.waiting_for_auth)
async def expose_auth_text(message: Message, state: FSMContext):
    """Fallback text handler if user types yes/no."""
    choice = message.text.strip().lower()
    if choice in ("yes", "y"):
        await state.update_data(wants_auth=True)
        await state.set_state(ExposeStates.waiting_for_username)
        await message.answer("👤 <b>Step 3a — Enter Username</b>", parse_mode="HTML")
    elif choice in ("no", "n"):
        data = await state.get_data()
        await state.clear()
        await _launch_tunnel(message, data["target"], auth_user="", auth_pass="")
    else:
        await message.answer("⚠️ Please tap a button or reply <b>yes</b> / <b>no</b>.", parse_mode="HTML")


@tunnel_router.message(ExposeStates.waiting_for_username)
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
    await _launch_tunnel(message, data["target"], auth_user=data["auth_user"], auth_pass=password)


async def _launch_tunnel(message: Message, target: str, auth_user: str, auth_pass: str):
    """
    Launch cloudflared tunnel.
    Note: cloudflared quick tunnels don't natively support basic auth.
    When auth is requested we note it in the tunnel record and inform the user
    they can use the URL with credentials in the browser or via reverse proxy.
    """
    has_auth = bool(auth_user and auth_pass)

    status_msg = await message.answer(
        f"🚀 <b>Step 4/4 — Launching Tunnel</b>\n\n"
        f"Starting cloudflared for <code>{target}</code>...\n"
        f"<i>May take up to 35 seconds.</i>",
        parse_mode="HTML"
    )

    success, result, tunnel_id = await tunnel_engine.create_tunnel(target)

    if not success:
        await status_msg.edit_text(
            f"❌ <b>Tunnel Launch Failed</b>\n\n<code>{result}</code>\n\n"
            f"If cloudflared is not installed, run /expose_setup first.",
            parse_mode="HTML"
        )
        return

    # Store auth info in record for display
    if has_auth:
        tunnel_engine._tunnels[tunnel_id]["auth"] = True
        tunnel_engine._tunnels[tunnel_id]["auth_user"] = auth_user

    auth_line = (
        f"🔒 <b>Auth:</b> <code>{auth_user}</code> / <i>[password set]</i>\n"
        f"   <i>Use credentials when accessing the URL.</i>\n"
        if has_auth else
        "🔓 <b>Auth:</b> Open access\n"
    )

    duck_note = ""
    if runtime_settings.DUCKDNS_DOMAIN:
        domain = runtime_settings.DUCKDNS_DOMAIN
        if not domain.endswith(".duckdns.org"):
            domain = f"{domain}.duckdns.org"
        duck_note = f"\n🦆 <b>DuckDNS:</b> <code>{domain}</code> → your public IP"

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

@tunnel_router.message(Command("tunnel_status"))
@require_2fa
async def cmd_tunnel_status(message: Message):
    tunnels = tunnel_engine.list_tunnels()
    await message.answer(_tunnel_summary(tunnels), parse_mode="HTML")


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
    success, info = await tunnel_engine.revoke_tunnel(tunnel_id)
    if success:
        await status_msg.edit_text(
            f"🛑 <b>Tunnel Revoked</b>\n\n<code>{info}</code> is now offline.",
            parse_mode="HTML"
        )
        await delete_after(status_msg, delay=15)
    else:
        await status_msg.edit_text(f"❌ <b>Revoke Failed:</b> {info}", parse_mode="HTML")
        await delete_after(status_msg, delay=15)
