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

# Default upload destination (inside container)
DEFAULT_UPLOAD_DIR = "/app/data/uploads"

# Root path shown when browser opens — your WSL home
BROWSER_ROOT = "/home/d"

# Max folders to show per page (Telegram has button limits)
PAGE_SIZE = 10


# ── FSM ───────────────────────────────────────────────────────────────────────

class UploadStates(StatesGroup):
    browsing = State()   # user is navigating folders, file not yet sent
    waiting_for_file = State()   # folder chosen, waiting for file


# ── SSH helper ────────────────────────────────────────────────────────────────

def _list_host_dirs(path: str) -> tuple[int, list[str]]:
    """Return sorted list of subdirectory names in path on the WSL host via SSH."""
    ssh_user = os.getenv("HOST_SSH_USER")
    ssh_pass = os.getenv("HOST_SSH_PASSWORD")
    if not ssh_user or not ssh_pass:
        return -1, []
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect("127.0.0.1", username=ssh_user, password=ssh_pass, timeout=10)
        # List only directories, sorted, no hidden folders (skip dot-dirs for cleanliness)
        _, stdout, _ = ssh.exec_command(
            f'find "{path}" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | sort',
            timeout=15
        )
        stdout.channel.recv_exit_status()
        dirs = [
            line.strip() for line in stdout.read().decode(errors="ignore").splitlines()
            if line.strip()
        ]
        return 0, dirs
    except Exception:
        return -1, []
    finally:
        ssh.close()


# ── Keyboard builders ─────────────────────────────────────────────────────────

