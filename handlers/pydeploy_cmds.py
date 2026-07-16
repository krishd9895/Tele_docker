"""
/pydeploy and /pyps — deploy and manage plain Python apps on the WSL host.

New, standalone router. Registered additively in main.py alongside every
other feature's router. Does not import from, call, or modify any existing
handler module — it only reuses shared, already-public helpers
(middlewares.totp_auth.require_2fa, utils.msg_cleaner) exactly the way every
other handler in this bot already does.
"""

import asyncio
import html as _html

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, BufferedInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from config.settings import runtime_settings
from middlewares.totp_auth import require_2fa
from utils.msg_cleaner import delete_command, delete_after
from database.pydeploy_models import get_deployment, list_deployments, update_deployment
from services.pydeploy_service import (
    deploy_from_git, deploy_from_archive, continue_provision,
    start_deployment, stop_deployment, restart_deployment, get_logs,
    update_from_git, update_from_archive, delete_deployment,
)

pydeploy_router = Router()

# Pending "this project has a Dockerfile" choices, keyed by chat id. Small,
# short-lived, process-local state — analogous to the crash-loop tracker in
# pydeploy_service.py. Lost on bot restart, which just means the user would
# need to re-run /pydeploy, no different from any other in-flight FSM state.
_pending_dockerfile_choices: dict[int, dict] = {}

# ── Wizard session tracking + timeout ────────────────────────────────────────
# Stores {user_id: {"chat_id": int, "state": FSMContext}} so the background
# timeout task can clean up FSM state even without a handler context.
_pyd_wizard: dict[int, dict] = {}
_pyd_timeout_tasks: dict[int, asyncio.Task] = {}
_PYD_INACTIVITY_SEC = 120  # 2 minutes

# Reusable single-button keyboard shown on every wizard prompt.
_CANCEL_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="❌ Cancel", callback_data="pyd:wizard_cancel")
]])

_STATUS_EMOJI = {
    "running": "🟢",
    "stopped": "⚪",
    "crashed": "🔴",
    "crash_looping": "🚨",
    "deploying": "🟡",
    "error": "🔴",
}


class PyDeployStates(StatesGroup):
    waiting_for_source = State()
    awaiting_build_command = State()
    awaiting_run_command = State()
    awaiting_env_content = State()
    awaiting_update_archive = State()


def _is_owner(user_id: int) -> bool:
    return user_id == runtime_settings.ALLOWED_USER_ID


# ── Wizard timeout helpers ────────────────────────────────────────────────────

def _pyd_cancel_timeout(user_id: int):
    task = _pyd_timeout_tasks.pop(user_id, None)
    if task and not task.done():
        task.cancel()


def _pyd_schedule_timeout(user_id: int, bot):
    """Reset the 2-min inactivity timer. Call on every wizard interaction."""
    _pyd_cancel_timeout(user_id)

    async def _waiter():
        await asyncio.sleep(_PYD_INACTIVITY_SEC)
        info = _pyd_wizard.pop(user_id, None)
        if not info:
            return
        _pending_dockerfile_choices.pop(info.get("chat_id"), None)
        try:
            await info["state"].clear()
        except Exception:
            pass
        try:
            await bot.send_message(
                info["chat_id"],
                "⏰ <b>/pydeploy timed out</b> — no activity for 2 minutes. "
                "Run /pydeploy again whenever you're ready.",
                parse_mode="HTML",
            )
        except Exception:
            pass

    task = asyncio.create_task(_waiter())
    _pyd_timeout_tasks[user_id] = task


def _pyd_wizard_done(user_id: int, chat_id: int):
    """Call when the wizard finishes normally (or is cancelled). Stops the timer."""
    _pyd_cancel_timeout(user_id)
    _pyd_wizard.pop(user_id, None)
    _pending_dockerfile_choices.pop(chat_id, None)


def _pyd_start_wizard(user_id: int, chat_id: int, state: FSMContext, bot):
    """Register a wizard session and start its inactivity timer."""
    _pyd_wizard[user_id] = {"chat_id": chat_id, "state": state}
    _pyd_schedule_timeout(user_id, bot)


