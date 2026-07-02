"""
Message auto-delete utility.

Usage
-----
from utils.msg_cleaner import delete_after, delete_command

# Delete a bot reply after 10 seconds
await delete_after(some_message, delay=10)

# Delete the user's command message immediately (or after delay)
await delete_command(message)

# Both together — clean up command + send a reply that disappears
reply = await message.answer("✅ Done")
await delete_command(message)
await delete_after(reply, delay=8)
"""

import asyncio
import logging
from aiogram.types import Message
from aiogram.exceptions import TelegramBadRequest

logger = logging.getLogger(__name__)


async def _safe_delete(msg: Message) -> None:
    """Delete a message, silently ignoring 'message not found' errors."""
    try:
        await msg.delete()
    except TelegramBadRequest as e:
        # Already deleted or too old (>48h) — not an error
        if "message to delete not found" in str(e).lower() or \
           "message can't be deleted" in str(e).lower():
            pass
        else:
            logger.debug(f"Could not delete message {msg.message_id}: {e}")
    except Exception as e:
        logger.debug(f"Could not delete message {msg.message_id}: {e}")


async def delete_after(msg: Message, delay: float = 10.0) -> None:
    """
    Schedule a message for deletion after `delay` seconds.
    Fire-and-forget — does not block the caller.
    """
    async def _task():
        await asyncio.sleep(delay)
        await _safe_delete(msg)

    asyncio.create_task(_task())


async def delete_command(msg: Message, delay: float = 0.0) -> None:
    """
    Delete the user's command message (the one that triggered the handler).
    Use delay=0 for immediate, or a small delay if you want the user to see
    what they typed for a moment.
    """
    if delay > 0:
        await delete_after(msg, delay)
    else:
        await _safe_delete(msg)


async def auto_clean(
    command_msg: Message,
    reply_msg: Message,
    reply_delay: float = 10.0,
    command_delay: float = 0.0,
) -> None:
    """
    Convenience: delete both the user command and the bot reply.

    command_msg  — the user's /command message
    reply_msg    — the bot's response message
    reply_delay  — seconds before deleting the bot reply (default 10s)
    command_delay — seconds before deleting the command (default: immediately)
    """
    await delete_command(command_msg, delay=command_delay)
    await delete_after(reply_msg, delay=reply_delay)
