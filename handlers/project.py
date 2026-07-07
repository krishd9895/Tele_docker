import os
import asyncio
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from services.git_service import git_engine
from services.docker_service import docker_engine
from utils.queue import global_execution_queue

project_router = Router()

class DeploymentStates(StatesGroup):
    awaiting_token = State()

@project_router.message(Command("deploy"))
async def cmd_deploy(message: Message, state: FSMContext):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("⚠️ Usage: <code>/deploy &lt;git_repo_url&gt;</code>", parse_mode="HTML")
        return

    repo_url = args[1]
    status_msg = await message.answer("🚀 <b>Deployment Core Initiated...</b>", parse_mode="HTML")

    async def deployment_task(token: str = None):
        try:
            analysis = await git_engine.process_deployment(repo_url, status_msg.edit_text, token=token)
            project_name = analysis["project_name"]
            project_type = analysis["type"]
            repo_path = analysis["repo_path"]
            manifest_file = analysis.get("manifest") or "compose.yaml"

            if "Docker Compose" in project_type:
                await status_msg.edit_text(
                    f"📋 <b>Multi-Container Compose Stack Detected</b>\n\n"
                    f"<b>Project:</b> {project_name}\n"
                    f"<b>Manifest:</b> {manifest_file}\n\n"
                    f"🔄 <i>Step 3/4: Compiling image arrays and bringing services up...</i>",
                    parse_mode="HTML"
                )
                success = await docker_engine.deploy_compose_sandbox(repo_path, manifest_name=manifest_file)
            else:
                await status_msg.edit_text(
                    f"📋 <b>Dockerfile Blueprint Detected</b>\n\n"
                    f"<b>Project:</b> {project_name}\n\n"
                    f"🔄 <i>Step 3/4: Running docker build and starting container...</i>",
                    parse_mode="HTML"
                )
                success = await docker_engine.deploy_sandbox_verify(
                    image_tag=f"{project_name.lower()}:latest", 
                    target_port=80, 
                    host_port=8080
                )

            if success:
                await status_msg.edit_text(
                    f"✅ <b>Deployment Successful</b>\n\n"
                    f"<b>Project Workspace:</b> <code>{project_name}</code>\n"
                    f"<b>Infrastructure Class:</b> {project_type}\n"
                    f"<b>Status:</b> 🟢 Active / Running in Background",
                    parse_mode="HTML"
                )
            else:
                await status_msg.edit_text("❌ <b>Deployment Failed:</b> Docker engine dropped out unexpectedly.")
        
        except Exception as err:
            err_str = str(err)
            if "Authentication failed" in err_str or "could not read Username" in err_str or "Repository not found" in err_str:
                await status_msg.edit_text(
                    "🔐 <b>Private Repository Detected / Access Locked</b>\n\n"
                    "This repository appears to be private or requires explicit authorization.\n"
                    "Please reply to this message providing your <b>GitHub Personal Access Token (PAT)</b> to confirm ownership.\n\n"
                    "⏱️ <i>Operation will cancel automatically if no token is provided.</i>", 
                    parse_mode="HTML"
                )
                await state.update_data(repo_url=repo_url, status_msg_id=status_msg.message_id)
                await state.set_state(DeploymentStates.awaiting_token)
            else:
                import html as _html
                await status_msg.edit_text(
                    f"❌ <b>Deployment Aborted due to Engine Error:</b>\n\n"
                    f"<code>{_html.escape(err_str)}</code>",
                    parse_mode="HTML"
                )

    try:
        await global_execution_queue.execute(deployment_task())
    except Exception as err:
        await status_msg.edit_text(f"❌ <b>Pipeline Failure Interruption:</b> {err}")


@project_router.message(DeploymentStates.awaiting_token)
async def capture_github_token(message: Message, state: FSMContext):
    state_data = await state.get_data()
    repo_url = state_data.get("repo_url")
    token = message.text.strip()
    
    try: await message.delete()
    except Exception: pass
    
    await message.answer("🔑 <i>Token received. Processing private deployment thread...</i>", parse_mode="HTML")
    
    async def private_deployment_task():
        try:
            analysis = await git_engine.process_deployment(repo_url, lambda text: message.answer(text), token=token)
            project_name = analysis["project_name"]
            project_type = analysis["type"]
            repo_path = analysis["repo_path"]
            manifest_file = analysis.get("manifest") or "compose.yaml"
            
            await message.answer(f"⚙️ Running authenticated build for <b>{project_name}</b>...", parse_mode="HTML")
            
            if "Docker Compose" in project_type:
                success = await docker_engine.deploy_compose_sandbox(repo_path, manifest_name=manifest_file)
            else:
                success = await docker_engine.deploy_sandbox_verify(f"{project_name.lower()}:latest", 80, 8080)
            
            if success:
                await message.answer(f"✅ <b>Private Deployment Successful!</b>\nProject <code>{project_name}</code> is now running.", parse_mode="HTML")
            else:
                await message.answer("❌ <b>Private Deployment Failed:</b> Initialization dropped out.")
        except Exception as err:
            await message.answer(f"❌ <b>Operation Aborted due to Engine Error:</b>\n\n<code>{str(err)}</code>", parse_mode="HTML")
        finally:
            await state.clear()

    await global_execution_queue.execute(private_deployment_task())


