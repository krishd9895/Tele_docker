import os
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, BufferedInputFile
from services.shell_service import shell_engine
from utils.msg_cleaner import delete_command, delete_after, auto_clean

file_router = Router()

# Default folder inside the container where uploads land
DEFAULT_UPLOAD_DIR = "/app/data/uploads"


@file_router.message(Command("ls"))
async def cmd_ls(message: Message):
    await delete_command(message)
    code, output = await shell_engine.execute_cmd("ls -la")
    reply = await message.answer(f"📁 <b>Directory Indexing:</b>\n<pre>{output}</pre>", parse_mode="HTML")
    await delete_after(reply, delay=30)


@file_router.message(Command("cat"))
async def cmd_cat(message: Message):
    args = message.text.replace("/cat", "").strip()
    if not args:
        reply = await message.answer("⚠️ Usage: <code>/cat &lt;filename&gt;</code>", parse_mode="HTML")
        await auto_clean(message, reply, reply_delay=10)
        return
    await delete_command(message)
    code, output = await shell_engine.execute_cmd(f"cat {args}")
    if len(output) > 4000:
        doc = BufferedInputFile(output.encode('utf-8'), filename=args)
        await message.answer_document(doc, caption=f"📄 Contents of {args}")
    else:
        reply = await message.answer(f"📄 <b>File Viewer ({args}):</b>\n<pre>{output}</pre>", parse_mode="HTML")
        await delete_after(reply, delay=60)


@file_router.message(Command("download"))
async def cmd_download(message: Message):
    args = message.text.replace("/download", "").strip()
    if not args:
        reply = await message.answer("⚠️ Usage: <code>/download &lt;filepath&gt;</code>", parse_mode="HTML")
        await auto_clean(message, reply, reply_delay=10)
        return
    await delete_command(message)
    target_path = os.path.join(shell_engine.current_wd, args)
    if not os.path.exists(target_path) or os.path.isdir(target_path):
        reply = await message.answer("❌ Target file not found or path describes a folder array.")
        await delete_after(reply, delay=10)
        return
    try:
        with open(target_path, "rb") as f:
            file_data = f.read()
        doc = BufferedInputFile(file_data, filename=os.path.basename(target_path))
        await message.answer_document(doc, caption=f"📥 Download complete: {args}")
    except Exception as e:
        reply = await message.answer(f"❌ System read failure: {e}")
        await delete_after(reply, delay=15)


# ── /upload ────────────────────────────────────────────────────────────────────
#
# Two ways to use:
#   1. Send /upload alone  → bot shows instructions, then send a file
#   2. Send a file with caption:
#        - caption = "/app/data/workspaces/myproject"  → saves there
#        - caption = "/upload /some/path"              → saves there
#        - no caption                                  → saves to DEFAULT_UPLOAD_DIR

@file_router.message(Command("upload"))
async def cmd_upload_prompt(message: Message):
    """User sent /upload with no file attached — show instructions."""
    await delete_command(message)
    reply = await message.answer(
        "📤 <b>Upload a File</b>\n\n"
        "Send any file to this chat. To choose the destination, "
        "set the file caption to the target path:\n\n"
        "• <code>/app/data/uploads</code>  <i>(default)</i>\n"
        "• <code>/app/data/workspaces/myproject</code>\n"
        "• <code>/home/youruser/configs</code>\n\n"
        "If no caption is provided the file lands in:\n"
        f"<code>{DEFAULT_UPLOAD_DIR}</code>",
        parse_mode="HTML"
    )
    await delete_after(reply, delay=30)


async def _handle_upload(message: Message):
    """
    Core upload handler — resolves the attached file, determines destination
    from caption, downloads from Telegram, and writes to disk.
    """
    bot = message.bot

    # Determine which Telegram file object is attached
    file_obj = None
    original_name = "upload"

    if message.document:
        file_obj = message.document
        original_name = message.document.file_name or "document"
    elif message.photo:
        # Telegram sends multiple photo sizes — take the largest
        file_obj = message.photo[-1]
        original_name = f"photo_{file_obj.file_unique_id}.jpg"
    elif message.video:
        file_obj = message.video
        original_name = message.video.file_name or f"video_{file_obj.file_unique_id}.mp4"
    elif message.audio:
        file_obj = message.audio
        original_name = message.audio.file_name or f"audio_{file_obj.file_unique_id}.mp3"
    elif message.voice:
        file_obj = message.voice
        original_name = f"voice_{file_obj.file_unique_id}.ogg"

    if not file_obj:
        return  # not a recognisable file type, skip silently

    # Parse destination folder from caption
    caption = (message.caption or "").strip()
    if caption.startswith("/upload"):
        parts = caption.split(maxsplit=1)
        dest_dir = parts[1].strip() if len(parts) > 1 else DEFAULT_UPLOAD_DIR
    elif caption and caption.startswith("/"):
        dest_dir = caption
    else:
        dest_dir = DEFAULT_UPLOAD_DIR

    # Resolve relative paths against shell current working dir
    if not os.path.isabs(dest_dir):
        dest_dir = os.path.join(shell_engine.current_wd, dest_dir)

    status_msg = await message.answer(
        f"📥 <b>Uploading</b> <code>{original_name}</code>...\n"
        f"📂 Destination: <code>{dest_dir}</code>",
        parse_mode="HTML"
    )

    try:
        os.makedirs(dest_dir, exist_ok=True)

        # Download bytes from Telegram
        tg_file = await bot.get_file(file_obj.file_id)
        file_bytes_io = await bot.download_file(tg_file.file_path)

        dest_path = os.path.join(dest_dir, original_name)

        # Avoid overwriting — add counter suffix if file exists
        if os.path.exists(dest_path):
            base, ext = os.path.splitext(original_name)
            counter = 1
            while os.path.exists(dest_path):
                dest_path = os.path.join(dest_dir, f"{base}_{counter}{ext}")
                counter += 1

        with open(dest_path, "wb") as f:
            f.write(file_bytes_io.read())

        size_bytes = os.path.getsize(dest_path)
        if size_bytes < 1024:
            size_str = f"{size_bytes} B"
        elif size_bytes < 1024 ** 2:
            size_str = f"{size_bytes/1024:.1f} KB"
        else:
            size_str = f"{size_bytes/1024**2:.2f} MB"

        await status_msg.edit_text(
            f"✅ <b>Upload Complete</b>\n\n"
            f"📄 <b>File:</b> <code>{os.path.basename(dest_path)}</code>\n"
            f"📂 <b>Saved to:</b> <code>{dest_path}</code>\n"
            f"📦 <b>Size:</b> <code>{size_str}</code>",
            parse_mode="HTML"
        )
        await delete_after(status_msg, delay=30)

    except PermissionError:
        await status_msg.edit_text(
            f"❌ <b>Permission Denied</b>\n\n"
            f"Cannot write to <code>{dest_dir}</code>.\n"
            "Try a path inside <code>/app/data/</code>.",
            parse_mode="HTML"
        )
        await delete_after(status_msg, delay=20)
    except Exception as e:
        await status_msg.edit_text(
            f"❌ <b>Upload Failed</b>\n\n<code>{e}</code>",
            parse_mode="HTML"
        )
        await delete_after(status_msg, delay=20)


# Catch any message containing a file — document, photo, video, audio, voice
@file_router.message(F.document | F.photo | F.video | F.audio | F.voice)
async def cmd_receive_file(message: Message):
    await _handle_upload(message)
