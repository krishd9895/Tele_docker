import asyncio
import os
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
import docker

token = os.getenv('TELEGRAM_BOT_TOKEN', '')
allowed_id = int(os.getenv('ALLOWED_USER_ID', '0'))

bot = Bot(token=token)
dp = Dispatcher()
client = docker.from_env()

@dp.message(Command("docker_ps"))
async def rescue_ps(message: Message):
    if message.from_user.id != allowed_id: return
    containers = client.containers.list(all=True)
    res = "<b>🚨 Rescue Mode Node Tracker</b>\n\n"
    for c in containers:
        res += f"• <code>{c.name}</code> — <code>{c.status}</code>\n"
    await message.answer(res, parse_mode="HTML")

@dp.message(Command("docker_restart"))
async def rescue_restart(message: Message):
    if message.from_user.id != allowed_id: return
    args = message.text.split()
    if len(args) > 1:
        try:
            c = client.containers.get(args[1])
            c.restart()
            await message.answer(f"✅ Rescue instruction power-cycled: {args[1]}")
        except Exception as e:
            await message.answer(f"❌ Action failed: {str(e)}")

async def main():
    if not token or allowed_id == 0:
        print("Rescue bot skipped: Missing structural validation variables.")
        return
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())