import asyncio
import os
import logging
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import Message
import paramiko

from config.settings import runtime_settings
from database.models import init_db
from middlewares.auth import HardenedAuthMiddleware
from middlewares.totp_auth import require_2fa

from handlers.docker_cmds import docker_router
from handlers.shell import shell_router
from handlers.project import project_router
from handlers.file_manager import file_router
from handlers.auth_cmds import auth_router
from handlers.help_cmds import help_router
from handlers.tunnel_cmds import tunnel_router
from handlers.duckdns_cmds import duckdns_router
from handlers.env_cmds import env_router
from services.duckdns_service import duckdns_service

from utils.watchdog import ResiliencyWatchdogEngine

# --- Setup System Logging Profiles ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()]
)

# --- Define Hidden Host Execution Core Logic ---
def execute_on_host_machine(command: str) -> str:
    """Tunnels structural system commands to the host OS via SSH loopback interface."""
    try:
        # Load credentials directly out of the environment layout stream
        ssh_user = os.getenv("HOST_SSH_USER")
        ssh_pass = os.getenv("HOST_SSH_PASSWORD", "YOUR_UBUNTU_PASSWORD")
        
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        # Connect natively to host stack via host-networked localhost
        ssh.connect('127.0.0.1', username=ssh_user, password=ssh_pass)
        
        stdin, stdout, stderr = ssh.exec_command(command)
        output = stdout.read().decode(errors='ignore') + stderr.read().decode(errors='ignore')
        ssh.close()
        
        return output if output.strip() else "[Command executed with empty output]"
    except Exception as e:
        return f"❌ Host Bridge Error: {str(e)}"

async def main():
    logging.info("Validating configuration matrices and initializing SQLite metadata layers...")
    init_db()

    # Load and apply mutable settings from MongoDB or settings.json
    from utils.env_manager import load_mutable_settings, apply_to_runtime
    mutable = load_mutable_settings()
    if mutable:
        apply_to_runtime(mutable)
        logging.info(f"Applied {len(mutable)} mutable settings from persistent store.")

    # Kill any orphaned cloudflared / auth proxy processes from previous bot runs
    logging.info("Cleaning up orphaned tunnel processes from previous session...")
    from services.tunnel_service import _ssh_exec as _tun_ssh
    await asyncio.to_thread(
        _tun_ssh,
        "pkill -f 'cloudflared tunnel' 2>/dev/null; "
        "pkill -f 'tele_auth_proxy' 2>/dev/null; "
        "echo cleaned",
        30
    )

    # Start DuckDNS auto-updater if configured
    if runtime_settings.DUCKDNS_TOKEN and runtime_settings.DUCKDNS_DOMAIN:
        asyncio.create_task(
            duckdns_service.start_auto_updater(runtime_settings.DUCKDNS_UPDATE_INTERVAL)
        )
        logging.info(f"DuckDNS auto-updater scheduled every {runtime_settings.DUCKDNS_UPDATE_INTERVAL}s")

    os.makedirs("/app/data", exist_ok=True)
    health_marker = "/app/data/healthy"
    with open(health_marker, "w") as f:
        f.write("OK")

    bot = Bot(token=runtime_settings.TELEGRAM_BOT_TOKEN)
    dp = Dispatcher()

    # Apply global authentication safeguards
    dp.message.outer_middleware(HardenedAuthMiddleware())

    # Create a dedicated inline router for hidden administrative tasks
    hidden_host_router = Router()

    @hidden_host_router.message(Command("host"))
    @require_2fa
    async def handle_hidden_host_execution(message: Message):
        """Processes /host inputs completely under the radar (Not registered in BotFather Help Menu)"""
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            await message.reply("⚠️ Missing command execution payload.")
            return

        target_cmd = args[1]
        status_update = await message.reply("⏳ Routing command string directly to Host PC core...")

        result_payload = await asyncio.to_thread(execute_on_host_machine, target_cmd)

        await status_update.delete()
        # Trim if too long
        trimmed = result_payload[-3800:] if len(result_payload) > 3800 else result_payload
        await message.reply(f"```\n{trimmed}\n```", parse_mode="Markdown")

    # --- Attach Routers to the Dispatcher Engine ---
    # Register the hidden router first so it has immediate structural evaluation priority
    dp.include_router(hidden_host_router)
    
    # Standard imported functional routers
    dp.include_router(auth_router)
    dp.include_router(docker_router)
    dp.include_router(shell_router)
    dp.include_router(project_router)
    dp.include_router(file_router)
    dp.include_router(tunnel_router)
    dp.include_router(duckdns_router)
    dp.include_router(env_router)
    dp.include_router(help_router)

    sentinel = ResiliencyWatchdogEngine(bot, runtime_settings.ALLOWED_USER_ID)
    asyncio.create_task(sentinel.initialize_sentinel_loop())

    logging.info("Starting safe long-polling system link sequences with Telegram APIs...")
    try:
        await bot.send_message(
            runtime_settings.ALLOWED_USER_ID, 
            "🚀 <b>System Bootstrapped Successfully.</b> System online and monitoring containers.", 
            parse_mode="HTML"
        )
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        if os.path.exists(health_marker):
            os.remove(health_marker)
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("System gracefully exiting operational execution cycles.")