# ── Wizard cancel callback ────────────────────────────────────────────────────

@pydeploy_router.callback_query(F.data == "pyd:wizard_cancel")
async def cb_pyd_wizard_cancel(call: CallbackQuery, state: FSMContext):
    if not _is_owner(call.from_user.id):
        await call.answer("Not authorized.", show_alert=True)
        return
    await call.answer()
    user_id = call.from_user.id
    _pyd_wizard_done(user_id, call.message.chat.id)
    await state.clear()
    try:
        await call.message.edit_text(
            "❌ <b>/pydeploy cancelled.</b>\nRun /pydeploy again whenever you're ready.",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await delete_after(call.message, delay=8)


# ── /pydeploy ────────────────────────────────────────────────────────────────

@pydeploy_router.message(Command("pydeploy"))
@require_2fa
async def cmd_pydeploy(message: Message, state: FSMContext):
    args = message.text.split(maxsplit=2)

    if len(args) < 2:
        await delete_command(message)
        await state.set_state(PyDeployStates.waiting_for_source)
        _pyd_start_wizard(message.from_user.id, message.chat.id, state, message.bot)
        await message.answer(
            "🐍 <b>Deploy a Python project</b>\n\n"
            "Send me either:\n"
            "• A GitHub repo URL (public, or private with a token embedded in the URL)\n"
            "• A <code>.zip</code> / <code>.rar</code> file of your project\n\n"
            "<i>Tip: you can also run</i> <code>/pydeploy &lt;url&gt; [entry_file.py]</code> <i>directly.</i>\n\n"
            "No Docker required — plain Python files, an optional "
            "<code>requirements.txt</code>, and/or a <code>setup.py</code>/<code>pyproject.toml</code> only.\n\n"
            "⏰ <i>Auto-cancelled after 2 min of inactivity.</i>",
            parse_mode="HTML",
            reply_markup=_CANCEL_KB,
        )
        return

    source = args[1].strip()
    entry_override = args[2].strip() if len(args) > 2 else None

    if not (source.startswith("http://") or source.startswith("https://")):
        await delete_command(message)
        reply = await message.answer(
            "⚠️ Only GitHub-style URLs are supported as a direct argument.\n"
            "To deploy from a <code>.zip</code>/<code>.rar</code>, run <code>/pydeploy</code> with no arguments "
            "and then upload the file.",
            parse_mode="HTML"
        )
        await delete_after(reply, delay=15)
        return

    await delete_command(message)
    await _run_git_deploy(message, source, entry_override)


@pydeploy_router.message(PyDeployStates.waiting_for_source, F.document)
async def cmd_pydeploy_receive_archive(message: Message, state: FSMContext):
    _pyd_wizard_done(message.from_user.id, message.chat.id)
    await state.clear()
    doc = message.document
    filename = doc.file_name or "upload.zip"
    if not filename.lower().endswith((".zip", ".rar")):
        reply = await message.answer("⚠️ Only <code>.zip</code> or <code>.rar</code> files are supported.", parse_mode="HTML")
        await delete_after(reply, delay=10)
        return

    status = await message.answer(f"📥 Receiving <code>{_html.escape(filename)}</code>...", parse_mode="HTML")

    try:
        tg_file = await message.bot.get_file(doc.file_id)
        file_bytes = (await message.bot.download_file(tg_file.file_path)).read()
    except Exception as e:
        await status.edit_text(f"❌ Failed to download file from Telegram:\n<code>{_html.escape(str(e))}</code>", parse_mode="HTML")
        return

    async def _update(text: str):
        try:
            await status.edit_text(text, parse_mode="HTML")
        except Exception:
            pass

    result = await deploy_from_archive(file_bytes, filename, None, _update)
    await _report_result(message.chat.id, status, result)


@pydeploy_router.message(PyDeployStates.waiting_for_source, F.text)
async def cmd_pydeploy_receive_url(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    # Let the cancel callback handle "cancel" taps; this handler is for text.
    if not (text.startswith("http://") or text.startswith("https://")):
        reply = await message.answer("⚠️ That doesn't look like a URL. Send a valid GitHub URL or tap ❌ Cancel.", parse_mode="HTML")
        await delete_after(reply, delay=10)
        return
    _pyd_wizard_done(message.from_user.id, message.chat.id)
    await state.clear()
    await delete_command(message)
    await _run_git_deploy(message, text, None)


async def _run_git_deploy(message: Message, repo_url: str, entry_override: str | None):
    status = await message.answer("🚀 <b>Starting Python deployment...</b>", parse_mode="HTML")

    async def _update(text: str):
        try:
            await status.edit_text(text, parse_mode="HTML")
        except Exception:
            pass

    result = await deploy_from_git(repo_url, entry_override, _update)
    await _report_result(message.chat.id, status, result)


async def _report_result(chat_id: int, status_msg: Message, result: dict):
    if result.get("needs_dockerfile_choice"):
        _pending_dockerfile_choices[chat_id] = {
            "name": result["name"],
            "path": result["path"],
            "source_type": result["source_type"],
            "source": result["source"],
            "entry_point_override": result.get("entry_point_override"),
            "existing_id": result.get("existing_id"),
        }
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🐍 Continue as Python app", callback_data="pyd:dfconfirm")]
        ])
        try:
            await status_msg.edit_text(result["message"], parse_mode="HTML", reply_markup=keyboard)
        except Exception:
            await status_msg.answer(result["message"], parse_mode="HTML", reply_markup=keyboard)
        return

    try:
        if result.get("ok"):
            await status_msg.edit_text(result["message"], parse_mode="HTML")
            await delete_after(status_msg, delay=90)
        else:
            await status_msg.edit_text(f"❌ {result.get('message', 'Deployment failed.')}", parse_mode="HTML")
    except Exception:
        try:
            await status_msg.answer(f"❌ {result.get('message', 'Deployment failed, and the status message could not be updated.')}", parse_mode="HTML")
        except Exception:
            pass


