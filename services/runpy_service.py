"""
RunPyEngine — /runpy: run an uploaded Python script (plus optional input
files like a spreadsheet or PDF) on the WSL host, exactly like opening a
folder in PyCharm and hitting Run, then hand back the console output and any
new/modified files, and finally delete everything from the host.

Everything happens on the HOST machine over SSH/SFTP (utils/ssh_helper.py),
the same way every other host-facing feature in this bot works (/host,
/pydeploy, /gitclone). Each run gets its own throwaway folder under
PY_RUN_ROOT which is always removed afterwards — success, failure, or
timeout — so nothing lingers on disk.

Pipeline:
  1. create_session() — makes a fresh, uniquely-named folder on the host.
  2. upload_input_file() — SFTPs each uploaded file into that folder as the
     user sends it.
  3. run_script() —
       a. if requirements.txt was uploaded, create a throwaway virtualenv
          and pip-install it (best-effort; falls back to the host's system
          python3 if that fails, and says so in the output);
       b. snapshots the folder's file metadata;
       c. runs the chosen entry file with cwd = the session folder, so
          relative paths in the script (open("data.xlsx"), etc.) resolve
          exactly like they would in a PyCharm project folder;
       d. enforces PY_RUN_TIMEOUT by force-killing the process tree via
          utils.ssh_helper.exec_streaming/force_kill_remote — the same
          verified kill-escalation /host's Stop button uses, so a runaway
          script never lingers as an orphaned host process;
       e. re-snapshots the folder and diffs against (b) to find every file
          that's new or changed size/mtime — that's what gets sent back.
  4. download_output_files() — reads those changed files back over SFTP.
  5. cleanup_session() — `rm -rf` the whole session folder on the host.
"""

import logging
import re
import threading
import time
import uuid

from utils.ssh_helper import (
    get_ssh_creds,
    ssh_exec_async,
    sftp_write_async,
    sftp_read_many_async,
    exec_streaming,
)

logger = logging.getLogger(__name__)

# Directories/patterns never reported back as "changed" — they're either the
# throwaway venv this module creates itself, or Python's own bytecode cache.
_IGNORED_DIR_PARTS = (".venv/", "__pycache__/", ".git/")
_IGNORED_SUFFIXES = (".pyc",)

_host_home_cache: str | None = None


def _shq(s: str) -> str:
    """Single-quote a string safely for embedding in a shell command."""
    return "'" + (s or "").replace("'", "'\\''") + "'"


async def _get_host_home() -> str:
    global _host_home_cache
    if _host_home_cache:
        return _host_home_cache
    try:
        code, out = await ssh_exec_async("echo $HOME", timeout=15)
        if code == 0 and out.strip():
            _host_home_cache = out.strip().splitlines()[-1].strip()
        else:
            _host_home_cache = "/root"
    except Exception:
        _host_home_cache = "/root"
    return _host_home_cache


async def _get_run_root() -> str:
    from config.settings import runtime_settings
    home = await _get_host_home()
    sub = getattr(runtime_settings, "PY_RUN_ROOT", "py_runs") or "py_runs"
    if sub.startswith("/"):
        return sub.rstrip("/")
    return f"{home.rstrip('/')}/{sub.strip('/')}"


def _get_run_timeout() -> int:
    from config.settings import runtime_settings
    try:
        return int(getattr(runtime_settings, "PY_RUN_TIMEOUT", 300) or 300)
    except Exception:
        return 300


def _is_ignored(rel_path: str) -> bool:
    """Defense-in-depth on top of the `find` filters below — belt and suspenders."""
    if rel_path.endswith(_IGNORED_SUFFIXES):
        return True
    for part in _IGNORED_DIR_PARTS:
        if rel_path.startswith(part) or f"/{part}" in rel_path:
            return True
    return False


def is_configured() -> bool:
    user, password = get_ssh_creds()
    return bool(user and password)


async def create_session(user_id: int) -> str:
    """Create a fresh, uniquely-named session folder on the host and return its path."""
    run_root = await _get_run_root()
    session_id = f"run_{user_id}_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    session_dir = f"{run_root}/{session_id}"
    code, out = await ssh_exec_async(f'mkdir -p "{session_dir}"', timeout=15)
    if code != 0:
        raise RuntimeError(f"Could not create session folder on host: {out.strip()[:300]}")
    return session_dir


async def upload_input_file(session_dir: str, filename: str, data: bytes) -> str:
    """Write an uploaded file into the session folder (no collision renaming — a fresh session folder is always empty of that name)."""
    return await sftp_write_async(session_dir, filename, data)


async def cleanup_session(session_dir: str) -> None:
    """Best-effort recursive delete of the whole session folder on the host."""
    if not session_dir:
        return
    try:
        await ssh_exec_async(f'rm -rf {_shq(session_dir)}', timeout=30)
    except Exception:
        logger.exception(f"Failed to clean up run session folder {session_dir}")


