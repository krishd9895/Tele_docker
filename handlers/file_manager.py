import os
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, BufferedInputFile
from services.shell_service import shell_engine

file_router = Router()

@file_router.message(Command("ls"))
async def cmd_ls(message: Message):
    code, output = await shell_engine.execute_cmd("ls -la")
    await message.answer(f"📁 <b>Directory Indexing:</b>\n<pre>{output}</pre>", parse_mode="HTML")

@file_router.message(Command("cat"))
async def cmd_cat(message: Message):
    args = message.text.replace("/cat", "").strip()
    if not args:
        await message.answer("⚠️ Usage: <code>/cat &lt;filename&gt;</code>", parse_mode="HTML")
        return
    code, output = await shell_engine.execute_cmd(f"cat {args}")
    if len(output) > 4000:
        doc = BufferedInputFile(output.encode('utf-8'), filename=args)
        await message.answer_document(doc, caption=f"📄 Contents of {args}")
    else:
        await message.answer(f"📄 <b>File Viewer ({args}):</b>\n<pre>{output}</pre>", parse_mode="HTML")

@file_router.message(Command("download"))
async def cmd_download(message: Message):
    args = message.text.replace("/download", "").strip()
    if not args:
        await message.answer("⚠️ Usage: <code>/download &lt;filepath&gt;</code>", parse_mode="HTML")
        return
    target_path = os.path.join(shell_engine.current_wd, args)
    if not os.path.exists(target_path) or os.path.isdir(target_path):
        await message.answer("❌ Target file not found or path describes a folder array.")
        return
    try:
        with open(target_path, "rb") as f:
            file_data = f.read()
        doc = BufferedInputFile(file_data, filename=os.path.basename(target_path))
        await message.answer_document(doc, caption=f"📥 Download complete: {args}")
    except Exception as e:
        await message.answer(f"❌ System read failure: {e}")