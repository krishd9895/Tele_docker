"""
/runpy — run an uploaded Python script (plus optional input files) on the WSL
host in a temporary folder that is always wiped afterwards.

Flow
────
  1. User sends a .py file (auto-starts session) OR runs /runpy first.
  2. User sends more files as needed (data files, extra modules, etc.).
  3. User optionally types a build/install command, e.g.:
         pip install requests pandas openpyxl
     Type "skip" to skip this step (or leave it empty).
  4. User taps ▶️ Run — build command runs, then the script runs.
  5. Console output + any new/changed files are sent back.
  6. Temp folder is deleted — success, failure, cancel, or timeout.

Auto-cancel
───────────
  If no activity (no uploads, no messages, no button taps) for 2 minutes,
  the session is cancelled automatically and the workspace is wiped.
"""

import asyncio
import html as _html
import os

from aiogram import Router, F
from aiogram.filters import Command, StateFilter
from aiogram.types import (
    Message, CallbackQuery, BufferedInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from middlewares.totp_auth import require_2fa
from utils.msg_cleaner import delete_command, delete_after
from services import runpy_service

runpy_router = Router()

_MAX_OUTPUT_FILES = 15
_MAX_TG_BYTES = 50 * 1024 * 1024

# Auto-cancel after this many seconds of inactivity.
_INACTIVITY_SEC = 120  # 2 minutes


class RunPyStates(StatesGroup):
    collecting = State()       # uploading files + optional build command
    choosing_entry = State()   # multiple .py files — pick the entry point


# { user_id: {"dir", "files", "build_cmd", "status_msg_id", "chat_id", "state"} }
_sessions: dict[int, dict] = {}
# Users currently mid-execution — blocks a second /runpy until finished.
_running: set[int] = set()
# Per-user inactivity timeout tasks.
_timeout_tasks: dict[int, asyncio.Task] = {}


# ── Timeout helpers ───────────────────────────────────────────────────────────

def _cancel_timeout(user_id: int):
    """Cancel any pending inactivity timer for this user."""
    task = _timeout_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()


def _schedule_timeout(user_id: int, sess: dict, bot):
    """
    (Re-)start the inactivity timer.  Call on every user interaction so the
    clock resets each time they do something.  Fires after _INACTIVITY_SEC
    seconds of silence.
    """
    _cancel_timeout(user_id)

    async def _waiter():
        await asyncio.sleep(_INACTIVITY_SEC)
        if user_id not in _sessions:
            return  # already cleaned up
        s = _sessions.get(user_id)
        if not s:
            return
        try:
            await bot.edit_message_text(
                "⏰ <b>Session timed out</b> — no activity for 2 minutes.\n"
                "Temp workspace and all uploaded files have been deleted. 🗑",
                chat_id=s["chat_id"],
                message_id=s["status_msg_id"],
                parse_mode="HTML",
            )
        except Exception:
            pass
        await _cleanup_and_clear(user_id)

    task = asyncio.create_task(_waiter())
    _timeout_tasks[user_id] = task


# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 ** 2:.2f} MB"


def _collecting_text(files: list[str], build_cmd: str | None = None) -> str:
    lines = ["🐍 <b>Python Runner</b>", ""]

    if not files:
        lines += [
            "📁 Temp workspace ready — wiped automatically after the run.",
            "",
            "📤 <b>Step 1 — Send your <code>.py</code> file</b> to get started.",
            "You can also attach extra files the script needs",
            "(e.g. <code>.xlsx</code>, <code>.csv</code>, <code>.json</code>, <code>.pdf</code>).",
        ]
    else:
        lines += ["📁 <b>Files in temp workspace:</b>"]
        for f in files:
            emoji = "🐍" if f.lower().endswith(".py") else "📎"
            lines.append(f"  {emoji} <code>{_html.escape(f)}</code>")
        lines.append("")

        if build_cmd:
            lines += [
                "🔧 <b>Build command set:</b>",
                f"  <code>{_html.escape(build_cmd)}</code>",
                "",
                "<i>Type a new command to replace it, "
                "send <code>skip</code> to clear it, "
                "or tap 🗑 Clear Build.</i>",
                "",
            ]
        else:
            lines += [
                "💡 <b>Step 2 (optional) — Build / install command</b>",
                "If your script needs packages, type the install command:",
                "  <code>pip install requests pandas openpyxl</code>",
                "Send <code>skip</code> or just tap ▶️ Run to skip this step.",
                "",
            ]

        has_py = any(f.lower().endswith(".py") for f in files)
        if has_py:
            lines.append("✅ <b>Step 3 — Tap ▶️ Run when ready.</b>")
        else:
            lines.append("⚠️ Send at least one <code>.py</code> file to enable ▶️ Run.")

    lines += [
        "",
        "──────────────────────────────",
        "❌ <b>Cancel</b> deletes the temp workspace immediately.",
        "⏰ <i>Auto-cancelled after 2 min of inactivity.</i>",
    ]
    return "\n".join(lines)


def _collecting_keyboard(files: list[str], build_cmd: str | None = None) -> InlineKeyboardMarkup:
    rows = []
    if any(f.lower().endswith(".py") for f in files):
        rows.append([InlineKeyboardButton(text="▶️ Run", callback_data="rpy:run")])
    if build_cmd:
        rows.append([InlineKeyboardButton(text="🗑 Clear Build Command", callback_data="rpy:clear_build")])
    rows.append([InlineKeyboardButton(text="❌ Cancel", callback_data="rpy:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _refresh_status(sess: dict, bot) -> bool:
    """Re-render the pinned status message. Returns False if it needs to be re-sent."""
    try:
        await bot.edit_message_text(
            _collecting_text(sess["files"], sess.get("build_cmd")),
            chat_id=sess["chat_id"],
            message_id=sess["status_msg_id"],
            parse_mode="HTML",
            reply_markup=_collecting_keyboard(sess["files"], sess.get("build_cmd")),
        )
        return True
    except Exception:
        return False


async def _cleanup_and_clear(user_id: int, state: FSMContext | None = None):
    """
    Cancel the inactivity timer, delete the host workspace, and clear FSM state.
    Safe to call multiple times (idempotent).
    """
    _cancel_timeout(user_id)
    sess = _sessions.pop(user_id, None)
    if sess and sess.get("dir"):
        await runpy_service.cleanup_session(sess["dir"])
    # Prefer the explicitly-passed state; fall back to the one stored in the session.
    fsm = state or (sess.get("state") if sess else None)
    if fsm is not None:
        try:
            await fsm.clear()
        except Exception:
            pass


async def _init_session(user_id: int, message: Message, state: FSMContext) -> dict | None:
    """
    Create a fresh temp workspace on the host, register the session, start the
    inactivity timer, and post the pinned status panel.
    Returns the session dict on success, None on failure.
    """
    status = await message.answer("📂 <b>Preparing temp workspace…</b>", parse_mode="HTML")
    try:
        session_dir = await runpy_service.create_session(user_id)
    except Exception as e:
        await status.edit_text(
            f"❌ Could not prepare workspace:\n<code>{_html.escape(str(e))}</code>",
            parse_mode="HTML",
        )
        return None

    sess: dict = {
        "dir": session_dir,
        "files": [],
        "build_cmd": None,
        "chat_id": message.chat.id,
        "status_msg_id": status.message_id,
        "state": state,  # stored so the timeout task can clear FSM state
    }
    _sessions[user_id] = sess
    await state.set_state(RunPyStates.collecting)
    await status.edit_text(
        _collecting_text([], None),
        parse_mode="HTML",
        reply_markup=_collecting_keyboard([], None),
    )
    _schedule_timeout(user_id, sess, message.bot)
    return sess


# ── /runpy command ────────────────────────────────────────────────────────────

@runpy_router.message(Command("runpy"))
@require_2fa
async def cmd_runpy(message: Message, state: FSMContext):
    await delete_command(message)
    user_id = message.from_user.id

    if user_id in _running:
        reply = await message.answer(
            "⏳ A script is already running for you — wait for it to finish.",
            parse_mode="HTML",
        )
        await delete_after(reply, delay=10)
        return

    if not runpy_service.is_configured():
        reply = await message.answer(
            "⚠️ SSH not configured.\n"
            "Set <code>HOST_SSH_USER</code> and <code>HOST_SSH_PASSWORD</code> in .env.",
            parse_mode="HTML",
        )
        await delete_after(reply, delay=15)
        return

    # Discard any stale incomplete session.
    if user_id in _sessions:
        await _cleanup_and_clear(user_id, state)

    await _init_session(user_id, message, state)


# ── Auto-start: .py file sent with NO active session ─────────────────────────

@runpy_router.message(StateFilter(None), F.document)
async def cmd_runpy_auto_start(message: Message, state: FSMContext):
    """
    When a .py file is sent outside any active session, automatically spin up
    a RunPy workspace — no /runpy command needed.
    """
    doc = message.document
    filename = doc.file_name or ""
    if not filename.lower().endswith(".py"):
        return  # not a Python file — let other routers handle it

    user_id = message.from_user.id

    if not runpy_service.is_configured():
        reply = await message.answer(
            "⚠️ SSH not configured — can't run Python files.\n"
            "(<code>HOST_SSH_USER</code> / <code>HOST_SSH_PASSWORD</code> must be set in .env)",
            parse_mode="HTML",
        )
        await delete_after(reply, delay=15)
        return

    if user_id in _running:
        reply = await message.answer(
            "⏳ A script is already running for you — wait for it to finish.", parse_mode="HTML"
        )
        await delete_after(reply, delay=10)
        return

    await delete_command(message)
    sess = await _init_session(user_id, message, state)
    if not sess:
        return

    # Upload the triggering file immediately.
    try:
        tg_file = await message.bot.get_file(doc.file_id)
        file_bytes = (await message.bot.download_file(tg_file.file_path)).read()
        await runpy_service.upload_input_file(sess["dir"], filename, file_bytes)
        sess["files"].append(filename)
        _schedule_timeout(user_id, sess, message.bot)  # reset timer after activity
        ok = await _refresh_status(sess, message.bot)
        if not ok:
            new_status = await message.answer(
                _collecting_text(sess["files"], sess.get("build_cmd")),
                parse_mode="HTML",
                reply_markup=_collecting_keyboard(sess["files"], sess.get("build_cmd")),
            )
            sess["status_msg_id"] = new_status.message_id
    except Exception as e:
        reply = await message.answer(
            f"❌ Failed to receive <code>{_html.escape(filename)}</code>:\n"
            f"<code>{_html.escape(str(e))}</code>",
            parse_mode="HTML",
        )
        await delete_after(reply, delay=15)


# ── Receive additional files while collecting ─────────────────────────────────

@runpy_router.message(RunPyStates.collecting, F.document)
async def cmd_runpy_receive_file(message: Message, state: FSMContext):
    user_id = message.from_user.id
    sess = _sessions.get(user_id)
    if not sess:
        await message.answer("⚠️ No active session. Run /runpy or send a .py file to start one.")
        await state.clear()
        return

    doc = message.document
    filename = doc.file_name or "file"
    await delete_command(message)

    try:
        tg_file = await message.bot.get_file(doc.file_id)
        file_bytes = (await message.bot.download_file(tg_file.file_path)).read()
        await runpy_service.upload_input_file(sess["dir"], filename, file_bytes)
    except Exception as e:
        reply = await message.answer(
            f"❌ Failed to receive <code>{_html.escape(filename)}</code>:\n"
            f"<code>{_html.escape(str(e))}</code>",
            parse_mode="HTML",
        )
        await delete_after(reply, delay=15)
        return

    sess["files"].append(filename)
    _schedule_timeout(user_id, sess, message.bot)  # reset timer
    ok = await _refresh_status(sess, message.bot)
    if not ok:
        new_status = await message.answer(
            _collecting_text(sess["files"], sess.get("build_cmd")),
            parse_mode="HTML",
            reply_markup=_collecting_keyboard(sess["files"], sess.get("build_cmd")),
        )
        sess["status_msg_id"] = new_status.message_id


# ── Build command: any text message while collecting ─────────────────────────

@runpy_router.message(RunPyStates.collecting, F.text)
async def cmd_runpy_set_build(message: Message, state: FSMContext):
    """
    Any plain-text message during file collection sets the build/install
    command (e.g. 'pip install requests pandas').
    Sending 'skip' (case-insensitive) clears any previously set build command.
    """
    user_id = message.from_user.id
    sess = _sessions.get(user_id)
    if not sess:
        await message.answer("⚠️ No active session. Run /runpy or send a .py file to start one.")
        await state.clear()
        return

    await delete_command(message)
    raw = (message.text or "").strip()
    if not raw:
        return

    _schedule_timeout(user_id, sess, message.bot)  # reset timer on any text

    if raw.lower() == "skip":
        sess["build_cmd"] = None
    else:
        sess["build_cmd"] = raw

    ok = await _refresh_status(sess, message.bot)
    if not ok:
        new_status = await message.answer(
            _collecting_text(sess["files"], sess.get("build_cmd")),
            parse_mode="HTML",
            reply_markup=_collecting_keyboard(sess["files"], sess.get("build_cmd")),
        )
        sess["status_msg_id"] = new_status.message_id


# ── Callbacks ─────────────────────────────────────────────────────────────────

@runpy_router.callback_query(F.data == "rpy:clear_build")
async def cb_runpy_clear_build(call: CallbackQuery, state: FSMContext):
    await call.answer("Build command cleared.")
    user_id = call.from_user.id
    sess = _sessions.get(user_id)
    if not sess:
        return
    sess["build_cmd"] = None
    _schedule_timeout(user_id, sess, call.bot)  # reset timer
    try:
        await call.message.edit_text(
            _collecting_text(sess["files"], None),
            parse_mode="HTML",
            reply_markup=_collecting_keyboard(sess["files"], None),
        )
    except Exception:
        pass


@runpy_router.callback_query(F.data == "rpy:cancel")
async def cb_runpy_cancel(call: CallbackQuery, state: FSMContext):
    await call.answer()
    user_id = call.from_user.id
    await _cleanup_and_clear(user_id, state)
    try:
        await call.message.edit_text(
            "❌ <b>Cancelled.</b>\n"
            "Temp workspace and all uploaded files have been deleted. 🗑",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await delete_after(call.message, delay=6)


@runpy_router.callback_query(F.data == "rpy:run")
async def cb_runpy_run(call: CallbackQuery, state: FSMContext):
    await call.answer()
    user_id = call.from_user.id
    sess = _sessions.get(user_id)
    if not sess:
        await call.message.edit_text("⚠️ Session expired. Run /runpy or send a .py file again.")
        return

    _schedule_timeout(user_id, sess, call.bot)  # reset timer — user is active

    py_files = runpy_service.list_py_files(sess["files"])
    if not py_files:
        await call.answer("Send at least one .py file first.", show_alert=True)
        return

    if len(py_files) == 1:
        await _start_run(call.message, state, user_id, py_files[0])
        return

    # Multiple .py files — ask which to use as the entry point.
    await state.set_state(RunPyStates.choosing_entry)
    rows = [
        [InlineKeyboardButton(text=f"🐍 {f}", callback_data=f"rpy:entry:{i}")]
        for i, f in enumerate(py_files)
    ]
    rows.append([InlineKeyboardButton(text="❌ Cancel", callback_data="rpy:cancel")])
    await state.update_data(py_files=py_files)
    await call.message.edit_text(
        "🐍 <b>Multiple .py files found</b>\n\n"
        "Which one should be the <b>entry point</b> (the file to run first)?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@runpy_router.callback_query(F.data.startswith("rpy:entry:"))
async def cb_runpy_choose_entry(call: CallbackQuery, state: FSMContext):
    await call.answer()
    user_id = call.from_user.id
    sess = _sessions.get(user_id)
    if not sess:
        await call.message.edit_text("⚠️ Session expired. Run /runpy or send a .py file again.")
        return

    data = await state.get_data()
    py_files = data.get("py_files", [])
    try:
        idx = int(call.data.split(":")[2])
        entry_file = py_files[idx]
    except (IndexError, ValueError):
        await call.message.edit_text("⚠️ Invalid selection. Run /runpy again.")
        return

    await _start_run(call.message, state, user_id, entry_file)


# ── Execution ─────────────────────────────────────────────────────────────────

async def _start_run(status_message: Message, state: FSMContext, user_id: int, entry_file: str):
    sess = _sessions.get(user_id)
    if not sess:
        return

    # Stop the inactivity timer — execution is in progress.
    _cancel_timeout(user_id)
    await state.clear()
    _running.add(user_id)

    build_cmd = sess.get("build_cmd")
    has_requirements = "requirements.txt" in sess["files"]

    if build_cmd:
        build_note = f"\n🔧 <i>Running build: <code>{_html.escape(build_cmd)}</code></i>"
    elif has_requirements:
        build_note = "\n📦 <i>Installing <code>requirements.txt</code>…</i>"
    else:
        build_note = ""

    try:
        await status_message.edit_text(
            f"⏳ <b>Running</b> <code>{_html.escape(entry_file)}</code>…{build_note}\n\n"
            f"<i>Execution in progress — tap Cancel below to abort and clear the workspace.</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Cancel (clears workspace)", callback_data="rpy:cancel")]
            ]),
        )
    except Exception:
        pass

    try:
        result = await runpy_service.run_script(
            sess["dir"],
            entry_file,
            build_cmd=build_cmd,
            has_requirements=has_requirements,
        )
    except Exception as e:
        try:
            await status_message.edit_text(
                f"❌ <b>Unexpected error while running:</b>\n<code>{_html.escape(str(e))}</code>",
                parse_mode="HTML",
            )
        except Exception:
            pass
        await _cleanup_and_clear(user_id)
        _running.discard(user_id)
        return

    try:
        await _deliver_results(status_message, entry_file, result, sess["dir"])
    finally:
        await _cleanup_and_clear(user_id)
        _running.discard(user_id)


async def _deliver_results(
    status_message: Message,
    entry_file: str,
    result: dict,
    session_dir: str,
):
    exit_code = result["exit_code"]
    output = result["output"]
    timed_out = result["timed_out"]
    setup_log = result.get("setup_log") or []
    changed_files = result.get("changed_files") or []

    if timed_out:
        status_text = "⏹️ <b>Timed out</b> — script was force-stopped."
    elif exit_code == 0:
        status_text = "✅ <b>Finished successfully</b>"
    else:
        status_text = "❌ <b>Finished with an error</b>"

    safe_output = _html.escape(output)
    header = (
        f"{status_text}\n"
        f"<b>▶</b> <code>python {_html.escape(entry_file)}</code>\n"
        f"<b>Exit code:</b> <code>{exit_code}</code>\n\n"
    )
    if setup_log:
        header += "<i>" + _html.escape("\n".join(setup_log)) + "</i>\n\n"

    full_text = header + (f"<pre>{safe_output}</pre>" if safe_output else "<i>(no console output)</i>")

    try:
        await status_message.delete()
    except Exception:
        pass

    bot = status_message.bot
    chat_id = status_message.chat.id

    if len(full_text) <= 4096:
        await bot.send_message(chat_id, full_text, parse_mode="HTML")
    else:
        tail = safe_output[-3500:]
        if "\n" in tail:
            tail = tail[tail.index("\n") + 1:]
        truncated = (
            header
            + "<i>⚠️ Output too long — showing last 3500 chars. Full output attached below.</i>\n\n"
            + f"<pre>{tail}</pre>"
        )
        await bot.send_message(chat_id, truncated, parse_mode="HTML")
        raw_bytes = output.encode("utf-8", errors="replace")
        if len(raw_bytes) > 10 * 1024 * 1024:
            raw_bytes = raw_bytes[:10 * 1024 * 1024] + b"\n[OUTPUT CAPPED AT 10MB]"
        doc = BufferedInputFile(raw_bytes, filename="console_output.txt")
        await bot.send_document(
            chat_id, doc,
            caption=f"📄 Full console output — exit code {exit_code}",
        )

    if not changed_files:
        return

    if len(changed_files) > _MAX_OUTPUT_FILES:
        await bot.send_message(
            chat_id,
            f"⚠️ Script produced {len(changed_files)} new/changed files — "
            f"sending the first {_MAX_OUTPUT_FILES} only.",
            parse_mode="HTML",
        )
        changed_files = changed_files[:_MAX_OUTPUT_FILES]

    try:
        downloaded = await runpy_service.download_output_files(session_dir, changed_files)
    except Exception:
        downloaded = {}

    for rel_path, data in downloaded.items():
        if len(data) > _MAX_TG_BYTES:
            await bot.send_message(
                chat_id,
                f"⚠️ <code>{_html.escape(rel_path)}</code> is {_fmt_size(len(data))} "
                f"— too large for Telegram (50 MB limit).",
                parse_mode="HTML",
            )
            continue
        doc = BufferedInputFile(data, filename=os.path.basename(rel_path))
        await bot.send_document(
            chat_id, doc,
            caption=f"📄 {rel_path}  ({_fmt_size(len(data))})",
        )
