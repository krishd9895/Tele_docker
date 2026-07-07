import time
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from middlewares.totp_auth import (
    verify_code, open_session, has_valid_session, revoke_session,
    session_time_left, _session_duration_label,
)
from utils.msg_cleaner import delete_command, delete_after, auto_clean

auth_router = Router()


@auth_router.message(Command("verify"))
async def cmd_verify(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        reply = await message.answer(
            "⚠️ <b>Usage:</b> <code>/verify &lt;6-digit-code&gt;</code>\n\n"
            "Open Google Authenticator and enter the current code for <b>TeleDocker</b>.",
            parse_mode="HTML"
        )
        await auto_clean(message, reply, reply_delay=15)
        return

    code = args[1].strip()
    await delete_command(message)

    if verify_code(code):
        expiry = open_session(message.from_user.id)
        expiry_str = time.strftime("%H:%M:%S", time.localtime(expiry))
        duration = _session_duration_label()
        reply = await message.answer(
            f"✅ <b>2FA Verified</b>\n\n"
            f"Elevated access granted for <b>{duration}</b>.\n"
            f"Session expires at <code>{expiry_str}</code>.\n\n"
            f"Use /lock to revoke early.",
            parse_mode="HTML"
        )
        await delete_after(reply, delay=20)
    else:
        reply = await message.answer(
            "❌ <b>Invalid or expired code.</b>\n\n"
            "Make sure your device clock is synced and try again.\n"
            "Codes are valid for 30 seconds.",
            parse_mode="HTML"
        )
        await delete_after(reply, delay=15)


@auth_router.message(Command("lock"))
async def cmd_lock(message: Message):
    if not has_valid_session(message.from_user.id):
        reply = await message.answer("ℹ️ No active 2FA session to revoke.")
        await auto_clean(message, reply, reply_delay=8)
        return
    revoke_session(message.from_user.id)
    reply = await message.answer(
        "🔒 <b>Session Locked</b>\n\n"
        "Elevated access has been revoked. Use /verify to re-authenticate.",
        parse_mode="HTML"
    )
    await auto_clean(message, reply, reply_delay=10)


@auth_router.message(Command("2fa_status"))
async def cmd_2fa_status(message: Message):
    if has_valid_session(message.from_user.id):
        left = session_time_left(message.from_user.id)
        reply = await message.answer(
            f"🟢 <b>2FA Session Active</b>\n"
            f"Time remaining: <code>{left}</code>",
            parse_mode="HTML"
        )
    else:
        reply = await message.answer(
            "🔴 <b>No active 2FA session.</b>\n"
            "Use /verify to authenticate.",
            parse_mode="HTML"
        )
    await auto_clean(message, reply, reply_delay=10)
