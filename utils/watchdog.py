import asyncio
import shutil
import psutil
import docker
import logging
from aiogram import Bot

class ResiliencyWatchdogEngine:
    def __init__(self, bot: Bot, owner_id: int):
        self.bot = bot
        self.owner_id = owner_id
        self.loop_active = True

    async def initialize_sentinel_loop(self):
        logging.info("Initializing background orchestration system watchdogs...")
        while self.loop_active:
            try:
                total, used, free = shutil.disk_usage("/")
                used_percentage = (used / total) * 100
                if used_percentage > 80.0:
                    await self.bot.send_message(
                        self.owner_id, 
                        f"🚨 <b>CRITICAL SYSTEM ALERT:</b> Disk consumption high! Current state: {used_percentage:.2f}%", 
                        parse_mode="HTML"
                    )

                memory = psutil.virtual_memory()
                if memory.percent > 90.0:
                    await self.bot.send_message(
                        self.owner_id,
                        f"🚨 <b>CRITICAL SYSTEM ALERT:</b> Host experiencing RAM starvation! Current usage: {memory.percent}%",
                        parse_mode="HTML"
                    )

                try:
                    client = docker.from_env()
                    client.ping()
                except Exception:
                    await self.bot.send_message(
                        self.owner_id,
                        "🚨 <b>CRITICAL SYSTEM ALERT:</b> Docker Engine daemon socket connection down!",
                        parse_mode="HTML"
                    )
            except Exception as global_watchdog_fault:
                logging.error(f"Internal monitoring loop exception: {global_watchdog_fault}")
            
            await asyncio.sleep(60)