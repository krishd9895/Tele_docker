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
import time
import threading
import queue
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


def ssh_exec(command: str, timeout: int = 60, input_data: str | None = None) -> tuple[int, str]:
    """
    Synchronous SSH command execution on the WSL host.
    Returns (exit_code, output_string).
    Output capped at SSH_OUTPUT_CAP bytes.

    If `input_data` is given, it's written to the remote command's stdin and
    the channel is then closed for writing (EOF) — this is what lets
    non-interactive commands like `sudo -S` read a password from stdin
    instead of needing a real TTY prompt.
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
            stdin, stdout, stderr = ssh.exec_command(command, timeout=timeout)

            if input_data is not None:
                try:
                    stdin.write(input_data)
                    stdin.flush()
                    stdin.channel.shutdown_write()
                except Exception:
                    pass

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


def sftp_read(file_path: str) -> bytes:
    """Read a file's bytes from the WSL host via SFTP."""
    import io
    user, password = get_ssh_creds()
    if not user or not password:
        raise RuntimeError("HOST_SSH_USER or HOST_SSH_PASSWORD not configured in .env")

    host, port = get_ssh_host()
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, port=port, username=user, password=password, timeout=15)
    try:
        sftp = ssh.open_sftp()
        buf = io.BytesIO()
        sftp.getfo(file_path, buf)
        sftp.close()
        return buf.getvalue()
    finally:
        ssh.close()


async def sftp_read_async(file_path: str) -> bytes:
    """Async wrapper around sftp_read."""
    return await asyncio.to_thread(sftp_read, file_path)


def sftp_read_many(file_paths: list[str]) -> dict[str, bytes | Exception]:
    """
    Read several files from the WSL host over a single SSH/SFTP session —
    cheaper than opening a new connection per file when a run produces
    multiple output files. Returns {file_path: bytes} on success, or
    {file_path: Exception} for any individual file that failed to read
    (missing/permission), so the caller can report partial success.
    """
    import io
    results: dict[str, bytes | Exception] = {}
    user, password = get_ssh_creds()
    if not user or not password:
        err = RuntimeError("HOST_SSH_USER or HOST_SSH_PASSWORD not configured in .env")
        return {p: err for p in file_paths}

    host, port = get_ssh_host()
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(host, port=port, username=user, password=password, timeout=15)
        sftp = ssh.open_sftp()
        try:
            for p in file_paths:
                try:
                    buf = io.BytesIO()
                    sftp.getfo(p, buf)
                    results[p] = buf.getvalue()
                except Exception as e:
                    results[p] = e
        finally:
            sftp.close()
    finally:
        ssh.close()
    return results


async def sftp_read_many_async(file_paths: list[str]) -> dict[str, bytes | Exception]:
    """Async wrapper around sftp_read_many."""
    return await asyncio.to_thread(sftp_read_many, file_paths)


# ── Streaming host execution (kill-verified) ────────────────────────────────
#
# Moved here from main.py so other features (e.g. services/runpy_service.py)
# can reuse the same battle-tested "verify the remote process is really dead
# before giving up" logic that /host uses, instead of re-implementing
# paramiko signal-escalation from scratch. main.py imports these under their
# original names for its own /host command.

def force_kill_remote(ssh: "paramiko.SSHClient", process_pid: str | None) -> None:
    """
    Forcefully and *verifiably* terminate the remote process tree.

    Escalates INT (same as Ctrl+C) → TERM → KILL, re-checking liveness with
    `kill -0` between each signal, and blocks (via a single follow-up
    exec_command whose exit status we actually wait on) until the process is
    confirmed dead or all attempts are exhausted. This is deliberately
    synchronous/blocking so the caller can be sure the remote side is really
    gone before it closes the SSH connection — a fire-and-forget signal was
    the reason commands like a bare `ping` kept running after "Stop" was
    clicked.
    """
    if not process_pid:
        return
    verify_kill_cmd = (
        f'pkill -INT -P {process_pid} 2>/dev/null; '
        f'pkill -TERM -P {process_pid} 2>/dev/null; '
        f'for sig in INT TERM KILL; do '
        f'  kill -s $sig {process_pid} 2>/dev/null; '
        f'  for i in 1 2 3 4 5; do '
        f'    kill -0 {process_pid} 2>/dev/null || exit 0; '
        f'    sleep 0.2; '
        f'  done; '
        f'done; '
        f'kill -0 {process_pid} 2>/dev/null && exit 1 || exit 0'
    )
    try:
        _, kill_stdout, _ = ssh.exec_command(verify_kill_cmd, timeout=15)
        kill_stdout.channel.recv_exit_status()  # block until the escalation above finishes
    except Exception:
        pass


