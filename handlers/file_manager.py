import os
import asyncio
import paramiko
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from services.shell_service import shell_engine
from utils.msg_cleaner import delete_command, delete_after, auto_clean

file_router = Router()

DEFAULT_UPLOAD_DIR = "/app/data/uploads"
BROWSER_ROOT = "/home/d"
PAGE_SIZE = 8

# ── Path registry ─────────────────────────────────────────────────────────────
# Telegram callback_data limit is 64 bytes. We store full paths here and use
# short integer keys in the callback_data instead.
_path_registry: dict[int, str] = {}
_path_registry_rev: dict[str, int] = {}
_path_counter = 0

def _reg(path: str) -> int:
    """Register a path and return its short integer key."""
    global _path_counter
    if path in _path_registry_rev:
        return _path_registry_rev[path]
    _path_counter += 1
    _path_registry[_path_counter] = path
    _path_registry_rev[path] = _path_counter
    return _path_counter

def _lookup(key: int) -> str | None:
    return _path_registry.get(key)


# ── FSM ───────────────────────────────────────────────────────────────────────

class UploadStates(StatesGroup):
    browsing         = State()
    waiting_for_file = State()


# ── SSH helpers ───────────────────────────────────────────────────────────────

def _list_host_dirs(path: str) -> list[str]:
    """Return sorted list of subdirectory full paths via SSH."""
    ssh_user = os.getenv("HOST_SSH_USER")
    ssh_pass = os.getenv("HOST_SSH_PASSWORD")
    if not ssh_user or not ssh_pass:
        return []
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect("127.0.0.1", username=ssh_user, password=ssh_pass, timeout=10)
        _, stdout, _ = ssh.exec_command(
            f'find "{path}" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | sort',
            timeout=15
        )
        stdout.channel.recv_exit_status()
        return [
            line.strip() for line in stdout.read().decode(errors="ignore").splitlines()
            if line.strip() and not line.strip().split("/")[-1].startswith(".")
        ]
    except Exception:
        return []
    finally:
        ssh.close()


# ── Keyboard builders ─────────────────────────────────────────────────────────

