"""
DuckDNS management commands.

/duckdns         — open the DuckDNS control panel
  ├ 🔍 Status    — show current domain, IP, last update
  ├ 🔄 Update IP — force push current IP to DuckDNS now
  ├ ⚙️ Configure — set DUCKDNS_TOKEN and DUCKDNS_DOMAIN interactively
  └ ▶ Start / ⏹ Stop auto-updater

All commands require 2FA.
"""

import asyncio
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from services.duckdns_service import duckdns_service
from config.settings import runtime_settings
from middlewares.totp_auth import require_2fa
from utils.msg_cleaner import delete_after, delete_command

duckdns_router = Router()


class DuckDNSSetupStates(StatesGroup):
    waiting_for_token  = State()
    waiting_for_domain = State()


# ── Panel keyboard ────────────────────────────────────────────────────────────

def _panel_keyboard() -> InlineKeyboardMarkup:
    status = duckdns_service.get_status()
    auto = status["auto_update"]
    auto_btn = "⏹ Stop Auto-Update" if auto else "▶ Start Auto-Update"
    auto_cb  = "dd:stop_auto" if auto else "dd:start_auto"

    configured = bool(runtime_settings.DUCKDNS_TOKEN and runtime_settings.DUCKDNS_DOMAIN)

    buttons = [
        [InlineKeyboardButton(text="🔍 Status",       callback_data="dd:status")],
        [InlineKeyboardButton(text="🔄 Update IP Now", callback_data="dd:update")],
        [InlineKeyboardButton(text=auto_btn,           callback_data=auto_cb)],
        [InlineKeyboardButton(text="⚙️ Configure Token & Domain", callback_data="dd:configure")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _panel_text() -> str:
    status = duckdns_service.get_status()
    configured = bool(runtime_settings.DUCKDNS_TOKEN and runtime_settings.DUCKDNS_DOMAIN)
    auto_badge = "🟢 Running" if status["auto_update"] else "🔴 Stopped"

    if not configured:
        return (
            "🦆 <b>DuckDNS Control Panel</b>\n\n"
            "⚠️ <b>Not configured yet.</b>\n\n"
            "Tap <b>⚙️ Configure</b> to set your token and domain.\n"
            "You need your DuckDNS token from <code>duckdns.org</code>."
        )

    return (
        f"🦆 <b>DuckDNS Control Panel</b>\n\n"
        f"<b>Domain:</b> <code>{status['domain']}</code>\n"
        f"<b>Current IP:</b> <code>{status['last_ip']}</code>\n"
        f"<b>Last Updated:</b> <code>{status['last_updated']}</code>\n"
        f"<b>Auto-Update:</b> {auto_badge}"
    )


# ── /duckdns ──────────────────────────────────────────────────────────────────

@duckdns_router.message(Command("duckdns"))
@require_2fa
async def cmd_duckdns(message: Message):
    await delete_command(message)
    await message.answer(
        _panel_text(),
        parse_mode="HTML",
        reply_markup=_panel_keyboard()
    )


# ── Status ────────────────────────────────────────────────────────────────────

@duckdns_router.callback_query(F.data == "dd:status")
async def cb_dd_status(call: CallbackQuery):
    await call.answer()
    status = duckdns_service.get_status()
    configured = bool(runtime_settings.DUCKDNS_TOKEN and runtime_settings.DUCKDNS_DOMAIN)

    if not configured:
        msg = await call.message.answer(
            "⚠️ DuckDNS not configured. Tap ⚙️ Configure to set it up.",
            parse_mode="HTML"
        )
        await delete_after(msg, delay=10)
        return

    # Also fetch live public IP for comparison
    live_ip = await duckdns_service.get_public_ip()

    msg = await call.message.answer(
        f"🦆 <b>DuckDNS Status</b>\n\n"
        f"<b>Domain:</b> <code>{status['domain']}</code>\n"
        f"<b>Registered IP:</b> <code>{status['last_ip'] or 'unknown (not updated yet)'}</code>\n"
        f"<b>Your Current IP:</b> <code>{live_ip or 'could not fetch'}</code>\n"
        f"<b>Last Updated:</b> <code>{status['last_updated']}</code>\n"
        f"<b>Auto-Update:</b> {'🟢 Running' if status['auto_update'] else '🔴 Stopped'}\n\n"
        + ("✅ IP is up to date." if live_ip == status['last_ip']
           else "⚠️ IP mismatch — tap 🔄 Update IP Now."),
        parse_mode="HTML"
    )
    await delete_after(msg, delay=30)


# ── Force update ──────────────────────────────────────────────────────────────

@duckdns_router.callback_query(F.data == "dd:update")
async def cb_dd_update(call: CallbackQuery):
    await call.answer()

    if not runtime_settings.DUCKDNS_TOKEN or not runtime_settings.DUCKDNS_DOMAIN:
        msg = await call.message.answer(
            "⚠️ DuckDNS not configured. Tap ⚙️ Configure first.",
            parse_mode="HTML"
        )
        await delete_after(msg, delay=10)
        return

    msg = await call.message.answer("🔄 <b>Updating DuckDNS...</b>", parse_mode="HTML")
    success, result = await duckdns_service.update_ip()

    if success:
        await msg.edit_text(
            f"✅ <b>DuckDNS Updated</b>\n\n"
            f"<b>Domain:</b> <code>{runtime_settings.DUCKDNS_DOMAIN}.duckdns.org</code>\n"
            f"<b>IP:</b> <code>{result}</code>",
            parse_mode="HTML"
        )
    else:
        await msg.edit_text(
            f"❌ <b>Update Failed</b>\n\n<code>{result}</code>",
            parse_mode="HTML"
        )
    await delete_after(msg, delay=20)

    # Refresh panel
    try:
        await call.message.edit_text(_panel_text(), parse_mode="HTML", reply_markup=_panel_keyboard())
    except Exception:
        pass


# ── Auto-updater controls ─────────────────────────────────────────────────────

@duckdns_router.callback_query(F.data == "dd:start_auto")
async def cb_dd_start_auto(call: CallbackQuery):
    await call.answer()
    if not runtime_settings.DUCKDNS_TOKEN or not runtime_settings.DUCKDNS_DOMAIN:
        msg = await call.message.answer("⚠️ Configure DuckDNS first.", parse_mode="HTML")
        await delete_after(msg, delay=8)
        return
    await duckdns_service.start_auto_updater(runtime_settings.DUCKDNS_UPDATE_INTERVAL)
    await call.message.edit_text(_panel_text(), parse_mode="HTML", reply_markup=_panel_keyboard())


@duckdns_router.callback_query(F.data == "dd:stop_auto")
async def cb_dd_stop_auto(call: CallbackQuery):
    await call.answer()
    if duckdns_service._task and not duckdns_service._task.done():
        duckdns_service._task.cancel()
        duckdns_service._task = None
    await call.message.edit_text(_panel_text(), parse_mode="HTML", reply_markup=_panel_keyboard())


# ── Configure ─────────────────────────────────────────────────────────────────

@duckdns_router.callback_query(F.data == "dd:configure")
async def cb_dd_configure(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(DuckDNSSetupStates.waiting_for_token)
    await call.message.answer(
        "⚙️ <b>DuckDNS Setup — Step 1/2</b>\n\n"
        "Go to <code>duckdns.org</code>, log in, and copy your token.\n\n"
        "Paste your <b>DuckDNS token</b> here:\n\n"
        "<i>Message deleted immediately after reading.</i>",
        parse_mode="HTML"
    )


@duckdns_router.message(DuckDNSSetupStates.waiting_for_token)
async def dd_got_token(message: Message, state: FSMContext):
    token = message.text.strip()
    try:
        await message.delete()
    except Exception:
        pass

    if not token or len(token) < 10:
        await message.answer("⚠️ That doesn't look like a valid token. Please try again.")
        return

    await state.update_data(token=token)
    await state.set_state(DuckDNSSetupStates.waiting_for_domain)
    await message.answer(
        "⚙️ <b>DuckDNS Setup — Step 2/2</b>\n\n"
        "Enter your <b>subdomain</b> (just the name, not the full domain):\n\n"
        "• If your domain is <code>kyone.duckdns.org</code>\n"
        "  enter: <code>kyone</code>",
        parse_mode="HTML"
    )


@duckdns_router.message(DuckDNSSetupStates.waiting_for_domain)
async def dd_got_domain(message: Message, state: FSMContext):
    domain = message.text.strip().lower().replace(".duckdns.org", "")
    await delete_command(message)

    if not domain or " " in domain:
        await message.answer("⚠️ Invalid domain name. Enter just the subdomain part.")
        return

    data = await state.get_data()
    token = data["token"]
    await state.clear()

    # Apply to runtime settings immediately (takes effect without rebuild)
    runtime_settings.DUCKDNS_TOKEN = token
    runtime_settings.DUCKDNS_DOMAIN = domain

    status = await message.answer(
        f"⏳ <b>Testing DuckDNS connection...</b>",
        parse_mode="HTML"
    )

    success, result = await duckdns_service.update_ip()

    if success:
        await status.edit_text(
            f"✅ <b>DuckDNS Configured Successfully</b>\n\n"
            f"<b>Domain:</b> <code>{domain}.duckdns.org</code>\n"
            f"<b>IP:</b> <code>{result}</code>\n\n"
            f"Auto-update is now running every "
            f"{runtime_settings.DUCKDNS_UPDATE_INTERVAL // 60} minutes.\n\n"
            f"⚠️ <b>Important:</b> Add these to your <code>.env</code> file to persist\n"
            f"after a bot restart:\n"
            f"<code>DUCKDNS_TOKEN={token[:6]}...{token[-4:]}</code>\n"
            f"<code>DUCKDNS_DOMAIN={domain}</code>",
            parse_mode="HTML"
        )
        # Start auto-updater with new credentials
        await duckdns_service.start_auto_updater(runtime_settings.DUCKDNS_UPDATE_INTERVAL)
        await delete_after(status, delay=60)
    else:
        await status.edit_text(
            f"❌ <b>DuckDNS Test Failed</b>\n\n"
            f"<code>{result}</code>\n\n"
            "Check your token is correct and try /duckdns → Configure again.",
            parse_mode="HTML"
        )
        await delete_after(status, delay=20)