def _browser_keyboard(
    current_path: str,
    subdirs: list[str],
    page: int = 0,
    pending_file_id: str = "",
) -> InlineKeyboardMarkup:
    """
    Build the folder browser keyboard.
    pending_file_id is set once the user has already sent a file and is browsing
    to pick destination — used to differentiate the confirm action.
    """
    buttons = []
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_dirs = subdirs[start:end]

    # Folder buttons
    for full_path in page_dirs:
        name = full_path.rstrip("/").split("/")[-1]
        # Skip hidden dirs
        if name.startswith("."):
            continue
        cb = f"fb:enter:{full_path}:{page}:{pending_file_id}"
        # Truncate long paths in callback_data (Telegram limit: 64 bytes)
        if len(cb.encode()) > 64:
            cb = f"fb:enter:{full_path}::{pending_file_id}"
        buttons.append([InlineKeyboardButton(text=f"📁 {name}", callback_data=cb)])

    # Pagination row
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            text="◀ Prev",
            callback_data=f"fb:page:{current_path}:{page-1}:{pending_file_id}"
        ))
    if end < len(subdirs):
        nav.append(InlineKeyboardButton(
            text="Next ▶",
            callback_data=f"fb:page:{current_path}:{page+1}:{pending_file_id}"
        ))
    if nav:
        buttons.append(nav)

    # Up / confirm row
    action_row = []

    # Go up (unless already at root)
    parent = str(os.path.dirname(current_path.rstrip("/")))
    if current_path != BROWSER_ROOT and parent:
        action_row.append(InlineKeyboardButton(
            text="⬆️ Up",
            callback_data=f"fb:enter:{parent}:{0}:{pending_file_id}"
        ))

    # Upload here button
    confirm_text = "📂 Upload here" if pending_file_id else "✅ Select this folder"
    action_row.append(InlineKeyboardButton(
        text=confirm_text,
        callback_data=f"fb:confirm:{current_path}:{pending_file_id}"
    ))

    if action_row:
        buttons.append(action_row)

    # Cancel
    buttons.append([InlineKeyboardButton(text="❌ Cancel", callback_data="fb:cancel")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _browser_text(current_path: str, subdirs: list[str], page: int) -> str:
    start = page * PAGE_SIZE
    end = min(start + PAGE_SIZE, len(subdirs))
    total = len(subdirs)
    return (
        f"📂 <b>Folder Browser</b>\n\n"
        f"<b>Current:</b> <code>{current_path}</code>\n"
        f"<b>Subfolders:</b> {total}  "
        f"<i>(showing {start+1}–{end})</i>\n\n"
        f"Tap a folder to enter it, or tap <b>✅ Select this folder</b> / "
        f"<b>📂 Upload here</b> to use the current path."
    )


# ── /ls ───────────────────────────────────────────────────────────────────────

@file_router.message(Command("ls"))
async def cmd_ls(message: Message):
    await delete_command(message)
    code, output = await shell_engine.execute_cmd("ls -la")
    reply = await message.answer(f"📁 <b>Directory Indexing:</b>\n<pre>{output}</pre>", parse_mode="HTML")
    await delete_after(reply, delay=30)


# ── /cat ──────────────────────────────────────────────────────────────────────

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
    """Open the folder browser so user picks a destination first."""
    await delete_command(message)

    loading = await message.answer("📂 <b>Loading folder browser...</b>", parse_mode="HTML")
    subdirs = await asyncio.to_thread(_list_host_dirs, BROWSER_ROOT)
    code, subdirs = subdirs if isinstance(subdirs, tuple) else (0, subdirs)

    # _list_host_dirs returns tuple(code, list)
    _, dirs = await asyncio.to_thread(lambda: _list_host_dirs(BROWSER_ROOT))

    await state.set_state(UploadStates.browsing)
    await state.update_data(pending_file_id="", pending_file_name="")

    keyboard = _browser_keyboard(BROWSER_ROOT, dirs, page=0, pending_file_id="")
    await loading.edit_text(
        _browser_text(BROWSER_ROOT, dirs, page=0),
        parse_mode="HTML",
        reply_markup=keyboard
    )


# ── Browser callbacks ─────────────────────────────────────────────────────────

@file_router.callback_query(F.data.startswith("fb:enter:") | F.data.startswith("fb:page:"))
async def cb_browser_navigate(call: CallbackQuery, state: FSMContext):
    await call.answer()
    parts = call.data.split(":", 3)
    # parts: ["fb", "enter"|"page", path_and_rest]
    # actual format: fb:enter:<path>:<page>:<pending_file_id>
    raw = call.data[len("fb:enter:") if call.data.startswith("fb:enter:") else len("fb:page:"):]
    # Split from right to get page and pending_file_id safely
    segments = raw.rsplit(":", 2)
    if len(segments) == 3:
        path, page_str, pending_file_id = segments
        page = int(page_str) if page_str.isdigit() else 0
    elif len(segments) == 2:
        path, page_str = segments
        page = int(page_str) if page_str.isdigit() else 0
        pending_file_id = ""
    else:
        path = raw
        page = 0
        pending_file_id = ""

    _, dirs = await asyncio.to_thread(lambda: _list_host_dirs(path))

    keyboard = _browser_keyboard(path, dirs, page=page, pending_file_id=pending_file_id)
    try:
        await call.message.edit_text(
            _browser_text(path, dirs, page),
            parse_mode="HTML",
            reply_markup=keyboard
        )
    except Exception:
        pass  # message unchanged if same content


@file_router.callback_query(F.data.startswith("fb:confirm:"))
async def cb_browser_confirm(call: CallbackQuery, state: FSMContext):
    await call.answer()
    # format: fb:confirm:<path>:<pending_file_id>
    raw = call.data[len("fb:confirm:"):]
    parts = raw.rsplit(":", 1)
    if len(parts) == 2:
        dest_path, pending_file_id = parts
    else:
        dest_path = raw
        pending_file_id = ""

    if pending_file_id:
        # File was already sent before browsing — upload now
        fsm_data = await state.get_data()
        file_name = fsm_data.get("pending_file_name", "upload")
        await state.clear()
        await call.message.edit_text(
            f"📂 <b>Destination selected:</b> <code>{dest_path}</code>\n"
            f"⏳ Uploading <code>{file_name}</code>...",
            parse_mode="HTML"
        )
        await _save_pending_file(call.message, pending_file_id, file_name, dest_path, call.message.bot)
    else:
        # No file yet — store chosen path and ask for the file
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
    await call.message.edit_text("❌ Upload cancelled.", parse_mode="HTML")
    await delete_after(call.message, delay=5)


# ── Receive file ──────────────────────────────────────────────────────────────

@file_router.message(
    UploadStates.waiting_for_file,
    F.document | F.photo | F.video | F.audio | F.voice
)
async def cmd_receive_file_after_browse(message: Message, state: FSMContext):
    """User picked folder first, now sent the file."""
    fsm_data = await state.get_data()
    dest_dir = fsm_data.get("chosen_dir", DEFAULT_UPLOAD_DIR)
    await state.clear()
    await _handle_upload(message, override_dest=dest_dir)


@file_router.message(F.document | F.photo | F.video | F.audio | F.voice)
async def cmd_receive_file(message: Message, state: FSMContext):
    """
    File sent with no prior /upload command.
    If caption contains a path → use it directly.
    Otherwise → open folder browser with this file queued.
    """
    caption = (message.caption or "").strip()

    # Caption has explicit path — upload directly
    if caption and (caption.startswith("/") or caption.startswith("/upload")):
        await _handle_upload(message)
        return

    # No path — open folder browser with file queued
    file_obj, original_name = _extract_file(message)
    if not file_obj:
        return

    file_id = file_obj.file_id
    loading = await message.answer("📂 <b>Pick a destination folder:</b>", parse_mode="HTML")
    _, dirs = await asyncio.to_thread(lambda: _list_host_dirs(BROWSER_ROOT))

    await state.set_state(UploadStates.browsing)
    await state.update_data(pending_file_id=file_id, pending_file_name=original_name)

    keyboard = _browser_keyboard(BROWSER_ROOT, dirs, page=0, pending_file_id=file_id)
    await loading.edit_text(
        f"📄 <b>File queued:</b> <code>{original_name}</code>\n\n"
        + _browser_text(BROWSER_ROOT, dirs, page=0),
        parse_mode="HTML",
        reply_markup=keyboard
    )


# ── Core upload helpers ───────────────────────────────────────────────────────

def _extract_file(message: Message) -> tuple:
    """Return (file_obj, original_name) from a message."""
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


async def _save_pending_file(message: Message, file_id: str, file_name: str, dest_dir: str, bot):
    """Download a file by file_id and save to dest_dir on the host via SSH write."""
    status_msg = await message.answer(
        f"📥 <b>Uploading</b> <code>{file_name}</code>...\n"
        f"📂 <code>{dest_dir}</code>",
        parse_mode="HTML"
    )
    try:
        tg_file = await bot.get_file(file_id)
        file_bytes_io = await bot.download_file(tg_file.file_path)
        file_bytes = file_bytes_io.read()
        await _write_file_to_host(dest_dir, file_name, file_bytes, status_msg)
    except Exception as e:
        await status_msg.edit_text(f"❌ <b>Upload Failed</b>\n\n<code>{e}</code>", parse_mode="HTML")
        await delete_after(status_msg, delay=20)


async def _handle_upload(message: Message, override_dest: str = None):
    """Handle upload for messages with explicit caption path or override_dest."""
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
        f"📥 <b>Uploading</b> <code>{original_name}</code>...\n"
        f"📂 <code>{dest_dir}</code>",
        parse_mode="HTML"
    )
    try:
        tg_file = await bot.get_file(file_obj.file_id)
        file_bytes_io = await bot.download_file(tg_file.file_path)
        file_bytes = file_bytes_io.read()
        await _write_file_to_host(dest_dir, original_name, file_bytes, status_msg)
    except Exception as e:
        await status_msg.edit_text(f"❌ <b>Upload Failed</b>\n\n<code>{e}</code>", parse_mode="HTML")
        await delete_after(status_msg, delay=20)


async def _write_file_to_host(dest_dir: str, file_name: str, file_bytes: bytes, status_msg: Message):
    """Write file bytes to the WSL host via SFTP over the SSH bridge."""
    ssh_user = os.getenv("HOST_SSH_USER")
    ssh_pass = os.getenv("HOST_SSH_PASSWORD")

    # Fallback: write to container if no SSH credentials
    if not ssh_user or not ssh_pass:
        await _write_file_local(dest_dir, file_name, file_bytes, status_msg)
        return

    def _sftp_write():
        import paramiko, io
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect("127.0.0.1", username=ssh_user, password=ssh_pass, timeout=15)
        try:
            sftp = ssh.open_sftp()
            # Create directory on host
            ssh.exec_command(f'mkdir -p "{dest_dir}"')[1].channel.recv_exit_status()

            dest_path = f"{dest_dir.rstrip('/')}/{file_name}"

            # Handle name collision
            try:
                sftp.stat(dest_path)
                base, ext = os.path.splitext(file_name)
                counter = 1
                while True:
                    try:
                        new_name = f"{base}_{counter}{ext}"
                        dest_path = f"{dest_dir.rstrip('/')}/{new_name}"
                        sftp.stat(dest_path)
                        counter += 1
                    except FileNotFoundError:
                        file_name_final = new_name
                        break
            except FileNotFoundError:
                file_name_final = file_name

            dest_path = f"{dest_dir.rstrip('/')}/{file_name_final}"
            sftp.putfo(io.BytesIO(file_bytes), dest_path)
            sftp.close()
            return dest_path, len(file_bytes)
        finally:
            ssh.close()

    try:
        dest_path, size_bytes = await asyncio.to_thread(_sftp_write)
        size_str = (
            f"{size_bytes} B" if size_bytes < 1024
            else f"{size_bytes/1024:.1f} KB" if size_bytes < 1024**2
            else f"{size_bytes/1024**2:.2f} MB"
        )
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
            f"❌ <b>Permission Denied</b>\n\nCannot write to <code>{dest_dir}</code>.",
            parse_mode="HTML"
        )
        await delete_after(status_msg, delay=20)
    except Exception as e:
        await status_msg.edit_text(f"❌ <b>Upload Failed</b>\n\n<code>{e}</code>", parse_mode="HTML")
        await delete_after(status_msg, delay=20)


