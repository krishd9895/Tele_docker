import os
import asyncio
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from utils.msg_cleaner import delete_command, delete_after, auto_clean
from utils.ssh_helper import get_ssh_creds, get_ssh_host, ssh_exec as _ssh_exec_helper
from middlewares.totp_auth import require_2fa

file_router = Router()

DEFAULT_UPLOAD_DIR = "/app/data/uploads"
PAGE_SIZE = 8

# ── Path registry ─────────────────────────────────────────────────────────────
_path_registry: dict[int, str] = {}
_path_registry_rev: dict[str, int] = {}
_path_counter = 0

def _reg(path: str) -> int:
    global _path_counter
    if path in _path_registry_rev:
        return _path_registry_rev[path]
    _path_counter += 1
    _path_registry[_path_counter] = path
    _path_registry_rev[path] = _path_counter
    return _path_counter

def _lookup(key: int) -> str | None:
    return _path_registry.get(key)

def _get_browser_root() -> str:
    """Get the browser root from settings, fallback to /home/d."""
    try:
        from config.settings import runtime_settings
        return getattr(runtime_settings, "BROWSER_ROOT", None) or "/home/d"
    except Exception:
        return "/home/d"

# ── FSM ───────────────────────────────────────────────────────────────────────

class UploadStates(StatesGroup):
    browsing         = State()
    waiting_for_file = State()


# ── SSH helpers ───────────────────────────────────────────────────────────────

def _list_host_dirs(path: str) -> list[str]:
    """Return sorted list of subdirectory and symlink-to-directory full paths via SSH."""
    user, _ = get_ssh_creds()
    if not user:
        return []
    # Find real directories AND symlinks to directories (test -d)
    code, out = _ssh_exec_helper(
        f'cd "{path}" && for item in *; do [ -e "$item" ] && [ ! "$(echo "$item" | cut -c1)" = "." ] && ( [ -d "$item" ] || [ -L "$item" ] && [ -d "$item" ] ) && echo "$(pwd)/$item"; done 2>/dev/null | sort',
        timeout=15
    )
    if code != 0 or not out.strip():
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


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
    if current_path.rstrip("/") != _get_browser_root().rstrip("/") and parent:
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



# ── /download ─────────────────────────────────────────────────────────────────

def _list_host_entries(path: str) -> tuple[list[str], list[str]]:
    """Return (subdirs, files) at path on the host via SSH (including symlinks)."""
    user, _ = get_ssh_creds()
    if not user:
        return [], []

    # Use a loop to categorize each item
    code, out = _ssh_exec_helper(
        f'cd "{path}" && for item in *; do [ -e "$item" ] && [ ! "$(echo "$item" | cut -c1)" = "." ] && ( [ -d "$item" ] || [ -L "$item" ] && [ -d "$item" ] ) && echo "D:$(pwd)/$item" || ( [ -f "$item" ] || [ -L "$item" ] && [ -f "$item" ] ) && echo "F:$(pwd)/$item"; done 2>/dev/null | sort',
        timeout=15
    )
    dirs = []
    files = []
    if code == 0 and out.strip():
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("D:"):
                dirs.append(line[2:])
            elif line.startswith("F:"):
                files.append(line[2:])
    return dirs, files


