"""
Settings management commands.

/showenv        — view all settings (env + mutable store)
/setenv KEY val — update a mutable setting (saved to MongoDB or settings.json)

Sensitive keys (tokens, passwords) stay in .env only.
The .env file is read-only from the container — never written by the bot.
"""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from utils.env_manager import (
    get_all_settings, save_setting, apply_to_runtime, mask_value, _SENSITIVE_KEYS
)
from config.settings import runtime_settings
from middlewares.totp_auth import require_2fa
from utils.msg_cleaner import delete_command, delete_after, auto_clean

env_router = Router()


@env_router.message(Command("showenv"))
@require_2fa
async def cmd_showenv(message: Message):
    """Show current runtime settings — sensitive values masked."""
    await delete_command(message)

    # Combine: runtime settings (from .env) + mutable store (MongoDB/JSON)
    runtime_dict = runtime_settings.model_dump()
    mutable = get_all_settings()

    lines = [
        "📋 <b>Current Settings</b>\n"
        "<i>🔒 = from .env (read-only) | ☁️/📄 = from persistent store</i>\n"
    ]

    for key in sorted(runtime_dict.keys()):
        val = str(runtime_dict.get(key) or "")
        if not val or val == "None":
            val = ""

        in_store = key in mutable
        source = "☁️" if in_store else "🔒"
        display = mask_value(key, val) if val else "<i>not set</i>"
        lines.append(f"{source} <code>{key}</code> = <code>{display}</code>")

    # Show store-only keys not in runtime_settings
    for key, val in mutable.items():
        if key not in runtime_dict:
            lines.append(f"☁️ <code>{key}</code> = <code>{mask_value(key, str(val))}</code>")

    lines.append(
        "\n<b>Legend:</b> 🔒 read from .env  ☁️ saved in MongoDB  📄 saved in settings.json\n"
        "Use /setenv to change mutable settings."
    )

    reply = await message.answer("\n".join(lines), parse_mode="HTML")
    await delete_after(reply, delay=60)


@env_router.message(Command("setenv"))
@require_2fa
async def cmd_setenv(message: Message):
    """
    Update a mutable setting. Saved to MongoDB or settings.json.
    Applied to runtime immediately.

    Usage: /setenv KEY value
    Examples:
      /setenv GIT_SCAN_PATHS /home/d
      /setenv DUCKDNS_UPDATE_INTERVAL 600
      /setenv DEPLOYMENT_TIMEOUT 900
    """
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        reply = await message.answer(
            "⚠️ <b>Usage:</b> <code>/setenv KEY value</code>\n\n"
            "<i>Examples:</i>\n"
            "<code>/setenv GIT_SCAN_PATHS /home/d</code>\n"
            "<code>/setenv DUCKDNS_UPDATE_INTERVAL 600</code>\n"
            "<code>/setenv DEPLOYMENT_TIMEOUT 900</code>\n\n"
            "Use /showenv to see current values.\n\n"
            "⚠️ Sensitive keys (tokens, passwords) cannot be changed here.",
            parse_mode="HTML"
        )
        await auto_clean(message, reply, reply_delay=20)
        return

    key = args[1].strip().upper()
    value = args[2].strip()

    # Delete command immediately — may contain sensitive value
    await delete_command(message)

    if key in _SENSITIVE_KEYS:
        reply = await message.answer(
            f"🚫 <b>{key}</b> is a sensitive key.\n\n"
            "Sensitive settings stay in <code>.env</code> only "
            "and are never stored in the bot's database.\n"
            "Edit <code>.env</code> directly to change this value.",
            parse_mode="HTML"
        )
        await delete_after(reply, delay=20)
        return

    success, backend = save_setting(key, value)

    if success:
        # Apply to runtime immediately
        apply_to_runtime({key: value})
        backend_label = "☁️ MongoDB" if backend == "mongodb" else "📄 settings.json"
        reply = await message.answer(
            f"✅ <b>Setting saved</b> ({backend_label})\n\n"
            f"<code>{key}</code> = <code>{mask_value(key, value)}</code>\n\n"
            f"Applied to runtime immediately.\n"
            f"<i>No restart needed for most settings.</i>",
            parse_mode="HTML"
        )
        await delete_after(reply, delay=20)
    else:
        reply = await message.answer(
            f"❌ <b>Failed to save setting</b>\n\n"
            f"<code>{backend}</code>\n\n"
            "MongoDB not configured and settings.json write failed.\n"
            "Check that <code>data/</code> volume is mounted correctly.",
            parse_mode="HTML"
        )
        await delete_after(reply, delay=20)