async def _snapshot(session_dir: str) -> dict[str, tuple[int, str]]:
    """Return {relative_path: (size, mtime)} for every real file in session_dir, ignoring venv/cache noise."""
    cmd = (
        f'cd {_shq(session_dir)} 2>/dev/null && '
        f'find . -type f '
        f'-not -path "./.venv/*" -not -path "./__pycache__/*" -not -path "*/__pycache__/*" '
        f'-not -name "*.pyc" -not -path "./.git/*" '
        f'-printf "%P\\t%s\\t%T@\\n"'
    )
    code, out = await ssh_exec_async(cmd, timeout=30)
    snap: dict[str, tuple[int, str]] = {}
    if code != 0:
        return snap
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        rel, size, mtime = parts
        rel = rel.strip()
        if not rel or _is_ignored(rel):
            continue
        snap[rel] = (size.strip(), mtime.strip())
    return snap


def _diff_changed(before: dict, after: dict) -> list[str]:
    changed = []
    for rel, meta in after.items():
        if rel not in before or before[rel] != meta:
            changed.append(rel)
    return sorted(changed)


async def _maybe_build(
    session_dir: str,
    build_cmd: str | None,
    has_requirements: bool,
    log: list[str],
) -> str | None:
    """
    If the user supplied a build command OR a requirements.txt was uploaded,
    create an isolated venv and run the appropriate install step.

    Priority:
      1. User-supplied build_cmd (e.g. "pip install requests pandas")
      2. Auto-detected requirements.txt

    Returns the venv path on success, or None to fall back to system python3.
    """
    if not build_cmd and not has_requirements:
        return None

    venv_path = f"{session_dir}/.venv"
    log.append("🔧 Creating isolated virtual environment…")
    code, out = await ssh_exec_async(f'python3 -m venv {_shq(venv_path)} 2>&1', timeout=120)
    if code != 0:
        log.append(f"⚠️ Could not create venv, falling back to system python3:\n{out.strip()[-800:]}")
        return None

    venv_pip = venv_path + "/bin/pip"

    if build_cmd:
        # Map bare "pip" / "pip3" to the venv's pip so packages land in the venv.
        cmd = build_cmd.strip()
        if cmd.startswith("pip3 "):
            cmd = f'{_shq(venv_pip)} {cmd[5:]}'
        elif cmd.startswith("pip "):
            cmd = f'{_shq(venv_pip)} {cmd[4:]}'
        # else run the command as-is (e.g. a custom script, apt-get, etc.)

        log.append(f"🔧 Running build command: {build_cmd}")
        full_cmd = f'cd {_shq(session_dir)} && {cmd} 2>&1'
        code, out = await ssh_exec_async(full_cmd, timeout=300)
        if code != 0:
            log.append(
                f"⚠️ Build command exited with code {code} — "
                f"falling back to system python3:\n{out.strip()[-1200:]}"
            )
            return None
        log.append("✅ Build command completed.")
    else:
        # Auto-install requirements.txt
        log.append("📦 Detected requirements.txt — installing dependencies…")
        code, out = await ssh_exec_async(
            f'{_shq(venv_pip)} install --no-input --disable-pip-version-check '
            f'-r {_shq(session_dir + "/requirements.txt")} 2>&1',
            timeout=300,
        )
        if code != 0:
            log.append(f"⚠️ pip install failed, falling back to system python3:\n{out.strip()[-1200:]}")
            return None
        log.append("✅ Dependencies installed.")

    return venv_path


async def run_script(
    session_dir: str,
    entry_file: str,
    build_cmd: str | None = None,
    has_requirements: bool = False,
) -> dict:
    """
    Run entry_file (relative to session_dir) with cwd=session_dir.

    Returns a dict:
      {
        "exit_code": int,
        "output": str,           # combined stdout+stderr
        "timed_out": bool,
        "setup_log": list[str],  # venv/pip messages, if any
        "changed_files": list[str],  # relative paths, new or modified
      }
    """
    setup_log: list[str] = []
    venv_path = await _maybe_build(session_dir, build_cmd, has_requirements, setup_log)

    before = await _snapshot(session_dir)

    python_bin = f'{venv_path}/bin/python' if venv_path else "python3"
    run_cmd = f'{python_bin} {_shq(entry_file)} 2>&1'

    stop_event = threading.Event()
    timeout = _get_run_timeout()
    timer = threading.Timer(timeout, stop_event.set)
    timer.daemon = True
    timer.start()
    try:
        exit_code, output, was_stopped = await _to_thread_exec(run_cmd, session_dir, stop_event)
    finally:
        timer.cancel()

    after = await _snapshot(session_dir)
    changed = _diff_changed(before, after)

    return {
        "exit_code": exit_code,
        "output": output,
        "timed_out": was_stopped,
        "setup_log": setup_log,
        "changed_files": changed,
    }


async def _to_thread_exec(command: str, cwd: str, stop_event: threading.Event):
    import asyncio
    return await asyncio.to_thread(exec_streaming, command, cwd, stop_event, None)


async def download_output_files(session_dir: str, relative_paths: list[str]) -> dict[str, bytes]:
    """Read the given relative paths (from a run's changed_files) back from the host. Skips any that fail to read."""
    if not relative_paths:
        return {}
    full_paths = [f"{session_dir}/{p}" for p in relative_paths]
    raw = await sftp_read_many_async(full_paths)
    result: dict[str, bytes] = {}
    for rel, full in zip(relative_paths, full_paths):
        val = raw.get(full)
        if isinstance(val, bytes):
            result[rel] = val
    return result


def list_py_files(filenames: list[str]) -> list[str]:
    return [f for f in filenames if f.lower().endswith(".py")]