# ── Dockerfile-detected choice: continue as plain Python app ─────────────────

@pydeploy_router.callback_query(F.data == "pyd:dfconfirm")
async def cb_pyd_dockerfile_confirm(call: CallbackQuery, state: FSMContext):
    if not _is_owner(call.from_user.id):
        await call.answer("Not authorized.", show_alert=True)
        return
    await call.answer()
    chat_id = call.message.chat.id
    if chat_id not in _pending_dockerfile_choices:
        await call.message.edit_text("⚠️ This deployment prompt expired. Please run /pydeploy again.")
        return

    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    user_id = call.from_user.id
    _pyd_start_wizard(user_id, call.message.chat.id, state, call.bot)
    await state.set_state(PyDeployStates.awaiting_build_command)
    await call.message.answer(
        "🛠️ <b>Step 1 of 3 — Build command</b> (optional)\n\n"
        "Send a shell command to run before starting, e.g.:\n"
        "  <code>pip install -r requirements.txt</code>\n\n"
        "Send <code>skip</code> or just press the button to use the default "
        "(auto-install <code>requirements.txt</code> / <code>setup.py</code>).",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Skip this step", callback_data="pyd:skip_build")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="pyd:wizard_cancel")],
        ]),
    )


def _norm_skip(text: str) -> str | None:
    text = (text or "").strip()
    if not text or text.lower() == "skip":
        return None
    return text


async def _send_run_cmd_prompt(target, reset_user_id: int, bot):
    """Send the 'run command' prompt. Used by both text handler and skip callback."""
    _pyd_schedule_timeout(reset_user_id, bot)
    await target.answer(
        "▶️ <b>Step 2 of 3 — Run command</b> (optional)\n\n"
        "Send the command that starts your app, e.g.:\n"
        "  <code>python bot.py</code>  or  <code>gunicorn app:app</code>\n\n"
        "Send <code>skip</code> or press the button to auto-detect "
        "(<code>main.py</code> / <code>app.py</code> / <code>bot.py</code> / etc.).",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Skip this step", callback_data="pyd:skip_run")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="pyd:wizard_cancel")],
        ]),
    )