def _browser_keyboard(
    current_path: str,
    subdirs: list[str],
    page: int,
    pending_key: str = "0",   # "0" means no pending file
) -> InlineKeyboardMarkup:
    buttons = []
    start = page * PAGE_SIZE
    page_dirs = subdirs[start : start + PAGE_SIZE]

    # Folder buttons — use registry key, not full path
    for full_path in page_dirs:
        name = full_path.rstrip("/").split("/")[-1]
        key = _reg(full_path)
        # callback: fb:e:<key>:<page>:<pending_key>  — all short integers
        buttons.append([InlineKeyboardButton(
            text=f"📁 {name}",
            callback_data=f"fb:e:{key}:{page}:{pending_key}"
        )])

    # Pagination
    nav = []
    cur_key = _reg(current_path)
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀ Prev", callback_data=f"fb:p:{cur_key}:{page-1}:{pending_key}"))
    if start + PAGE_SIZE < len(subdirs):
        nav.append(InlineKeyboardButton(text="Next ▶", callback_data=f"fb:p:{cur_key}:{page+1}:{pending_key}"))
    if nav:
        buttons.append(nav)

    # Up + confirm row
    action = []
    parent = str(os.path.dirname(current_path.rstrip("/")))
    if current_path.rstrip("/") != BROWSER_ROOT.rstrip("/") and parent:
        pk = _reg(parent)
        action.append(InlineKeyboardButton(text="⬆️ Up", callback_data=f"fb:e:{pk}:0:{pending_key}"))

    confirm_text = "📂 Upload here" if pending_key != "0" else "✅ Select folder"
    action.append(InlineKeyboardButton(
        text=confirm_text,
        callback_data=f"fb:c:{cur_key}:{pending_key}"
    ))
    buttons.append(action)

    buttons.append([InlineKeyboardButton(text="❌ Cancel", callback_data="fb:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _browser_text(path: str, subdirs: list[str], page: int) -> str:
    total = len(subdirs)
    start = page * PAGE_SIZE
    end = min(start + PAGE_SIZE, total)
    return (
        f"📂 <b>Folder Browser</b>\n\n"
        f"<b>Path:</b> <code>{path}</code>\n"
        f"<b>Subfolders:</b> {total}"
        + (f"  <i>(page {page+1}, showing {start+1}–{end})</i>" if total > PAGE_SIZE else "")
        + "\n\nTap a folder to enter it, or tap <b>✅ Select folder</b> / <b>📂 Upload here</b>."
    )


# ── /ls ───────────────────────────────────────────────────────────────────────

@file_router.message(Command("ls"))
async def cmd_ls(message: Message):
    import html as _html
    await delete_command(message)
    code, output = await shell_engine.execute_cmd("ls -la")
    safe_output = _html.escape(output[:3800] if len(output) > 3800 else output)
    reply = await message.answer(f"📁 <b>Directory Indexing:</b>\n<pre>{safe_output}</pre>", parse_mode="HTML")
    await delete_after(reply, delay=30)


# ── /cat ──────────────────────────────────────────────────────────────────────

@file_router.message(Command("cat"))
async def cmd_cat(message: Message):
    import html as _html
    args = message.text.replace("/cat", "").strip()
    if not args:
        reply = await message.answer("⚠️ Usage: <code>/cat &lt;filename&gt;</code>", parse_mode="HTML")
        await auto_clean(message, reply, reply_delay=10)
        return
    await delete_command(message)
    code, output = await shell_engine.execute_cmd(f"cat {args}")
    # Cap output before any processing
    if len(output) > 512 * 1024:
        output = output[:512 * 1024] + "\n\n[OUTPUT CAPPED AT 512KB]"
    safe_output = _html.escape(output)
    if len(safe_output) > 4000:
        # Use a safe filename — strip path separators
        safe_name = os.path.basename(args.strip()) or "file_contents.txt"
        if not safe_name.endswith(".txt"):
            safe_name = safe_name + ".txt"
        doc = BufferedInputFile(output.encode('utf-8', errors='replace'), filename=safe_name)
        await message.answer_document(doc, caption=f"📄 Contents of {args}")
    else:
        reply = await message.answer(
            f"📄 <b>File Viewer ({_html.escape(args)}):</b>\n<pre>{safe_output}</pre>",
            parse_mode="HTML"
        )
        await delete_after(reply, delay=60)


# ── /download ─────────────────────────────────────────────────────────────────

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


# ── /upload ───────────────────────────────────────────────────────────────────

@file_router.message(Command("upload"))
async def cmd_upload_prompt(message: Message, state: FSMContext):
    """Open folder browser so user picks a destination first."""
    await delete_command(message)
    loading = await message.answer("📂 <b>Loading folder browser...</b>", parse_mode="HTML")
    dirs = await asyncio.to_thread(_list_host_dirs, BROWSER_ROOT)
    await state.set_state(UploadStates.browsing)
    await state.update_data(pending_file_id="", pending_file_name="")
    keyboard = _browser_keyboard(BROWSER_ROOT, dirs, page=0, pending_key="0")
    await loading.edit_text(
        _browser_text(BROWSER_ROOT, dirs, page=0),
        parse_mode="HTML",
        reply_markup=keyboard
    )


# ── Browser callbacks ─────────────────────────────────────────────────────────

@file_router.callback_query(F.data.startswith("fb:e:") | F.data.startswith("fb:p:"))
async def cb_browser_navigate(call: CallbackQuery):
    await call.answer()
    parts = call.data.split(":")
    # format: fb:e:<path_key>:<page>:<pending_key>
    try:
        path_key = int(parts[2])
        page = int(parts[3])
        pending_key = parts[4] if len(parts) > 4 else "0"
    except (IndexError, ValueError):
        return

    path = _lookup(path_key)
    if not path:
        await call.answer("Path expired, please restart /upload", show_alert=True)
        return

    dirs = await asyncio.to_thread(_list_host_dirs, path)
    keyboard = _browser_keyboard(path, dirs, page=page, pending_key=pending_key)
    try:
        await call.message.edit_text(
            _browser_text(path, dirs, page),
            parse_mode="HTML",
            reply_markup=keyboard
        )
    except Exception:
        pass


@file_router.callback_query(F.data.startswith("fb:c:"))
async def cb_browser_confirm(call: CallbackQuery, state: FSMContext):
    await call.answer()
    parts = call.data.split(":")
    # format: fb:c:<path_key>:<pending_key>
    try:
        path_key = int(parts[2])
        pending_key = parts[3] if len(parts) > 3 else "0"
    except (IndexError, ValueError):
        return

    dest_path = _lookup(path_key)
    if not dest_path:
        await call.answer("Path expired, please restart /upload", show_alert=True)
        return

    if pending_key != "0":
        # File already queued — upload now
        fsm_data = await state.get_data()
        file_name = fsm_data.get("pending_file_name", "upload")
        await state.clear()
        await call.message.edit_text(
            f"📂 <b>Destination:</b> <code>{dest_path}</code>\n"
            f"⏳ Uploading <code>{file_name}</code>...",
            parse_mode="HTML"
        )
        await _save_by_file_id(call.message, pending_key, file_name, dest_path, call.message.bot)
    else:
        # No file yet — store path and ask for file
        await state.update_data(chosen_dir=dest_path)
        await state.set_state(UploadStates.waiting_for_file)
        await call.message.edit_text(
            f"✅ <b>Destination set:</b> <code>{dest_path}</code>\n\n"
            f"Now send the file you want to upload.",
            parse_mode="HTML"
        )


@file_router.callback_query(F.data == "fb:cancel")
async def cb_browser_cancel(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await call.message.edit_text("❌ Upload cancelled.")
    await delete_after(call.message, delay=5)


# ── Receive file after folder was chosen ──────────────────────────────────────

@file_router.message(
    UploadStates.waiting_for_file,
    F.document | F.photo | F.video | F.audio | F.voice
)
async def cmd_receive_file_after_browse(message: Message, state: FSMContext):
    fsm_data = await state.get_data()
    dest_dir = fsm_data.get("chosen_dir", DEFAULT_UPLOAD_DIR)
    await state.clear()
    await _handle_upload(message, override_dest=dest_dir)


# ── Receive file with no prior /upload (caption path or open browser) ─────────

@file_router.message(F.document | F.photo | F.video | F.audio | F.voice)
async def cmd_receive_file(message: Message, state: FSMContext):
    caption = (message.caption or "").strip()

    # Caption has explicit path → upload directly
    if caption and caption.startswith("/"):
        await _handle_upload(message)
        return

    # No path → open folder browser with file queued
    file_obj, original_name = _extract_file(message)
    if not file_obj:
        return

    file_id = file_obj.file_id
    # Register file_id as a short key (reuse path registry for simplicity)
    fid_key = str(_reg(file_id))

    loading = await message.answer("📂 <b>Pick a destination folder:</b>", parse_mode="HTML")
    dirs = await asyncio.to_thread(_list_host_dirs, BROWSER_ROOT)

    await state.set_state(UploadStates.browsing)
    await state.update_data(pending_file_id=file_id, pending_file_name=original_name)

    keyboard = _browser_keyboard(BROWSER_ROOT, dirs, page=0, pending_key=fid_key)
    await loading.edit_text(
        f"📄 <b>File queued:</b> <code>{original_name}</code>\n\n"
        + _browser_text(BROWSER_ROOT, dirs, page=0),
        parse_mode="HTML",
        reply_markup=keyboard
    )


# ── Core helpers ──────────────────────────────────────────────────────────────

def _extract_file(message: Message) -> tuple:
    if message.document:
        return message.document, message.document.file_name or "document"
    elif message.photo:
        f = message.photo[-1]
        return f, f"photo_{f.file_unique_id}.jpg"
    elif message.video:
        return message.video, message.video.file_name or f"video_{message.video.file_unique_id}.mp4"
    elif message.audio:
        return message.audio, message.audio.file_name or f"audio_{message.audio.file_unique_id}.mp3"
    elif message.voice:
        return message.voice, f"voice_{message.voice.file_unique_id}.ogg"
    return None, ""


async def _handle_upload(message: Message, override_dest: str = None):
    bot = message.bot
    file_obj, original_name = _extract_file(message)
    if not file_obj:
        return

    caption = (message.caption or "").strip()
    if override_dest:
        dest_dir = override_dest
    elif caption.startswith("/upload"):
        parts = caption.split(maxsplit=1)
        dest_dir = parts[1].strip() if len(parts) > 1 else DEFAULT_UPLOAD_DIR
    elif caption and caption.startswith("/"):
        dest_dir = caption
    else:
        dest_dir = DEFAULT_UPLOAD_DIR

    if not os.path.isabs(dest_dir):
        dest_dir = os.path.join(shell_engine.current_wd, dest_dir)

    status_msg = await message.answer(
        f"📥 <b>Uploading</b> <code>{original_name}</code>...\n📂 <code>{dest_dir}</code>",
        parse_mode="HTML"
    )
    try:
        tg_file = await bot.get_file(file_obj.file_id)
        file_bytes = (await bot.download_file(tg_file.file_path)).read()
        await _write_via_sftp(dest_dir, original_name, file_bytes, status_msg)
    except Exception as e:
        await status_msg.edit_text(f"❌ <b>Upload Failed</b>\n\n<code>{e}</code>", parse_mode="HTML")
        await delete_after(status_msg, delay=20)


async def _save_by_file_id(message: Message, file_id: str, file_name: str, dest_dir: str, bot):
    # file_id might be a registry key (int str) or actual Telegram file_id
    actual_id = _lookup(int(file_id)) if file_id.isdigit() else file_id
    if not actual_id:
        await message.answer("❌ File reference expired. Please send the file again.")
        return
    status_msg = await message.answer(
        f"📥 <b>Uploading</b> <code>{file_name}</code>...\n📂 <code>{dest_dir}</code>",
        parse_mode="HTML"
    )
    try:
        tg_file = await bot.get_file(actual_id)
        file_bytes = (await bot.download_file(tg_file.file_path)).read()
        await _write_via_sftp(dest_dir, file_name, file_bytes, status_msg)
    except Exception as e:
        await status_msg.edit_text(f"❌ <b>Upload Failed</b>\n\n<code>{e}</code>", parse_mode="HTML")
        await delete_after(status_msg, delay=20)


async def _write_via_sftp(dest_dir: str, file_name: str, file_bytes: bytes, status_msg: Message):
    """Write file to WSL host via SFTP. Falls back to container write if no SSH creds."""
    ssh_user = os.getenv("HOST_SSH_USER")
    ssh_pass = os.getenv("HOST_SSH_PASSWORD")

    if not ssh_user or not ssh_pass:
        await _write_local(dest_dir, file_name, file_bytes, status_msg)
        return

    def _do_sftp():
        import io
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect("127.0.0.1", username=ssh_user, password=ssh_pass, timeout=15)
        try:
            ssh.exec_command(f'mkdir -p "{dest_dir}"')[1].channel.recv_exit_status()
            sftp = ssh.open_sftp()
            final_name = file_name
            dest_path = f"{dest_dir.rstrip('/')}/{final_name}"
            try:
                sftp.stat(dest_path)
                base, ext = os.path.splitext(file_name)
                counter = 1
                while True:
                    final_name = f"{base}_{counter}{ext}"
                    dest_path = f"{dest_dir.rstrip('/')}/{final_name}"
                    try:
                        sftp.stat(dest_path)
                        counter += 1
                    except FileNotFoundError:
                        break
            except FileNotFoundError:
                pass
            sftp.putfo(io.BytesIO(file_bytes), dest_path)
            sftp.close()
            return dest_path
        finally:
            ssh.close()

    try:
        dest_path = await asyncio.to_thread(_do_sftp)
        size = len(file_bytes)
        size_str = f"{size} B" if size < 1024 else f"{size/1024:.1f} KB" if size < 1024**2 else f"{size/1024**2:.2f} MB"
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
            f"❌ <b>Permission Denied:</b> <code>{dest_dir}</code>", parse_mode="HTML"
        )
        await delete_after(status_msg, delay=20)
    except Exception as e:
        await status_msg.edit_text(f"❌ <b>Upload Failed</b>\n\n<code>{e}</code>", parse_mode="HTML")
        await delete_after(status_msg, delay=20)


async def _write_local(dest_dir: str, file_name: str, file_bytes: bytes, status_msg: Message):
    try:
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, file_name)
        if os.path.exists(dest_path):
            base, ext = os.path.splitext(file_name)
            i = 1
            while os.path.exists(dest_path):
                dest_path = os.path.join(dest_dir, f"{base}_{i}{ext}")
                i += 1
        with open(dest_path, "wb") as f:
            f.write(file_bytes)
        size = len(file_bytes)
        size_str = f"{size} B" if size < 1024 else f"{size/1024:.1f} KB" if size < 1024**2 else f"{size/1024**2:.2f} MB"
        await status_msg.edit_text(
            f"✅ <b>Upload Complete</b> (container)\n\n"
            f"📄 <code>{os.path.basename(dest_path)}</code>\n"
            f"📂 <code>{dest_path}</code>\n"
            f"📦 <code>{size_str}</code>",
            parse_mode="HTML"
        )
        await delete_after(status_msg, delay=30)
    except Exception as e:
        await status_msg.edit_text(f"❌ <b>Upload Failed</b>\n\n<code>{e}</code>", parse_mode="HTML")
        await delete_after(status_msg, delay=20)
