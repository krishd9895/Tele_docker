"""
/runpy — run an uploaded Python script (plus optional input files like a
spreadsheet or PDF) on the WSL host, PyCharm-style: upload files, run,
get back the console output and any new/changed files, then everything is
deleted from the host automatically.

New, standalone router. Registered additively in main.py alongside every
other feature's router. Does not import from, call, or modify any existing
handler module — it only reuses shared, already-public helpers
(middlewares.totp_auth.require_2fa, utils.msg_cleaner) exactly the way every
other handler in this bot already does.
"""

import html as _html
import os

from aiogram import Router, F
from aiogram.filters import Command
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

# Cap how many output files we try to send back per run — a script that
# quietly produced hundreds of files (e.g. writing to the wrong directory)
# shouldn't turn into hundreds of Telegram uploads.
_MAX_OUTPUT_FILES = 15
# Telegram bot upload cap.
_MAX_TG_BYTES = 50 * 1024 * 1024


class RunPyStates(StatesGroup):
    collecting = State()
    choosing_entry = State()


# Per-user session bookkeeping. Small, short-lived, process-local state —
# same pattern as pydeploy_cmds._pending_dockerfile_choices. Lost on bot
# restart, which just means an in-progress /runpy upload would need to be
# restarted — no different from any other in-flight FSM state in this bot.
# { user_id: {"dir": str, "files": list[str], "status_msg_id": int, "chat_id": int} }
_sessions: dict[int, dict] = {}
# Users currently mid-execution — blocks a second /runpy until this one finishes.
_running: set[int] = set()


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 ** 2:.2f} MB"


def _collecting_text(files: list[str]) -> str:
    if not files:
        return (
            "🐍 <b>Run a Python script</b>\n\n"
            "Send me the <code>.py</code> file to run. You can also send extra "
            "input files it needs (e.g. an <code>.xlsx</code> or <code>.pdf</code>) — "
            "one at a time, in any order.\n\n"
            "Tap ▶️ Run once you've sent everything."
        )
    lines = ["🐍 <b>Run a Python script</b>\n", "<b>Files received:</b>"]
    for f in files:
        emoji = "📄" if f.lower().endswith(".py") else "📎"
        lines.append(f"{emoji} <code>{_html.escape(f)}</code>")
    lines.append("\nSend more files, or tap ▶️ Run.")
    return "\n".join(lines)