async def _send_env_prompt(target, reset_user_id: int, bot):
    """Send the '.env file' prompt. Used by both text handler and skip callback."""
    _pyd_schedule_timeout(reset_user_id, bot)
    await target.answer(
        "🔐 <b>Step 3 of 3 — .env file</b> (optional)\n\n"
        "Paste the full contents of your <code>.env</code> file, or send <code>skip</code> to leave it empty.\n"
        "<i>Your message will be deleted immediately after this step for security.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Skip this step", callback_data="pyd:skip_env")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="pyd:wizard_cancel")],
        ]),
    )


@pydeploy_router.message(PyDeployStates.awaiting_build_command, F.text)
async def cmd_pyd_receive_build_command(message: Message, state: FSMContext):
    build_command = _norm_skip(message.text)
    await delete_command(message)
    await state.update_data(build_command=build_command)
    await state.set_state(PyDeployStates.awaiting_run_command)
    await _send_run_cmd_prompt(message, message.from_user.id, message.bot)


@pydeploy_router.callback_query(F.data == "pyd:skip_build")
async def cb_pyd_skip_build(call: CallbackQuery, state: FSMContext):
    if not _is_owner(call.from_user.id):
        await call.answer("Not authorized.", show_alert=True)
        return
    await call.answer("Build command skipped.")
    await state.update_data(build_command=None)
    await state.set_state(PyDeployStates.awaiting_run_command)
    await _send_run_cmd_prompt(call.message, call.from_user.id, call.bot)


@pydeploy_router.message(PyDeployStates.awaiting_run_command, F.text)
async def cmd_pyd_receive_run_command(message: Message, state: FSMContext):
    run_command = _norm_skip(message.text)
    await delete_command(message)
    await state.update_data(run_command=run_command)
    await state.set_state(PyDeployStates.awaiting_env_content)
    await _send_env_prompt(message, message.from_user.id, message.bot)


@pydeploy_router.callback_query(F.data == "pyd:skip_run")
async def cb_pyd_skip_run(call: CallbackQuery, state: FSMContext):
    if not _is_owner(call.from_user.id):
        await call.answer("Not authorized.", show_alert=True)
        return
    await call.answer("Run command skipped.")
    await state.update_data(run_command=None)
    await state.set_state(PyDeployStates.awaiting_env_content)
    await _send_env_prompt(call.message, call.from_user.id, call.bot)


@pydeploy_router.callback_query(F.data == "pyd:skip_env")
async def cb_pyd_skip_env(call: CallbackQuery, state: FSMContext):
    if not _is_owner(call.from_user.id):
        await call.answer("Not authorized.", show_alert=True)
        return
    await call.answer()
    # Treat as if user typed "skip" for env — synthesise the env-content flow
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    pending = _pending_dockerfile_choices.get(chat_id)
    data = await state.get_data()
    _pyd_wizard_done(user_id, chat_id)
    await state.clear()
    if not pending:
        await call.message.answer("⚠️ Deployment prompt expired. Please run /pydeploy again.")
        return
    status = await call.message.answer("🚀 <b>Continuing deployment as a plain Python app...</b>", parse_mode="HTML")
    async def _update(text: str):
        try:
            await status.edit_text(text, parse_mode="HTML")
        except Exception:
            pass
    result = await continue_provision(
        name=pending["name"], path=pending["path"], source_type=pending["source_type"],
        source=pending["source"], entry_point_override=pending.get("entry_point_override"),
        existing_id=pending.get("existing_id"), update_cb=_update,
        build_command=data.get("build_command"), run_command=data.get("run_command"), env_content=None,
    )
    await _report_result(chat_id, status, result)