def exec_streaming(
    command: str,
    cwd: str | None = None,
    stop_event: "threading.Event" = None,
    output_line_queue: "queue.Queue" = None,
) -> tuple[int, str, bool]:
    """
    Run a command on the host with streaming output, capturing its PID so it
    can be verifiably killed. Returns (exit_code, output, was_stopped).

    `stop_event` is required (not optional despite the default) — callers
    must pass a real threading.Event, either set by a UI "Stop" button or by
    a background timer for timeout enforcement. `output_line_queue` is
    optional; when given, output lines are also pushed there for live
    progress updates.
    """
    user, password = get_ssh_creds()
    if not user or not password:
        return -1, "HOST_SSH_USER or HOST_SSH_PASSWORD not configured in .env", False

    host, port = get_ssh_host()
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    full_cmd = command
    if cwd:
        full_cmd = f'cd "{cwd}" && {command}'
    # Wrap command to get PID and allow killing it
    wrapped_cmd = f"sh -c 'echo $; exec {full_cmd}'"
    output_lines = []
    was_stopped = False
    exit_code = -1
    process_pid = None

    try:
        ssh.connect(host, port=port, username=user, password=password, timeout=15)
        stdin, stdout, stderr = ssh.exec_command(wrapped_cmd)

        first_line = True
        while True:
            # Check stop event FIRST every time and kill immediately
            if stop_event.is_set():
                was_stopped = True
                try:
                    stdin.write(chr(3))
                    stdin.flush()
                except Exception:
                    pass
                force_kill_remote(ssh, process_pid)
                try:
                    if stdout.channel.recv_ready():
                        chunk = stdout.read(4096)
                        if chunk:
                            text = chunk.decode(errors="ignore")
                            output_lines.append(text)
                            if output_line_queue:
                                output_line_queue.put(text)
                except Exception:
                    pass
                break

            if stdout.channel.recv_ready():
                chunk = stdout.read(4096)
                if chunk:
                    text = chunk.decode(errors="ignore")
                    lines = text.splitlines(True)
                    for line in lines:
                        if first_line and line.strip().isdigit():
                            process_pid = line.strip()
                            first_line = False
                            continue
                        first_line = False
                        output_lines.append(line)
                        if output_line_queue:
                            output_line_queue.put(line)
            if stderr.channel.recv_ready():
                chunk = stderr.read(4096)
                if chunk:
                    text = chunk.decode(errors="ignore")
                    lines = text.splitlines(True)
                    output_lines.extend(lines)
                    if output_line_queue:
                        for line in lines:
                            output_line_queue.put(line)
            if stdout.channel.exit_status_ready():
                exit_code = stdout.channel.recv_exit_status()
                remaining_stdout = stdout.read()
                if remaining_stdout:
                    remaining_lines = remaining_stdout.decode(errors="ignore").splitlines(True)
                    output_lines.extend(remaining_lines)
                    if output_line_queue:
                        for line in remaining_lines:
                            output_line_queue.put(line)
                remaining_stderr = stderr.read()
                if remaining_stderr:
                    remaining_lines = remaining_stderr.decode(errors="ignore").splitlines(True)
                    output_lines.extend(remaining_lines)
                    if output_line_queue:
                        for line in remaining_lines:
                            output_line_queue.put(line)
                break
            time.sleep(0.01)
    except Exception as e:
        error_line = f"\nError: {e}"
        output_lines.append(error_line)
        if output_line_queue:
            output_line_queue.put(error_line)
    finally:
        try:
            ssh.close()
        except Exception:
            pass

    return exit_code, "".join(output_lines), was_stopped