async def _write_file_local(dest_dir: str, file_name: str, file_bytes: bytes, status_msg: Message):
    """Fallback: write to container filesystem."""
    try:
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, file_name)
        if os.path.exists(dest_path):
            base, ext = os.path.splitext(file_name)
            counter = 1
            while os.path.exists(dest_path):
                dest_path = os.path.join(dest_dir, f"{base}_{counter}{ext}")
                counter += 1
        with open(dest_path, "wb") as f:
            f.write(file_bytes)
        size_bytes = len(file_bytes)
        size_str = (
            f"{size_bytes} B" if size_bytes < 1024
            else f"{size_bytes/1024:.1f} KB" if size_bytes < 1024**2
            else f"{size_bytes/1024**2:.2f} MB"
        )
        await status_msg.edit_text(
            f"✅ <b>Upload Complete</b> (container)\n\n"
            f"📄 <b>File:</b> <code>{os.path.basename(dest_path)}</code>\n"
            f"📂 <b>Saved to:</b> <code>{dest_path}</code>\n"
            f"📦 <b>Size:</b> <code>{size_str}</code>",
            parse_mode="HTML"
        )
        await delete_after(status_msg, delay=30)
    except Exception as e:
        await status_msg.edit_text(f"❌ <b>Upload Failed</b>\n\n<code>{e}</code>", parse_mode="HTML")
        await delete_after(status_msg, delay=20)