@pydeploy_router.message(PyDeployStates.awaiting_env_content, F.text)
async def cmd_pyd_receive_env_content(message: Message, state: FSMContext):
    env_content = _norm_skip(message.text)
    await delete_command(message)

    user_id = message.from_user.id
    chat_id = message.chat.id
    pending = _pending_dockerfile_choices.get(chat_id)
    data = await state.get_data()
    _pyd_wizard_done(user_id, chat_id)
    await state.clear()

    if not pending:
        await message.answer("⚠️ This deployment prompt expired. Please run /pydeploy again.")
        return

    build_command = data.get("build_command")
    run_command = data.get("run_command")

    status = await message.answer("🚀 <b>Continuing deployment as a plain Python app...</b>", parse_mode="HTML")

    async def _update(text: str):
        try:
            await status.edit_text(text, parse_mode="HTML")
        except Exception:
            pass

    result = await continue_provision(
        name=pending["name"], path=pending["path"], source_type=pending["source_type"],
        source=pending["source"], entry_point_override=pending.get("entry_point_override"),
        existing_id=pending.get("existing_id"), update_cb=_update,
        build_command=build_command, run_command=run_command, env_content=env_content,
    )
    await _report_result(chat_id, status, result)


# ── /pyps ─────────────────────────────────────────────────────────────────────