@project_router.message(Command("gitclone"))
async def cmd_gitclone(message: Message):
    """
    Usage:
      /gitclone <repo_url>
      /gitclone <repo_url> <dest_path>
    dest_path is optional — defaults to data/workspaces/<repo_name>
    """
    args = message.text.split(maxsplit=2)
    if len(args) < 2:
        await message.answer(
            "⚠️ <b>Usage:</b>\n"
            "<code>/gitclone &lt;repo_url&gt; [dest_path]</code>\n\n"
            "Examples:\n"
            "• <code>/gitclone https://github.com/user/repo</code>\n"
            "• <code>/gitclone https://github.com/user/repo /home/user/projects/myapp</code>",
            parse_mode="HTML"
        )
        return

    repo_url = args[1]
    dest_path = args[2] if len(args) == 3 else None

    status_msg = await message.answer(
        f"📥 <b>Cloning...</b>\n<code>{repo_url}</code>",
        parse_mode="HTML"
    )

    success, result = await git_engine.clone(repo_url, dest_path=dest_path)

    if success:
        await status_msg.edit_text(
            f"✅ <b>Clone Successful</b>\n\n"
            f"<b>Repo:</b> <code>{repo_url}</code>\n"
            f"<b>Location:</b> <code>{result}</code>",
            parse_mode="HTML"
        )
    else:
        await status_msg.edit_text(
            f"❌ <b>Clone Failed</b>\n\n{result}",
            parse_mode="HTML"
        )


@project_router.message(Command("gitpull"))
async def cmd_gitpull(message: Message):
    """
    /gitpull          — shows all cloned repos as buttons, tap to pull
    /gitpull <path>   — pulls a specific path directly
    """
    args = message.text.split(maxsplit=1)

    # Direct path mode
    if len(args) == 2:
        repo_path = args[1].strip()
        await _do_pull(message, repo_path)
        return

    # List mode — show all repos as inline buttons
    repos = await git_engine.list_repos()

    if not repos:
        await message.answer(
            "📭 <b>No cloned repositories found</b> in <code>data/workspaces</code>.\n\n"
            "Use /gitclone to clone a repo first.",
            parse_mode="HTML"
        )
        return

    buttons = []
    for repo in repos:
        label = f"📁 {repo['name']}"
        buttons.append([InlineKeyboardButton(
            text=label,
            callback_data=f"gitpull:{repo['path']}"
        )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    lines = ["🔄 <b>Select a repo to pull:</b>\n"]
    for i, repo in enumerate(repos, 1):
        loc = "🖥️ host" if repo.get("location") == "host" else "🐳 container"
        lines.append(f"{i}. <code>{repo['name']}</code>  <i>({loc})</i>")
        if repo["remote"]:
            lines.append(f"   🔗 {repo['remote']}")
        lines.append(f"   📂 <code>{repo['path']}</code>\n")

    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=keyboard)


@project_router.callback_query(F.data.startswith("gitpull:"))
async def callback_gitpull(call: CallbackQuery):
    repo_path = call.data.split("gitpull:", 1)[1]
    await call.message.edit_reply_markup(reply_markup=None)  # remove buttons
    await _do_pull(call.message, repo_path)


async def _do_pull(message: Message, repo_path: str):
    status_msg = await message.answer(
        f"🔄 <b>Pulling latest changes...</b>\n<code>{repo_path}</code>",
        parse_mode="HTML"
    )
    success, result = await git_engine.pull(repo_path)
    if success:
        await status_msg.edit_text(
            f"✅ <b>Pull Successful</b>\n\n"
            f"<b>Path:</b> <code>{repo_path}</code>\n"
            f"<b>Result:</b>\n<pre>{result}</pre>",
            parse_mode="HTML"
        )
    else:
        await status_msg.edit_text(
            f"❌ <b>Pull Failed</b>\n\n{result}",
            parse_mode="HTML"
        )


@project_router.message(Command("deploy_raw"))
async def cmd_deploy_raw(message: Message):
    args = message.text.replace("/deploy_raw", "").strip().split()
    if not args:
        await message.answer(
            "⚠️ <b>Usage Format:</b>\n"
            "<code>/deploy_raw &lt;image&gt; &lt;host_port&gt; &lt;container_port&gt;</code>\n"
            "Example: <code>/deploy_raw nginx:alpine 8081 80</code>",
            parse_mode="HTML"
        )
        return

    image_name = args[0]
    host_port = int(args[1]) if len(args) > 1 else 8080
    container_port = int(args[2]) if len(args) > 2 else 80
    container_name = f"raw_{image_name.split(':')[0]}_{host_port}"

    status_msg = await message.answer(f"🐳 <b>Local Engine:</b> Pulling <code>{image_name}</code> directly from DockerHub...")
    
    try:
        import docker
        client = docker.from_env()
        await asyncio.to_thread(client.images.pull, image_name)
        
        await status_msg.edit_text("⚙️ <b>Local Engine:</b> Initializing container lifecycle parameters...")
        container = await asyncio.to_thread(
            client.containers.run,
            image=image_name,
            name=container_name,
            detach=True,
            ports={f'{container_port}/tcp': host_port},
            restart_policy={"Name": "always"}
        )
        
        await status_msg.edit_text(
            f"🚀 <b>Raw Image Deployment Successful!</b>\n\n"
            f"• <b>Container Name:</b> <code>{container.name}</code>\n"
            f"• <b>Image Vector:</b> <code>{image_name}</code>\n"
            f"• <b>Network Mapping:</b> Port <code>{host_port}</code> ➔ Container <code>{container_port}</code>\n"
            f"• <b>Status:</b> 🟢 Operational",
            parse_mode="HTML"
        )
    except Exception as e:
        await status_msg.edit_text(f"❌ <b>Local Deployment Failed:</b>\n<code>{str(e)}</code>", parse_mode="HTML")