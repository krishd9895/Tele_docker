import asyncio
import psutil
import shutil
import platform
import time
from services.docker_service import docker_engine

class InfrastructureMonitoringService:
    def __init__(self):
        self.boot_time = time.time()

    async def compile_system_health_dashboard(self) -> dict:
        loop = asyncio.get_running_loop()
        
        cpu_usage = await loop.run_in_executor(None, psutil.cpu_percent, 1.0)
        memory = await loop.run_in_executor(None, psutil.virtual_memory)
        total, used, free = await loop.run_in_executor(None, shutil.disk_usage, "/")
        
        try:
            running_containers = await docker_engine.get_running_containers()
            docker_status = f"🟢 Operational ({len(running_containers)} Active)"
        except Exception:
            docker_status = "🔴 Interrupted/Dead"

        is_wsl = "microsoft-standard" in platform.release().lower() or "wsl" in platform.uname().release.lower()
        wsl_status = "🟢 running (WSL Kernel)" if is_wsl else "ℹ️ Standard Host Node"

        uptime_seconds = int(time.time() - self.boot_time)
        hours, remainder = divmod(uptime_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)

        return {
            "cpu": f"{cpu_usage}%",
            "ram": f"{memory.percent}% (Used: {memory.used // (1024**2)}MB / Total: {memory.total // (1024**2)}MB)",
            "disk": f"{(used / total) * 100:.2f}% (Free: {free // (1024**3)}GB)",
            "docker": docker_status,
            "wsl": wsl_status,
            "uptime": f"{hours}h {minutes}m {seconds}s"
        }

monitor_service = InfrastructureMonitoringService()