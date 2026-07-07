"""
Centralized SSH credential helper.

HOST_SSH_USER and HOST_SSH_PASSWORD are sensitive — they live in .env only,
never in MongoDB or settings.json. This helper reads them safely with
multiple fallbacks:
  1. runtime_settings.HOST_SSH_USER  (loaded from .env via pydantic-settings)
  2. os.getenv("HOST_SSH_USER")      (direct OS env — works if load_dotenv() ran)
  3. Returns None if neither is set
"""

import os
import asyncio
import logging
import paramiko
from typing import Callable

logger = logging.getLogger(__name__)

# Hard cap on SSH output to prevent RAM exhaustion
SSH_OUTPUT_CAP = 512 * 1024  # 512 KB


def get_ssh_creds() -> tuple[str | None, str | None]:
    """Return (user, password) from runtime_settings or os.getenv fallback."""
    user = None
    password = None

    # Try runtime_settings first
    try:
        from config.settings import runtime_settings
        user = getattr(runtime_settings, "HOST_SSH_USER", None)
        password = getattr(runtime_settings, "HOST_SSH_PASSWORD", None)
    except Exception:
        pass

    # Fall back to direct env vars if settings didn't have them
    if not user:
        user = os.getenv("HOST_SSH_USER")
    if not password:
        password = os.getenv("HOST_SSH_PASSWORD")

    return user, password


def get_ssh_host() -> tuple[str, int]:
    """Return (host, port) for SSH connection."""
    host = "127.0.0.1"
    port = 22
    try:
        from config.settings import runtime_settings
        host = getattr(runtime_settings, "HOST_SSH_HOST", "127.0.0.1") or "127.0.0.1"
        port = int(getattr(runtime_settings, "HOST_SSH_PORT", 22) or 22)
    except Exception:
        pass
    return host, port


def ssh_exec(command: str, timeout: int = 60) -> tuple[int, str]:
    """
    Synchronous SSH command execution on the WSL host.
    Returns (exit_code, output_string).
    Output capped at SSH_OUTPUT_CAP bytes.
    """
    import threading
    import queue

    user, password = get_ssh_creds()
    if not user or not password:
        return -1, "HOST_SSH_USER or HOST_SSH_PASSWORD not configured in .env"

    host, port = get_ssh_host()
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    result_queue = queue.Queue()
    
    def _execute():
        try:
            ssh.connect(host, port=port, username=user, password=password, timeout=15)
            _, stdout, stderr = ssh.exec_command(command, timeout=timeout)
            
            # Read with size cap
            stdout_bytes = b""
            for chunk in iter(lambda: stdout.read(4096), b""):
                stdout_bytes += chunk
                if len(stdout_bytes) >= SSH_OUTPUT_CAP:
                    stdout_bytes = stdout_bytes[:SSH_OUTPUT_CAP]
                    stdout_bytes += b"\n[OUTPUT CAPPED AT 512KB]"
                    break
            
            stderr_bytes = stderr.read(min(4096, SSH_OUTPUT_CAP))
            output = stdout_bytes.decode(errors="ignore") + stderr_bytes.decode(errors="ignore")
            
            # Try to get exit code
            try:
                exit_code = stdout.channel.recv_exit_status()
            except:
                exit_code = -1
            
            result_queue.put((exit_code, output))
        except Exception as e:
            result_queue.put((-1, str(e)))
        finally:
            try:
                ssh.close()
            except:
                pass
    
    thread = threading.Thread(target=_execute, daemon=True)
    thread.start()
    
    try:
        exit_code, output = result_queue.get(timeout=timeout)
        return exit_code, output
    except queue.Empty:
        # Timeout!
        try:
            ssh.close()
        except:
            pass
        return -1, f"Command timed out after {timeout} seconds"



async def ssh_exec_async(command: str, timeout: int = 60) -> tuple[int, str]:
    """Async wrapper around ssh_exec."""
    return await asyncio.to_thread(ssh_exec, command, timeout)


def sftp_write(dest_dir: str, filename: str, data: bytes) -> str:
    """
    Write bytes to a file on the WSL host via SFTP.
    Creates dest_dir if it doesn't exist.
    Handles name collision by appending _1, _2, etc.
    Returns the final destination path.
    """
    import io
    user, password = get_ssh_creds()
    if not user or not password:
        raise RuntimeError("HOST_SSH_USER or HOST_SSH_PASSWORD not configured in .env")

    host, port = get_ssh_host()
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, port=port, username=user, password=password, timeout=15)
    try:
        # Create directory
        ssh.exec_command(f'mkdir -p "{dest_dir}"')[1].channel.recv_exit_status()
        sftp = ssh.open_sftp()

        dest_path = f"{dest_dir.rstrip('/')}/{filename}"
        # Handle name collision
        try:
            sftp.stat(dest_path)
            base, ext = os.path.splitext(filename)
            counter = 1
            while True:
                new_name = f"{base}_{counter}{ext}"
                dest_path = f"{dest_dir.rstrip('/')}/{new_name}"
                try:
                    sftp.stat(dest_path)
                    counter += 1
                except FileNotFoundError:
                    break
        except FileNotFoundError:
            pass  # file doesn't exist yet — good

        sftp.putfo(io.BytesIO(data), dest_path)
        sftp.close()
        return dest_path
    finally:
        ssh.close()


async def sftp_write_async(dest_dir: str, filename: str, data: bytes) -> str:
    """Async wrapper around sftp_write."""
    return await asyncio.to_thread(sftp_write, dest_dir, filename, data)
