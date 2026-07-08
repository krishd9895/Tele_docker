import asyncio
import psutil
import platform
import time
import json
from services.docker_service import docker_engine
from utils.ssh_helper import ssh_exec_async


class InfrastructureMonitoringService:
    def __init__(self):
        self.boot_time = time.time()

    async def compile_system_health_dashboard(self) -> dict:
        loop = asyncio.get_running_loop()
        
        # CPU
        cpu_percent = await loop.run_in_executor(None, psutil.cpu_percent, 1.0)
        cpu_cores = psutil.cpu_count(logical=True)
        try:
            cpu_freq = psutil.cpu_freq().current if psutil.cpu_freq() else "N/A"
        except:
            cpu_freq = "N/A"
        try:
            load_avg = psutil.getloadavg() if hasattr(psutil, 'getloadavg') else ("N/A", "N/A", "N/A")
        except:
            load_avg = ("N/A", "N/A", "N/A")

        # Memory
        memory = await loop.run_in_executor(None, psutil.virtual_memory)
        try:
            swap = await loop.run_in_executor(None, psutil.swap_memory)
        except:
            swap = None

        # Disks — get from host via SSH first, fall back to container-local
        disks = []
        try:
            # Run a Python script on the host to get disk info
            host_script = '''
import psutil
import json
disks = []
for part in psutil.disk_partitions():
    skip_mounts = ['/proc', '/sys', '/dev', '/tmp', '/run', '/var/run', '/var/lib/docker']
    skip_fstypes = ['tmpfs', 'devtmpfs', 'devpts', 'sysfs', 'proc', 'cgroup', 'overlay', 'aufs', 'shm', 'mqueue']
    if part.mountpoint in skip_mounts:
        continue
    if part.fstype in skip_fstypes:
        continue
    if part.mountpoint.startswith(('/proc/', '/sys/', '/dev/')):
        continue
    try:
        usage = psutil.disk_usage(part.mountpoint)
        disks.append({
            "mountpoint": part.mountpoint,
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "percent": (usage.used / usage.total) * 100
        })
    except:
        pass
print(json.dumps(disks))
'''.strip()
            # Escape single quotes for the shell command
            host_script_escaped = host_script.replace("'", "'\"'\"'")
            exit_code, output = await ssh_exec_async(f"python3 -c '{host_script_escaped}'", timeout=30)
            if exit_code == 0:
                disks = json.loads(output.strip())
        except Exception:
            # If SSH fails, try container-local disks (filtered)
            try:
                for part in psutil.disk_partitions():
                    skip_mounts = [
                        "/app/.env", "/app/data", "/etc/resolv.conf", "/etc/hostname",
                        "/etc/hosts", "/proc", "/sys", "/dev", "/tmp", "/run",
                        "/var/run", "/var/lib/docker"
                    ]
                    skip_fstypes = [
                        "tmpfs", "devtmpfs", "devpts", "sysfs", "proc", "cgroup",
                        "overlay", "aufs", "shm", "mqueue"
                    ]
                    if part.mountpoint in skip_mounts:
                        continue
                    if part.fstype in skip_fstypes:
                        continue
                    if part.mountpoint.startswith(("/proc/", "/sys/", "/dev/")):
                        continue
                    try:
                        usage = psutil.disk_usage(part.mountpoint)
                        disks.append({
                            "mountpoint": part.mountpoint,
                            "total": usage.total,
                            "used": usage.used,
                            "free": usage.free,
                            "percent": (usage.used / usage.total) * 100
                        })
                    except:
                        pass
            except:
                pass

        # Network
        net_io = None
        try:
            net_io = await loop.run_in_executor(None, psutil.net_io_counters)
        except:
            pass
        interfaces = []
        try:
            if_addrs = psutil.net_if_addrs()
            interfaces = list(if_addrs.keys())
        except:
            pass

        # Docker
        docker_running = []
        docker_status = "🔴 Interrupted/Dead"
        try:
            running_containers = await docker_engine.get_running_containers()
            docker_status = f"🟢 Operational ({len(running_containers)} Active)"
            docker_running = [c.name for c in running_containers]
        except Exception:
            pass

        # System info
        is_wsl = "microsoft-standard" in platform.release().lower() or "wsl" in platform.uname().release.lower()
        wsl_status = "🟢 running (WSL Kernel)" if is_wsl else "ℹ️ Standard Host Node"
        os_name = f"{platform.system()} {platform.release()}"
        try:
            system_uptime_seconds = int(time.time() - psutil.boot_time())
        except:
            system_uptime_seconds = 0
        sys_h, sys_r = divmod(system_uptime_seconds, 3600)
        sys_m, sys_s = divmod(sys_r, 60)
        system_uptime_str = f"{sys_h}h {sys_m}m {sys_s}s"
        try:
            process_count = len(psutil.pids())
        except:
            process_count = 0

        # Bot uptime
        bot_uptime_seconds = int(time.time() - self.boot_time)
        bot_h, bot_r = divmod(bot_uptime_seconds, 3600)
        bot_m, bot_s = divmod(bot_r, 60)
        bot_uptime_str = f"{bot_h}h {bot_m}m {bot_s}s"

        # Format memory values
        def format_bytes(b):
            if b >= 1024**3:
                return f"{b / (1024**3):.1f} GB"
            elif b >= 1024**2:
                return f"{b / (1024**2):.1f} MB"
            elif b >= 1024:
                return f"{b / 1024:.1f} KB"
            else:
                return f"{b} B"

        ram_used = format_bytes(memory.used)
        ram_total = format_bytes(memory.total)
        
        if swap:
            swap_used = format_bytes(swap.used)
            swap_total = format_bytes(swap.total)
            swap_percent = f"{swap.percent}%"
        else:
            swap_used = "N/A"
            swap_total = "N/A"
            swap_percent = "N/A"

        # Network formatting
        net_sent = format_bytes(net_io.bytes_sent) if net_io else "N/A"
        net_recv = format_bytes(net_io.bytes_recv) if net_io else "N/A"

        return {
            "cpu": {
                "usage": f"{cpu_percent}%",
                "cores": cpu_cores,
                "freq": f"{cpu_freq} MHz" if cpu_freq != "N/A" else cpu_freq,
                "load_avg": f"{load_avg[0]:.2f}, {load_avg[1]:.2f}, {load_avg[2]:.2f}" if load_avg[0] != "N/A" else "N/A"
            },
            "ram": {
                "percent": f"{memory.percent}%",
                "used": ram_used,
                "total": ram_total
            },
            "swap": {
                "percent": swap_percent,
                "used": swap_used,
                "total": swap_total
            },
            "disks": disks,
            "network": {
                "bytes_sent": net_sent,
                "bytes_recv": net_recv,
                "interfaces": interfaces
            },
            "docker": {
                "status": docker_status,
                "running_containers": docker_running
            },
            "system": {
                "os": os_name,
                "wsl": wsl_status,
                "uptime": system_uptime_str,
                "processes": process_count
            },
            "bot": {
                "uptime": bot_uptime_str
            }
        }


monitor_service = InfrastructureMonitoringService()