def _dl_browser_keyboard(path: str, dirs: list[str], files: list[str], page: int) -> InlineKeyboardMarkup:
    """Keyboard for download browser — shows folders and files, tap file to download."""
    buttons = []
    all_entries = [("dir", d) for d in dirs] + [("file", f) for f in files]
    start = page * PAGE_SIZE
    page_entries = all_entries[start: start + PAGE_SIZE]

    for kind, full_path in page_entries:
        name = full_path.rstrip("/").split("/")[-1]
        key = _reg(full_path)
        if kind == "dir":
            buttons.append([InlineKeyboardButton(
                text=f"📁 {name}",
                callback_data=f"dl:e:{key}:{page}"
            )])
        else:
            # Show file size if possible
            buttons.append([InlineKeyboardButton(
                text=f"📄 {name}",
                callback_data=f"dl:f:{key}"
            )])

    # Pagination
    nav = []
    cur_key = _reg(path)
    total = len(all_entries)
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀ Prev", callback_data=f"dl:p:{cur_key}:{page-1}"))
    if start + PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="Next ▶", callback_data=f"dl:p:{cur_key}:{page+1}"))
    if nav:
        buttons.append(nav)

    # Up button
    parent = str(os.path.dirname(path.rstrip("/")))
    if path.rstrip("/") != _get_browser_root().rstrip("/") and parent:
        pk = _reg(parent)
        buttons.append([InlineKeyboardButton(text="⬆️ Up", callback_data=f"dl:e:{pk}:0")])

    buttons.append([InlineKeyboardButton(text="❌ Cancel", callback_data="dl:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _dl_browser_text(path: str, dirs: list[str], files: list[str], page: int) -> str:
    total = len(dirs) + len(files)
    start = page * PAGE_SIZE
    end = min(start + PAGE_SIZE, total)
    return (
        f"📂 <b>Download Browser</b>\n\n"
        f"<b>Path:</b> <code>{path}</code>\n"
        f"<b>📁 Folders:</b> {len(dirs)}  <b>📄 Files:</b> {len(files)}"
        + (f"  <i>(page {page+1}, showing {start+1}–{end})</i>" if total > PAGE_SIZE else "")
        + "\n\nTap 📁 to navigate into a folder.\nTap 📄 to download that file."
    )


@file_router.message(Command("download"))
@require_2fa
async def cmd_download(message: Message):
    """Open folder browser to pick a file to download."""
    await delete_command(message)
    browser_root = _get_browser_root()

    # Check if SSH is available
    user, _ = get_ssh_creds()
    if not user:
        # Fall back to old behaviour — just use a path argument
        reply = await message.answer(
            "⚠️ SSH not configured. Usage: <code>/download &lt;filepath&gt;</code>",
            parse_mode="HTML"
        )
        await delete_after(reply, delay=10)
        return

    loading = await message.answer("📂 <b>Loading download browser...</b>", parse_mode="HTML")
    dirs, files = await asyncio.to_thread(_list_host_entries, browser_root)
    keyboard = _dl_browser_keyboard(browser_root, dirs, files, page=0)
    await loading.edit_text(
        _dl_browser_text(browser_root, dirs, files, page=0),
        parse_mode="HTML",
        reply_markup=keyboard
    )


@file_router.callback_query(F.data.startswith("dl:e:") | F.data.startswith("dl:p:"))
async def cb_dl_navigate(call: CallbackQuery):
    """Navigate into a folder or paginate."""
    await call.answer()
    parts = call.data.split(":")
    try:
        path_key = int(parts[2])
        page = int(parts[3])
    except (IndexError, ValueError):
        return

    path = _lookup(path_key)
    if not path:
        await call.answer("Path expired, please restart /download", show_alert=True)
        return

    dirs, files = await asyncio.to_thread(_list_host_entries, path)
    keyboard = _dl_browser_keyboard(path, dirs, files, page=page)
    try:
        await call.message.edit_text(
            _dl_browser_text(path, dirs, files, page),
            parse_mode="HTML",
            reply_markup=keyboard
        )
    except Exception:
        pass


@file_router.callback_query(F.data.startswith("dl:f:"))
async def cb_dl_download_file(call: CallbackQuery):
    """User tapped a file — download it via SFTP and send to Telegram."""
    await call.answer()
    parts = call.data.split(":")
    try:
        file_key = int(parts[2])
    except (IndexError, ValueError):
        return

    file_path = _lookup(file_key)
    if not file_path:
        await call.message.answer("❌ File reference expired. Please restart /download.")
        return

    filename = os.path.basename(file_path)
    status = await call.message.answer(
        f"📥 <b>Downloading</b> <code>{filename}</code>...",
        parse_mode="HTML"
    )

    # Download file from host via SFTP
    def _sftp_read() -> bytes:
        import io
        import paramiko as _pm
        from utils.ssh_helper import get_ssh_creds, get_ssh_host
        user, password = get_ssh_creds()
        host, port = get_ssh_host()
        ssh = _pm.SSHClient()
        ssh.set_missing_host_key_policy(_pm.AutoAddPolicy())
        ssh.connect(host, port=port, username=user, password=password, timeout=15)
        try:
            sftp = ssh.open_sftp()
            buf = io.BytesIO()
            sftp.getfo(file_path, buf)
            sftp.close()
            return buf.getvalue()
        finally:
            ssh.close()

    try:
        file_bytes = await asyncio.to_thread(_sftp_read)

        # Cap at 50MB (Telegram bot upload limit)
        MAX_TG_BYTES = 50 * 1024 * 1024
        if len(file_bytes) > MAX_TG_BYTES:
            await status.edit_text(
                f"❌ <b>File too large</b>\n\n"
                f"<code>{filename}</code> is {len(file_bytes) // 1024 // 1024}MB.\n"
                f"Telegram bots can only send files up to 50MB.",
                parse_mode="HTML"
            )
            return

        size = len(file_bytes)
        size_str = f"{size} B" if size < 1024 else f"{size/1024:.1f} KB" if size < 1024**2 else f"{size/1024**2:.2f} MB"

        doc = BufferedInputFile(file_bytes, filename=filename)
        await call.message.answer_document(
            doc,
            caption=f"📥 <b>{filename}</b>  ({size_str})\n<code>{file_path}</code>",
            parse_mode="HTML"
        )
        await status.delete()

    except FileNotFoundError:
        await status.edit_text(f"❌ File not found: <code>{file_path}</code>", parse_mode="HTML")
        await delete_after(status, delay=15)
    except PermissionError:
        await status.edit_text(f"❌ Permission denied: <code>{file_path}</code>", parse_mode="HTML")
        await delete_after(status, delay=15)
    except Exception as e:
        await status.edit_text(f"❌ <b>Download failed:</b>\n<code>{e}</code>", parse_mode="HTML")
        await delete_after(status, delay=15)


@file_router.callback_query(F.data == "dl:cancel")
async def cb_dl_cancel(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text("❌ Download cancelled.")
    await delete_after(call.message, delay=5)


# ── /upload ───────────────────────────────────────────────────────────────────

@file_router.message(Command("upload"))
@require_2fa
async def cmd_upload_prompt(message: Message, state: FSMContext):
    """Open folder browser so user picks a destination first."""
    await delete_command(message)
    browser_root = _get_browser_root()
    loading = await message.answer("📂 <b>Loading folder browser...</b>", parse_mode="HTML")
    dirs = await asyncio.to_thread(_list_host_dirs, browser_root)
    await state.set_state(UploadStates.browsing)
    await state.update_data(pending_file_id="", pending_file_name="")
    keyboard = _browser_keyboard(browser_root, dirs, page=0, pending_key="0")
    await loading.edit_text(
        _browser_text(browser_root, dirs, page=0),
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
    browser_root = _get_browser_root()
    dirs = await asyncio.to_thread(_list_host_dirs, browser_root)

    await state.set_state(UploadStates.browsing)
    await state.update_data(pending_file_id=file_id, pending_file_name=original_name)

    keyboard = _browser_keyboard(browser_root, dirs, page=0, pending_key=fid_key)
    await loading.edit_text(
        f"📄 <b>File queued:</b> <code>{original_name}</code>\n\n"
        + _browser_text(browser_root, dirs, page=0),
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
    """Write file to WSL host via SFTP using centralized ssh_helper. Falls back to local."""
    user, _ = get_ssh_creds()
    if not user:
        await _write_local(dest_dir, file_name, file_bytes, status_msg)
        return

    try:
        from utils.ssh_helper import sftp_write
        dest_path = await asyncio.to_thread(sftp_write, dest_dir, file_name, file_bytes)
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