def _collecting_keyboard(files: list[str]) -> InlineKeyboardMarkup:
    rows = []
    if any(f.lower().endswith(".py") for f in files):
        rows.append([InlineKeyboardButton(text="▶️ Run", callback_data="rpy:run")])
    rows.append([InlineKeyboardButton(text="❌ Cancel", callback_data="rpy:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _cleanup_and_clear(user_id: int, state: FSMContext | None = None):
    sess = _sessions.pop(user_id, None)
    if sess and sess.get("dir"):
        await runpy_service.cleanup_session(sess["dir"])
    if state is not None:
        await state.clear()


@runpy_router.message(Command("runpy"))
@require_2fa
async def cmd_runpy(message: Message, state: FSMContext):
    await delete_command(message)
    user_id = message.from_user.id

    if user_id in _running:
        reply = await message.answer("⏳ A script is already running for you — please wait for it to finish.", parse_mode="HTML")
        await delete_after(reply, delay=10)
        return

    if not runpy_service.is_configured():
        reply = await message.answer(
            "⚠️ Host SSH is not configured (<code>HOST_SSH_USER</code> / <code>HOST_SSH_PASSWORD</code> in .env).",
            parse_mode="HTML"
        )
        await delete_after(reply, delay=15)
        return

    # Discard any stale, incomplete session for this user first.
    if user_id in _sessions:
        await _cleanup_and_clear(user_id, state)

    status = await message.answer("📂 <b>Preparing a workspace...</b>", parse_mode="HTML")
    try:
        session_dir = await runpy_service.create_session(user_id)
    except Exception as e:
        await status.edit_text(f"❌ Could not prepare a workspace:\n<code>{_html.escape(str(e))}</code>", parse_mode="HTML")
        return

    _sessions[user_id] = {"dir": session_dir, "files": [], "chat_id": message.chat.id}
    await state.set_state(RunPyStates.collecting)
    await status.edit_text(_collecting_text([]), parse_mode="HTML", reply_markup=_collecting_keyboard([]))
    _sessions[user_id]["status_msg_id"] = status.message_id


@runpy_router.message(RunPyStates.collecting, F.document)
async def cmd_runpy_receive_file(message: Message, state: FSMContext):
    user_id = message.from_user.id
    sess = _sessions.get(user_id)
    if not sess:
        await message.answer("⚠️ No active /runpy session. Run /runpy to start one.")
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
        reply = await message.answer(f"❌ Failed to receive <code>{_html.escape(filename)}</code>:\n<code>{_html.escape(str(e))}</code>", parse_mode="HTML")
        await delete_after(reply, delay=15)
        return

    sess["files"].append(filename)
    text = _collecting_text(sess["files"])
    keyboard = _collecting_keyboard(sess["files"])
    try:
        await message.bot.edit_message_text(
            text, chat_id=sess["chat_id"], message_id=sess["status_msg_id"],
            parse_mode="HTML", reply_markup=keyboard
        )
    except Exception:
        new_status = await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
        sess["status_msg_id"] = new_status.message_id


@runpy_router.callback_query(F.data == "rpy:cancel")
async def cb_runpy_cancel(call: CallbackQuery, state: FSMContext):
    await call.answer()
    user_id = call.from_user.id
    await _cleanup_and_clear(user_id, state)
    try:
        await call.message.edit_text("❌ /runpy cancelled — workspace cleaned up.")
    except Exception:
        pass
    await delete_after(call.message, delay=5)


@runpy_router.callback_query(F.data == "rpy:run")
async def cb_runpy_run(call: CallbackQuery, state: FSMContext):
    await call.answer()
    user_id = call.from_user.id
    sess = _sessions.get(user_id)
    if not sess:
        await call.message.edit_text("⚠️ This session expired. Run /runpy again.")
        return

    py_files = runpy_service.list_py_files(sess["files"])
    if not py_files:
        await call.answer("Send at least one .py file first.", show_alert=True)
        return

    if len(py_files) == 1:
        await _start_run(call.message, state, user_id, py_files[0])
        return

    await state.set_state(RunPyStates.choosing_entry)
    rows = [[InlineKeyboardButton(text=f"📄 {f}", callback_data=f"rpy:entry:{i}")] for i, f in enumerate(py_files)]
    rows.append([InlineKeyboardButton(text="❌ Cancel", callback_data="rpy:cancel")])
    await state.update_data(py_files=py_files)
    await call.message.edit_text(
        "🐍 <b>Multiple .py files detected</b>\n\nWhich one should be run as the entry point?",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


@runpy_router.callback_query(F.data.startswith("rpy:entry:"))
async def cb_runpy_choose_entry(call: CallbackQuery, state: FSMContext):
    await call.answer()
    user_id = call.from_user.id
    sess = _sessions.get(user_id)
    if not sess:
        await call.message.edit_text("⚠️ This session expired. Run /runpy again.")
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


async def _start_run(status_message: Message, state: FSMContext, user_id: int, entry_file: str):
    sess = _sessions.get(user_id)
    if not sess:
        return

    await state.clear()
    _running.add(user_id)
    has_requirements = "requirements.txt" in sess["files"]

    try:
        await status_message.edit_text(
            f"⏳ <b>Running</b> <code>{_html.escape(entry_file)}</code>...\n"
            f"<i>This may take a moment, especially if dependencies need installing.</i>",
            parse_mode="HTML"
        )
    except Exception:
        pass

    try:
        result = await runpy_service.run_script(sess["dir"], entry_file, has_requirements)
    except Exception as e:
        try:
            await status_message.edit_text(
                f"❌ <b>Unexpected error while running the script:</b>\n<code>{_html.escape(str(e))}</code>",
                parse_mode="HTML"
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


async def _deliver_results(status_message: Message, entry_file: str, result: dict, session_dir: str):
    exit_code = result["exit_code"]
    output = result["output"]
    timed_out = result["timed_out"]
    setup_log = result.get("setup_log") or []
    changed_files = result.get("changed_files") or []

    if timed_out:
        status_text = "⏹️ <b>Timed out</b> — the script was force-stopped."
    elif exit_code == 0:
        status_text = "✅ <b>Finished</b>"
    else:
        status_text = "❌ <b>Finished with an error</b>"

    safe_output = _html.escape(output)
    header = f"{status_text}\n<b>$</b> <code>python {_html.escape(entry_file)}</code>\n<b>Exit code:</b> <code>{exit_code}</code>\n\n"
    if setup_log:
        header = header + "<i>" + _html.escape("\n".join(setup_log)) + "</i>\n\n"

    full_text = header + f"<pre>{safe_output}</pre>" if safe_output else header + "<i>(no console output)</i>"

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
        truncated = header + "<i>⚠️ Output too long — showing last 3500 chars. Full output attached.</i>\n\n" + f"<pre>{tail}</pre>"
        await bot.send_message(chat_id, truncated, parse_mode="HTML")

        raw_bytes = output.encode("utf-8", errors="replace")
        if len(raw_bytes) > 10 * 1024 * 1024:
            raw_bytes = raw_bytes[:10 * 1024 * 1024] + b"\n[OUTPUT CAPPED AT 10MB]"
        doc = BufferedInputFile(raw_bytes, filename="console_output.txt")
        await bot.send_document(chat_id, doc, caption=f"📄 Full console output — exit code {exit_code}")

    if not changed_files:
        return

    if len(changed_files) > _MAX_OUTPUT_FILES:
        await bot.send_message(
            chat_id,
            f"⚠️ The script produced {len(changed_files)} new/changed files — sending only the first {_MAX_OUTPUT_FILES}.",
            parse_mode="HTML"
        )
        changed_files = changed_files[:_MAX_OUTPUT_FILES]

    try:
        downloaded = await runpy_service.download_output_files(session_dir, changed_files)
    except Exception:
        downloaded = {}

    if not downloaded:
        return

    for rel_path, data in downloaded.items():
        if len(data) > _MAX_TG_BYTES:
            await bot.send_message(
                chat_id,
                f"⚠️ <code>{_html.escape(rel_path)}</code> is {_fmt_size(len(data))} — too large to send (50MB Telegram limit).",
                parse_mode="HTML"
            )
            continue
        doc = BufferedInputFile(data, filename=os.path.basename(rel_path))
        await bot.send_document(chat_id, doc, caption=f"📄 {rel_path}  ({_fmt_size(len(data))})")