def _list_text_and_keyboard():
    deployments = list_deployments()
    if not deployments:
        return None, None
    lines = ["🐍 <b>Python Deployments</b>\n"]
    buttons = []
    for dep in deployments:
        emoji = _STATUS_EMOJI.get(dep["last_status"], "⚪")
        lines.append(f"{emoji} <code>{_html.escape(dep['name'])}</code> — {dep['last_status']}")
        buttons.append([InlineKeyboardButton(text=f"⚙️ {dep['name']}", callback_data=f"pyd:menu:{dep['id']}")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@pydeploy_router.message(Command("pyps"))
@require_2fa
async def cmd_pyps(message: Message):
    await delete_command(message)
    text, keyboard = _list_text_and_keyboard()
    if not text:
        reply = await message.answer("📭 <b>No Python deployments yet.</b>\nUse /pydeploy to create one.", parse_mode="HTML")
        await delete_after(reply, delay=15)
        return
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


def _detail_text(dep: dict) -> str:
    emoji = _STATUS_EMOJI.get(dep["last_status"], "⚪")
    lines = [
        f"{emoji} <b>{_html.escape(dep['name'])}</b>\n",
        f"<b>Status:</b> {_html.escape(dep['last_status'])}",
        f"<b>Source:</b> <code>{_html.escape(dep['source_type'])}</code>",
        f"<b>Path:</b> <code>{_html.escape(dep['path'])}</code>",
    ]
    if dep.get("run_command"):
        lines.append(f"<b>Run command:</b> <code>{_html.escape(dep['run_command'])}</code>")
    else:
        lines.append(f"<b>Entry point:</b> <code>{_html.escape(dep.get('entry_point') or '—')}</code>")
    if dep.get("build_command"):
        lines.append(f"<b>Build command:</b> <code>{_html.escape(dep['build_command'])}</code>")
    lines.append(f"<b>PID:</b> <code>{dep.get('pid') or '—'}</code>")
    lines.append(f"<b>Auto-restart:</b> {'✅ on' if dep.get('desired_state') == 'running' else '⏸️ off'}")
    return "\n".join(lines)


def _detail_keyboard(dep: dict) -> InlineKeyboardMarkup:
    dep_id = dep["id"]
    rows = [
        [
            InlineKeyboardButton(text="▶️ Start", callback_data=f"pyd:start:{dep_id}"),
            InlineKeyboardButton(text="⏹️ Stop", callback_data=f"pyd:stop:{dep_id}"),
            InlineKeyboardButton(text="🔄 Restart", callback_data=f"pyd:restart:{dep_id}"),
        ],
        [InlineKeyboardButton(text="📄 Logs", callback_data=f"pyd:logs:{dep_id}")],
    ]
    update_row = []
    if dep.get("source_type") == "github":
        update_row.append(InlineKeyboardButton(text="⬇️ Git Pull Update", callback_data=f"pyd:gitpull:{dep_id}"))
    update_row.append(InlineKeyboardButton(text="📤 Upload New Version", callback_data=f"pyd:uploadprompt:{dep_id}"))
    rows.append(update_row)
    rows.append([InlineKeyboardButton(text="🗑️ Delete", callback_data=f"pyd:delete:{dep_id}")])
    rows.append([InlineKeyboardButton(text="◀ Back to list", callback_data="pyd:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _extract_dep_id(data: str) -> int | None:
    try:
        return int(data.split(":")[2])
    except (IndexError, ValueError):
        return None


@pydeploy_router.callback_query(F.data.startswith("pyd:menu:"))
async def cb_pyd_menu(call: CallbackQuery):
    if not _is_owner(call.from_user.id):
        await call.answer("Not authorized.", show_alert=True)
        return
    await call.answer()
    dep_id = _extract_dep_id(call.data)
    dep = get_deployment(dep_id) if dep_id is not None else None
    if not dep:
        await call.message.edit_text("❌ Deployment not found (it may have been removed).")
        return
    await call.message.edit_text(_detail_text(dep), parse_mode="HTML", reply_markup=_detail_keyboard(dep))


@pydeploy_router.callback_query(F.data == "pyd:back")
async def cb_pyd_back(call: CallbackQuery):
    if not _is_owner(call.from_user.id):
        await call.answer("Not authorized.", show_alert=True)
        return
    await call.answer()
    text, keyboard = _list_text_and_keyboard()
    if not text:
        await call.message.edit_text("📭 No Python deployments left.")
        return
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)


async def _handle_action(call: CallbackQuery, action_fn, verb: str):
    if not _is_owner(call.from_user.id):
        await call.answer("Not authorized.", show_alert=True)
        return
    await call.answer()
    dep_id = _extract_dep_id(call.data)
    dep = get_deployment(dep_id) if dep_id is not None else None
    if not dep:
        await call.message.edit_text("❌ Deployment not found.")
        return

    try:
        await call.message.edit_text(f"⏳ {verb} <code>{_html.escape(dep['name'])}</code>...", parse_mode="HTML")
    except Exception:
        pass

    try:
        ok, msg = await action_fn(dep)
    except Exception as e:
        ok, msg = False, f"Unexpected error: {_html.escape(str(e))}"

    dep = get_deployment(dep_id) or dep
    text = f"{'✅' if ok else '❌'} {msg}\n\n" + _detail_text(dep)
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=_detail_keyboard(dep))
    except Exception:
        try:
            await call.message.answer(text, parse_mode="HTML", reply_markup=_detail_keyboard(dep))
        except Exception:
            pass


@pydeploy_router.callback_query(F.data.startswith("pyd:gitpull:"))
async def cb_pyd_gitpull(call: CallbackQuery):
    if not _is_owner(call.from_user.id):
        await call.answer("Not authorized.", show_alert=True)
        return
    await call.answer()
    dep_id = _extract_dep_id(call.data)
    dep = get_deployment(dep_id) if dep_id is not None else None
    if not dep:
        await call.message.edit_text("❌ Deployment not found.")
        return

    async def _update(text: str):
        try:
            await call.message.edit_text(text, parse_mode="HTML")
        except Exception:
            pass

    await _update(f"🔄 <b>Updating <code>{_html.escape(dep['name'])}</code> from Git...</b>")
    try:
        result = await update_from_git(dep, _update)
    except Exception as e:
        result = {"ok": False, "message": f"Unexpected error: {_html.escape(str(e))}"}

    dep = get_deployment(dep_id) or dep
    prefix = "✅" if result.get("ok") else "❌"
    text = f"{prefix} {result.get('message', 'Update finished.')}\n\n" + _detail_text(dep)
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=_detail_keyboard(dep))
    except Exception:
        try:
            await call.message.answer(text, parse_mode="HTML", reply_markup=_detail_keyboard(dep))
        except Exception:
            pass


@pydeploy_router.callback_query(F.data.startswith("pyd:uploadprompt:"))
async def cb_pyd_upload_prompt(call: CallbackQuery, state: FSMContext):
    if not _is_owner(call.from_user.id):
        await call.answer("Not authorized.", show_alert=True)
        return
    await call.answer()
    dep_id = _extract_dep_id(call.data)
    dep = get_deployment(dep_id) if dep_id is not None else None
    if not dep:
        await call.message.edit_text("❌ Deployment not found.")
        return

    await state.set_state(PyDeployStates.awaiting_update_archive)
    await state.update_data(update_dep_id=dep_id)
    _pyd_start_wizard(call.from_user.id, call.message.chat.id, state, call.bot)
    await call.message.answer(
        f"📤 Send a <code>.zip</code> or <code>.rar</code> file to replace the code for "
        f"<b>{_html.escape(dep['name'])}</b>.\n\n"
        "This wipes the existing code (keeping the virtual environment, logs, and any "
        "<code>.env</code> file) and rebuilds/restarts with the new contents.\n\n"
        "⏰ <i>Auto-cancelled after 2 min of inactivity.</i>",
        parse_mode="HTML",
        reply_markup=_CANCEL_KB,
    )


@pydeploy_router.message(PyDeployStates.awaiting_update_archive, F.document)
async def cmd_pyd_receive_update_archive(message: Message, state: FSMContext):
    _pyd_wizard_done(message.from_user.id, message.chat.id)
    data = await state.get_data()
    await state.clear()
    dep_id = data.get("update_dep_id")
    dep = get_deployment(dep_id) if dep_id is not None else None
    if not dep:
        await message.answer("❌ Deployment not found (it may have been removed). Please try again from /pyps.")
        return

    doc = message.document
    filename = doc.file_name or "upload.zip"
    if not filename.lower().endswith((".zip", ".rar")):
        reply = await message.answer("⚠️ Only <code>.zip</code> or <code>.rar</code> files are supported.", parse_mode="HTML")
        await delete_after(reply, delay=10)
        return

    status = await message.answer(f"📥 Receiving <code>{_html.escape(filename)}</code>...", parse_mode="HTML")
    try:
        tg_file = await message.bot.get_file(doc.file_id)
        file_bytes = (await message.bot.download_file(tg_file.file_path)).read()
    except Exception as e:
        await status.edit_text(f"❌ Failed to download file from Telegram:\n<code>{_html.escape(str(e))}</code>", parse_mode="HTML")
        return

    async def _update(text: str):
        try:
            await status.edit_text(text, parse_mode="HTML")
        except Exception:
            pass

    try:
        result = await update_from_archive(dep, file_bytes, filename, _update)
    except Exception as e:
        result = {"ok": False, "message": f"Unexpected error: {_html.escape(str(e))}"}

    dep = get_deployment(dep_id) or dep
    prefix = "✅" if result.get("ok") else "❌"
    text = f"{prefix} {result.get('message', 'Update finished.')}\n\n" + _detail_text(dep)
    try:
        await status.edit_text(text, parse_mode="HTML", reply_markup=_detail_keyboard(dep))
    except Exception:
        try:
            await message.answer(text, parse_mode="HTML", reply_markup=_detail_keyboard(dep))
        except Exception:
            pass


@pydeploy_router.callback_query(F.data.startswith("pyd:delete:"))
async def cb_pyd_delete_prompt(call: CallbackQuery):
    if not _is_owner(call.from_user.id):
        await call.answer("Not authorized.", show_alert=True)
        return
    await call.answer()
    dep_id = _extract_dep_id(call.data)
    dep = get_deployment(dep_id) if dep_id is not None else None
    if not dep:
        await call.message.edit_text("❌ Deployment not found.")
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Yes, delete it", callback_data=f"pyd:delconfirm:{dep_id}"),
        InlineKeyboardButton(text="✖ Cancel", callback_data=f"pyd:delcancel:{dep_id}"),
    ]])
    await call.message.edit_text(
        f"⚠️ <b>Delete {_html.escape(dep['name'])}?</b>\n\n"
        "This stops the process and permanently removes its files, virtual environment, and logs "
        "from the host. This cannot be undone.",
        parse_mode="HTML", reply_markup=keyboard
    )


