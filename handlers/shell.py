import html
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message, BufferedInputFile
from services.shell_service import shell_engine, TELEGRAM_MSG_LIMIT
from middlewares.totp_auth import require_2fa
from utils.msg_cleaner import delete_command, delete_after

shell_router = Router()

# Max raw output bytes to send as a file (avoid encoding 100MB strings)
_MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB


@shell_router.message(Command("shell"))
@require_2fa
async def cmd_shell_execute(message: Message):
    command_body = message.text.replace("/shell", "").strip()
    if not command_body:
        reply = await message.answer(
            "⚠️ Provide a command. Example: <code>/shell ls -la</code>",
            parse_mode="HTML"
        )
        await delete_after(reply, delay=8)
        return

    await delete_command(message)
    working_msg = await message.answer(
        "⚡ <i>Executing...</i>", parse_mode="HTML"
    )

    code, output = await shell_engine.execute_cmd(command_body)

    # Escape HTML special chars so <, >, & in output don't break rendering
    safe_output = html.escape(output)

    header = f"<b>Exit:</b> <code>{code}</code>  <b>$</b> <code>{html.escape(command_body)}</code>\n\n"
    full_text = header + f"<pre>{safe_output}</pre>"

    if len(full_text) <= 4096:
        # Fits in one message
        await working_msg.edit_text(full_text, parse_mode="HTML")
        await delete_after(working_msg, delay=300)
    else:
        # Too long for a message — show last TELEGRAM_MSG_LIMIT chars inline
        # AND send full output as a file
        tail = safe_output[-TELEGRAM_MSG_LIMIT:]
        # Make sure we don't cut in the middle of a line
        if '\n' in tail:
            tail = tail[tail.index('\n') + 1:]

        truncated_text = (
            header
            + f"<i>⚠️ Output truncated — showing last {TELEGRAM_MSG_LIMIT} chars. Full output attached as file.</i>\n\n"
            + f"<pre>{tail}</pre>"
        )

        # Cap file size before encoding to avoid memory issues
        raw_bytes = output.encode('utf-8', errors='replace')
        if len(raw_bytes) > _MAX_FILE_BYTES:
            raw_bytes = raw_bytes[:_MAX_FILE_BYTES]
            raw_bytes += b"\n\n[OUTPUT CAPPED AT 10MB]"

        safe_filename = "output.txt"
        doc = BufferedInputFile(raw_bytes, filename=safe_filename)

        await working_msg.edit_text(truncated_text, parse_mode="HTML")
        await message.answer_document(
            doc,
            caption=f"📄 Full output — exit code: {code}"
        )
        await delete_after(working_msg, delay=300)
