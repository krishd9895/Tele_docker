from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message, BufferedInputFile
from services.shell_service import shell_engine
from middlewares.totp_auth import require_2fa
from utils.msg_cleaner import delete_command, delete_after

shell_router = Router()

@shell_router.message(Command("shell"))
@require_2fa
async def cmd_shell_execute(message: Message):
    command_body = message.text.replace("/shell", "").strip()
    if not command_body:
        reply = await message.answer("⚠️ Extraction syntax error. Provide terminal argument arrays.")
        await delete_after(reply, delay=8)
        return

    await delete_command(message)
    working_msg = await message.answer("⚡ <i>Executing command sequence...</i>", parse_mode="HTML")
    code, output = await shell_engine.execute_cmd(command_body)
    formatted_reply = f"<b>Exit Code:</b> <code>{code}</code>\n\n<b>Terminal Response:</b>\n<pre>{output}</pre>"

    if len(formatted_reply) > 4096:
        doc_data = BufferedInputFile(output.encode('utf-8'), filename="terminal_output.log")
        await message.answer_document(doc_data, caption=f"Exit Code: {code} (Output Truncated)")
        await working_msg.delete()
    else:
        await working_msg.edit_text(formatted_reply, parse_mode="HTML")
        # Shell output kept for 5 minutes — useful context
        await delete_after(working_msg, delay=300)