@pydeploy_router.callback_query(F.data.startswith("pyd:delcancel:"))
async def cb_pyd_delete_cancel(call: CallbackQuery):
    if not _is_owner(call.from_user.id):
        await call.answer("Not authorized.", show_alert=True)
        return
    await call.answer("Cancelled.")
    dep_id = _extract_dep_id(call.data)
    dep = get_deployment(dep_id) if dep_id is not None else None
    if not dep:
        await call.message.edit_text("❌ Deployment not found.")
        return
    await call.message.edit_text(_detail_text(dep), parse_mode="HTML", reply_markup=_detail_keyboard(dep))


@pydeploy_router.callback_query(F.data.startswith("pyd:delconfirm:"))
async def cb_pyd_delete_confirm(call: CallbackQuery):
    if not _is_owner(call.from_user.id):
        await call.answer("Not authorized.", show_alert=True)
        return
    await call.answer()
    dep_id = _extract_dep_id(call.data)
    dep = get_deployment(dep_id) if dep_id is not None else None
    if not dep:
        await call.message.edit_text("❌ Deployment not found (it may already be deleted).")
        return

    try:
        await call.message.edit_text(f"⏳ Deleting <code>{_html.escape(dep['name'])}</code>...", parse_mode="HTML")
    except Exception:
        pass

    try:
        ok, msg = await delete_deployment(dep)
    except Exception as e:
        ok, msg = False, f"Unexpected error: {_html.escape(str(e))}"

    text = f"{'✅' if ok else '❌'} {msg}"
    try:
        await call.message.edit_text(text, parse_mode="HTML")
    except Exception:
        try:
            await call.message.answer(text, parse_mode="HTML")
        except Exception:
            pass
    if ok:
        await delete_after(call.message, delay=15)


async def _manual_start(dep: dict):
    ok, msg = await start_deployment(dep)
    if ok:
        update_deployment(dep["id"], desired_state="running")
    return ok, msg


@pydeploy_router.callback_query(F.data.startswith("pyd:start:"))
async def cb_pyd_start(call: CallbackQuery):
    await _handle_action(call, _manual_start, "Starting")


@pydeploy_router.callback_query(F.data.startswith("pyd:stop:"))
async def cb_pyd_stop(call: CallbackQuery):
    await _handle_action(call, stop_deployment, "Stopping")


@pydeploy_router.callback_query(F.data.startswith("pyd:restart:"))
async def cb_pyd_restart(call: CallbackQuery):
    await _handle_action(call, restart_deployment, "Restarting")


@pydeploy_router.callback_query(F.data.startswith("pyd:logs:"))
async def cb_pyd_logs(call: CallbackQuery):
    if not _is_owner(call.from_user.id):
        await call.answer("Not authorized.", show_alert=True)
        return
    await call.answer()
    dep_id = _extract_dep_id(call.data)
    dep = get_deployment(dep_id) if dep_id is not None else None
    if not dep:
        await call.message.answer("❌ Deployment not found.")
        return

    try:
        logs = await get_logs(dep, lines=200)
    except Exception as e:
        logs = f"Could not read logs: {e}"

    safe_logs = _html.escape(logs)
    header = f"📄 <b>Logs — {_html.escape(dep['name'])}</b>\n\n"
    full_text = header + f"<pre>{safe_logs}</pre>"

    if len(full_text) <= 4096:
        await call.message.answer(full_text, parse_mode="HTML")
        return

    tail = safe_logs[-3500:]
    if "\n" in tail:
        tail = tail[tail.index("\n") + 1:]
    truncated = header + "<i>⚠️ Showing the last 3500 characters. Full log attached below.</i>\n\n" + f"<pre>{tail}</pre>"
    await call.message.answer(truncated, parse_mode="HTML")

    raw_bytes = logs.encode("utf-8", errors="replace")
    if len(raw_bytes) > 10 * 1024 * 1024:
        raw_bytes = raw_bytes[:10 * 1024 * 1024] + b"\n[CAPPED AT 10MB]"
    doc = BufferedInputFile(raw_bytes, filename=f"{dep['name']}_log.txt")
    await call.message.answer_document(doc, caption=f"📄 Full log for {dep['name']}")
