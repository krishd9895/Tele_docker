"""
PythonProcessEngine — deploys plain Python projects (no Docker) onto the WSL
host and keeps them running 24/7.

Everything runs on the HOST machine over SSH/SFTP (utils/ssh_helper.py),
exactly like the rest of this bot's host-facing features (/host, /gitclone,
docker deployments). This module never touches the container's own
filesystem for the deployed app's code, and it never reads from or writes
to any existing table/file used by other features — it is fully additive.

Pipeline for a deployment:
  1. If the project contains a Dockerfile/compose file, don't refuse it —
     flag it back to the caller so the handler can ask the user to either
     use /deploy (full Docker support) or continue anyway with optional
     custom build/run commands and .env content (see `needs_dockerfile_choice`
     below and `confirmed_python` on `_provision`).
  2. Detect an entry point (main.py / app.py / bot.py / run.py / server.py,
     or an explicit override) — skipped if a custom run_command is supplied.
  3. Run a custom build_command if given; otherwise install requirements.txt
     (or `pip install -e .` if there's a setup.py/pyproject.toml and no
     requirements.txt).
  4. Launch the app in the background via a tiny wrapper script. With no
     custom run_command it `exec`s into the venv's python directly, so the
     recorded PID always matches the real running process; a custom
     run_command is executed as-is (with the venv's bin/ prepended to PATH).
  5. Persist deployment metadata (database/pydeploy_models.py) so state
     survives bot restarts.

A background supervisor loop (see supervisor_loop) periodically checks that
every deployment whose desired_state is "running" is actually alive, and
restarts it if not — unless the user explicitly stopped it, or it is
crash-looping (in which case auto-restart pauses and the owner is notified).
"""

import asyncio
import html as _html
import logging
import re
import time

from utils.ssh_helper import get_ssh_creds, ssh_exec_async, sftp_write_async
from database.pydeploy_models import (
    create_deployment,
    update_deployment,
    get_deployment,
    get_deployment_by_name,
    list_deployments,
    delete_deployment as _db_delete_deployment,
)

logger = logging.getLogger(__name__)

_ENTRY_CANDIDATES = ["main.py", "app.py", "bot.py", "run.py", "server.py"]

_host_home_cache: str | None = None

# In-memory crash-loop tracking — resets on bot restart, which is fine since
# it's only meant to protect against rapid flapping within a single run.
_restart_history: dict[int, list[float]] = {}
_FLAP_WINDOW_SECONDS = 600   # 10 minutes
_FLAP_LIMIT = 5              # more than this many restarts in the window => pause


# ── small helpers ──────────────────────────────────────────────────────────

def _html_escape(s: str) -> str:
    return _html.escape(s or "")


def _shq(s: str) -> str:
    """Single-quote a string safely for embedding in a shell command."""
    return "'" + (s or "").replace("'", "'\\''") + "'"


def _redact_url(url: str) -> str:
    """Never echo embedded credentials/tokens back to the chat."""
    return re.sub(r'https://([^/@\s]+)@', 'https://<redacted>@', url or "")


def _sanitize_name(raw: str) -> str:
    raw = (raw or "").strip().lower()
    raw = re.sub(r'[^a-z0-9._-]+', '-', raw)
    raw = raw.strip('-.') or "app"
    return raw[:48]


def _derive_name_from_git_url(url: str) -> str:
    clean = url.rstrip("/").split("/")[-1]
    if clean.endswith(".git"):
        clean = clean[:-4]
    if "@" in clean:
        clean = clean.split("@")[-1]
    return _sanitize_name(clean or "repo")


def _derive_name_from_filename(filename: str) -> str:
    base = filename.rsplit(".", 1)[0] if "." in filename else filename
    return _sanitize_name(base or "app")


async def _unique_name(base: str) -> str:
    name = base
    i = 2
    while get_deployment_by_name(name):
        name = f"{base}-{i}"
        i += 1
    return name


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


async def _get_deploy_root() -> str:
    from config.settings import runtime_settings
    home = await _get_host_home()
    sub = getattr(runtime_settings, "PY_DEPLOY_ROOT", "py_deployments") or "py_deployments"
    if sub.startswith("/"):
        return sub.rstrip("/")
    return f"{home.rstrip('/')}/{sub.strip('/')}"


async def _kill_pid(pid: int):
    if not pid:
        return
    try:
        await ssh_exec_async(
            f'kill -TERM {pid} 2>/dev/null; sleep 1; '
            f'kill -0 {pid} 2>/dev/null && kill -9 {pid} 2>/dev/null; true',
            timeout=15
        )
    except Exception:
        pass


async def is_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        code, out = await ssh_exec_async(f'kill -0 {pid} 2>/dev/null && echo ALIVE || echo DEAD', timeout=10)
        return "ALIVE" in out
    except Exception:
        return False


async def _detect_entry_point(path: str, override: str | None) -> str | None:
    try:
        if override:
            code, out = await ssh_exec_async(f'test -f "{path}/{override}" && echo "{override}"', timeout=15)
            return override if code == 0 and override in out else None
        candidates = " ".join(_ENTRY_CANDIDATES)
        cmd = f'cd "{path}" 2>/dev/null && for f in {candidates}; do if [ -f "$f" ]; then echo "$f"; break; fi; done'
        code, out = await ssh_exec_async(cmd, timeout=15)
        lines = [l.strip() for l in out.strip().splitlines() if l.strip()]
        return lines[0] if lines else None
    except Exception:
        logger.exception("Entry point detection failed")
        return None


# ── deployment entry points ────────────────────────────────────────────────

async def deploy_from_git(
    repo_url: str, entry_point_override: str | None, update_cb,
    build_command: str | None = None, run_command: str | None = None,
    env_content: str | None = None, confirmed_python: bool = False,
) -> dict:
    try:
        user, password = get_ssh_creds()
        if not user or not password:
            return {"ok": False, "message": "Host SSH is not configured (HOST_SSH_USER / HOST_SSH_PASSWORD in .env)."}

        base_name = _derive_name_from_git_url(repo_url)
        existing = get_deployment_by_name(base_name)
        is_redeploy = bool(existing and existing.get("source_type") == "github" and existing.get("source") == repo_url)

        deploy_root = await _get_deploy_root()

        if is_redeploy:
            name = base_name
            path = existing["path"]
            await update_cb(f"📥 <b>Updating existing deployment</b> <code>{_html_escape(name)}</code>...")
            if existing.get("pid"):
                await _kill_pid(existing["pid"])
            code, out = await ssh_exec_async(f'cd "{path}" && GIT_TERMINAL_PROMPT=0 git pull 2>&1', timeout=180)
        else:
            name = base_name if not existing else await _unique_name(base_name)
            path = f"{deploy_root}/{name}"
            await update_cb(f"📥 <b>Cloning</b> <code>{_html_escape(_redact_url(repo_url))}</code>...")
            code, out = await ssh_exec_async(
                f'mkdir -p "{deploy_root}" && GIT_TERMINAL_PROMPT=0 git clone --depth 1 "{repo_url}" "{path}" 2>&1',
                timeout=180
            )

        if code != 0:
            safe_out = _html_escape(_redact_url(out))[-2000:]
            return {"ok": False, "message": f"Git operation failed:\n<pre>{safe_out}</pre>"}

        return await _provision(
            name=name, path=path, source_type="github", source=repo_url,
            entry_point_override=entry_point_override, update_cb=update_cb,
            existing=existing if is_redeploy else None,
            build_command=build_command, run_command=run_command, env_content=env_content,
            confirmed_python=confirmed_python,
        )
    except Exception as e:
        logger.exception("deploy_from_git failed")
        return {"ok": False, "message": f"Unexpected error during git deployment: {_html_escape(str(e))}"}


async def deploy_from_archive(
    file_bytes: bytes, filename: str, entry_point_override: str | None, update_cb,
    build_command: str | None = None, run_command: str | None = None,
    env_content: str | None = None, confirmed_python: bool = False,
) -> dict:
    try:
        user, password = get_ssh_creds()
        if not user or not password:
            return {"ok": False, "message": "Host SSH is not configured (HOST_SSH_USER / HOST_SSH_PASSWORD in .env)."}

        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in ("zip", "rar"):
            return {"ok": False, "message": "Only <code>.zip</code> or <code>.rar</code> archives are supported."}

        base_name = _derive_name_from_filename(filename)
        existing = get_deployment_by_name(base_name)
        name = base_name if not existing else await _unique_name(base_name)

        deploy_root = await _get_deploy_root()
        path = f"{deploy_root}/{name}"
        upload_dir = f"{deploy_root}/_uploads"

        await update_cb("📤 <b>Uploading archive to host...</b>")
        remote_archive = await sftp_write_async(upload_dir, filename, file_bytes)

        await update_cb("📦 <b>Extracting archive...</b>")
        await ssh_exec_async(f'mkdir -p "{path}"', timeout=15)

        if ext == "zip":
            code, out = await ssh_exec_async(f'unzip -o -q "{remote_archive}" -d "{path}" 2>&1', timeout=120)
        else:
            code, out = await ssh_exec_async(
                f'(command -v unrar >/dev/null 2>&1 && unrar x -o+ "{remote_archive}" "{path}/" 2>&1) || '
                f'(command -v 7z >/dev/null 2>&1 && 7z x -y -o"{path}" "{remote_archive}" 2>&1) || '
                f'echo NO_EXTRACTOR_AVAILABLE',
                timeout=120
            )
            if "NO_EXTRACTOR_AVAILABLE" in out:
                return {
                    "ok": False,
                    "message": (
                        "❌ Neither <code>unrar</code> nor <code>7z</code> is installed on the host.\n"
                        "Install one, e.g. <code>/host sudo apt install -y unrar</code>, then try again."
                    )
                }
        if code != 0:
            return {"ok": False, "message": f"Extraction failed:\n<pre>{_html_escape(out[-1500:])}</pre>"}

        # Flatten a single top-level wrapper folder (common with GitHub zip exports)
        try:
            await ssh_exec_async(
                f'cd "{path}" && entries=$(ls -A) && count=$(echo "$entries" | wc -l); '
                f'if [ "$count" = "1" ] && [ -d "$entries" ]; then '
                f'inner="$entries"; shopt -s dotglob; mv "$inner"/* . 2>/dev/null; rmdir "$inner" 2>/dev/null; fi',
                timeout=20
            )
        except Exception:
            pass

        return await _provision(
            name=name, path=path, source_type="archive", source=filename,
            entry_point_override=entry_point_override, update_cb=update_cb,
            existing=existing,
            build_command=build_command, run_command=run_command, env_content=env_content,
            confirmed_python=confirmed_python,
        )
    except Exception as e:
        logger.exception("deploy_from_archive failed")
        return {"ok": False, "message": f"Unexpected error during archive deployment: {_html_escape(str(e))}"}


async def _provision(name: str, path: str, source_type: str, source: str,
                      entry_point_override: str | None, update_cb, existing: dict | None,
                      build_command: str | None = None, run_command: str | None = None,
                      env_content: str | None = None, confirmed_python: bool = False) -> dict:
    try:
        docker_check_cmd = (
            f'cd "{path}" 2>/dev/null && '
            f'(test -f Dockerfile -o -f docker-compose.yml -o -f docker-compose.yaml '
            f'-o -f compose.yml -o -f compose.yaml && echo FOUND || echo CLEAR)'
        )
        code, out = await ssh_exec_async(docker_check_cmd, timeout=20)
        if code != 0:
            return {"ok": False, "message": f"Could not access <code>{_html_escape(path)}</code> on the host. Is HOST_SSH configured correctly?"}
        if "FOUND" in out and not confirmed_python:
            # Don't hard-reject — Dockerfile/compose projects can still be run
            # the plain-Python way if the user supplies (or explicitly skips)
            # a custom build command, run command, and .env content.
            return {
                "ok": False,
                "needs_dockerfile_choice": True,
                "name": name,
                "path": path,
                "source_type": source_type,
                "source": source,
                "entry_point_override": entry_point_override,
                "existing_id": existing["id"] if existing else None,
                "message": (
                    "🐳 <b>This project contains a Dockerfile/compose file.</b>\n\n"
                    "You can either:\n"
                    "• Use <b>/deploy</b> or <b>/compose_up</b> for full Docker support, or\n"
                    "• Continue here and I'll run it as a plain Python app — you can provide "
                    "a custom <b>build command</b>, <b>run command</b>, and <b>.env</b> content "
                    "(or skip any of them; skipping the run command falls back to "
                    "auto-detecting <code>main.py</code>/<code>app.py</code>/etc.)."
                )
            }

        entry_point = ""
        if not (run_command and run_command.strip()):
            await update_cb("🔎 <b>Detecting entry point...</b>")
            entry_point = await _detect_entry_point(path, entry_point_override)
            if not entry_point:
                return {
                    "ok": False,
                    "message": (
                        "⚠️ <b>Could not detect a Python entry point.</b>\n\n"
                        "None of <code>main.py</code>, <code>app.py</code>, <code>bot.py</code>, "
                        "<code>run.py</code>, <code>server.py</code> were found at the project root.\n\n"
                        "Retry specifying the file explicitly:\n"
                        "<code>/pydeploy &lt;source&gt; &lt;entry_file.py&gt;</code>\n"
                        "...or provide a custom run command instead."
                    )
                }

        venv_path = f"{path}/.venv"
        await update_cb("🐍 <b>Creating virtual environment...</b>")
        code, out = await ssh_exec_async(f'python3 -m venv "{venv_path}" 2>&1', timeout=120)
        if code != 0:
            return {"ok": False, "message": f"Failed to create venv:\n<pre>{_html_escape(out[-1500:])}</pre>"}

        if env_content and env_content.strip():
            await update_cb("🔐 <b>Writing .env file...</b>")
            try:
                await sftp_write_async(path, ".env", env_content.encode("utf-8"))
            except Exception as e:
                return {"ok": False, "message": f"Failed to write .env on host: {_html_escape(str(e))}"}

        from config.settings import runtime_settings
        install_timeout = getattr(runtime_settings, "DEPLOYMENT_TIMEOUT", 600)

        if build_command and build_command.strip():
            await update_cb("📦 <b>Running custom build command...</b>\n<i>(this can take a while)</i>")
            code, out = await ssh_exec_async(
                f'cd "{path}" && PATH="{venv_path}/bin:$PATH" bash -lc {_shq(build_command)} 2>&1',
                timeout=install_timeout
            )
            if code != 0:
                return {"ok": False, "message": f"Build command failed:\n<pre>{_html_escape(out[-2000:])}</pre>"}
        else:
            code, out = await ssh_exec_async(f'test -f "{path}/requirements.txt" && echo HAS_REQ || echo NO_REQ', timeout=15)
            if "HAS_REQ" in out:
                await update_cb("📦 <b>Installing dependencies from requirements.txt...</b>\n<i>(this can take a while)</i>")
                code, out = await ssh_exec_async(
                    f'"{venv_path}/bin/pip" install --no-input --disable-pip-version-check -r "{path}/requirements.txt" 2>&1',
                    timeout=install_timeout
                )
                if code != 0:
                    return {"ok": False, "message": f"Dependency install failed:\n<pre>{_html_escape(out[-2000:])}</pre>"}
            else:
                code, out2 = await ssh_exec_async(
                    f'test -f "{path}/setup.py" -o -f "{path}/pyproject.toml" && echo HAS_SETUP || echo NO_SETUP', timeout=15
                )
                if "HAS_SETUP" in out2:
                    await update_cb("📦 <b>Installing package (setup.py/pyproject.toml)...</b>")
                    code, out = await ssh_exec_async(
                        f'"{venv_path}/bin/pip" install --no-input --disable-pip-version-check -e "{path}" 2>&1',
                        timeout=install_timeout
                    )
                    if code != 0:
                        return {"ok": False, "message": f"Package install failed:\n<pre>{_html_escape(out[-2000:])}</pre>"}

        if existing:
            update_deployment(
                existing["id"], venv_path=venv_path, entry_point=entry_point, last_status="deploying",
                build_command=build_command, run_command=run_command, env_content=env_content,
            )
            deployment_id = existing["id"]
        else:
            deployment_id = create_deployment(
                name, source_type, source, path, venv_path, entry_point,
                build_command=build_command, run_command=run_command, env_content=env_content,
            )

        await update_cb("🚀 <b>Starting application...</b>")
        dep = get_deployment(deployment_id)
        ok, start_msg = await start_deployment(dep)

        if ok:
            update_deployment(deployment_id, desired_state="running")
            return {
                "ok": True,
                "name": name,
                "message": (
                    f"✅ <b>Deployed:</b> <code>{_html_escape(name)}</code>\n{start_msg}\n\n"
                    f"Manage it anytime with /pyps."
                )
            }
        update_deployment(deployment_id, desired_state="stopped", last_status="error")
        return {"ok": False, "message": f"Deployment provisioned but failed to start:\n{start_msg}"}
    except Exception as e:
        logger.exception("Provisioning failed")
        return {"ok": False, "message": f"Unexpected error during provisioning: {_html_escape(str(e))}"}


# ── process lifecycle ──────────────────────────────────────────────────────

async def continue_provision(
    name: str, path: str, source_type: str, source: str,
    entry_point_override: str | None, existing_id: int | None, update_cb,
    build_command: str | None = None, run_command: str | None = None,
    env_content: str | None = None,
) -> dict:
    """
    Resume provisioning a project that was already cloned/extracted onto the
    host but paused because it contained a Dockerfile/compose file (see
    `needs_dockerfile_choice` in `_provision`). The user has now explicitly
    chosen to continue as a plain Python app, optionally supplying a custom
    build/run command and .env content.
    """
    existing = get_deployment(existing_id) if existing_id else None
    return await _provision(
        name=name, path=path, source_type=source_type, source=source,
        entry_point_override=entry_point_override, update_cb=update_cb, existing=existing,
        build_command=build_command, run_command=run_command, env_content=env_content,
        confirmed_python=True,
    )


async def _reprovision_existing(dep: dict, update_cb) -> dict:
    """Re-run the build/venv/start pipeline for a deployment whose code on
    disk has just been changed (git pull or a freshly-uploaded archive),
    reusing whatever build_command/run_command/env_content/entry_point it
    already had. Always ends by (re)starting the app."""
    entry_point_override = dep.get("entry_point") if not dep.get("run_command") else None
    return await _provision(
        name=dep["name"], path=dep["path"], source_type=dep["source_type"], source=dep["source"],
        entry_point_override=entry_point_override, update_cb=update_cb, existing=dep,
        build_command=dep.get("build_command"), run_command=dep.get("run_command"),
        env_content=dep.get("env_content"), confirmed_python=True,
    )


async def update_from_git(dep: dict, update_cb) -> dict:
    """Pull latest changes for a git-sourced deployment, then rebuild and restart it."""
    try:
        if dep.get("source_type") != "github":
            return {"ok": False, "message": "This deployment wasn't created from a Git repo, so there's nothing to pull."}
        path = dep["path"]
        await update_cb("🔄 <b>Pulling latest changes...</b>")
        code, out = await ssh_exec_async(f'cd "{path}" && GIT_TERMINAL_PROMPT=0 git pull 2>&1', timeout=180)
        if code != 0:
            return {"ok": False, "message": f"<code>git pull</code> failed:\n<pre>{_html_escape(_redact_url(out))[-2000:]}</pre>"}
        return await _reprovision_existing(dep, update_cb)
    except Exception as e:
        logger.exception("update_from_git failed")
        return {"ok": False, "message": f"Unexpected error updating from git: {_html_escape(str(e))}"}


async def update_from_archive(dep: dict, file_bytes: bytes, filename: str, update_cb) -> dict:
    """Replace a deployment's code with a freshly-uploaded .zip/.rar, then rebuild and restart it."""
    try:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in ("zip", "rar"):
            return {"ok": False, "message": "Only <code>.zip</code> or <code>.rar</code> archives are supported."}

        path = dep["path"]
        deploy_root = await _get_deploy_root()
        upload_dir = f"{deploy_root}/_uploads"

        await update_cb("📤 <b>Uploading archive to host...</b>")
        remote_archive = await sftp_write_async(upload_dir, filename, file_bytes)

        if dep.get("pid"):
            await _kill_pid(dep["pid"])

        # Keep the venv, logs, pid file, run wrapper, and any .env in place —
        # wipe everything else so the new archive's contents fully replace
        # the old code instead of merging with it.
        await update_cb("🧹 <b>Clearing old code (keeping venv/.env/logs)...</b>")
        keep = {".venv", ".env", ".pydeploy.log", ".pydeploy.pid", ".pydeploy_run.sh"}
        keep_expr = " ".join(f'! -name "{k}"' for k in keep)
        code, out = await ssh_exec_async(
            f'cd "{path}" && find . -mindepth 1 -maxdepth 1 {keep_expr} -exec rm -rf {{}} + 2>&1',
            timeout=60
        )
        if code != 0:
            return {"ok": False, "message": f"Failed to clear old code:\n<pre>{_html_escape(out[-1500:])}</pre>"}

        await update_cb("📦 <b>Extracting new archive...</b>")
        if ext == "zip":
            code, out = await ssh_exec_async(f'unzip -o -q "{remote_archive}" -d "{path}" 2>&1', timeout=120)
        else:
            code, out = await ssh_exec_async(
                f'(command -v unrar >/dev/null 2>&1 && unrar x -o+ "{remote_archive}" "{path}/" 2>&1) || '
                f'(command -v 7z >/dev/null 2>&1 && 7z x -y -o"{path}" "{remote_archive}" 2>&1) || '
                f'echo NO_EXTRACTOR_AVAILABLE',
                timeout=120
            )
            if "NO_EXTRACTOR_AVAILABLE" in out:
                return {
                    "ok": False,
                    "message": (
                        "❌ Neither <code>unrar</code> nor <code>7z</code> is installed on the host.\n"
                        "Install one, e.g. <code>/host sudo apt install -y unrar</code>, then try again."
                    )
                }
        if code != 0:
            return {"ok": False, "message": f"Extraction failed:\n<pre>{_html_escape(out[-1500:])}</pre>"}

        # Flatten a single top-level wrapper folder (common with GitHub zip exports),
        # ignoring the files we intentionally preserved above.
        try:
            keep_pattern = "|".join(re.escape(k) for k in keep)
            await ssh_exec_async(
                f'cd "{path}" && entries=$(ls -A | grep -vE "^({keep_pattern})$"); '
                f'count=$(echo "$entries" | grep -c .); '
                f'if [ "$count" = "1" ] && [ -d "$entries" ]; then '
                f'inner="$entries"; shopt -s dotglob; mv "$inner"/* . 2>/dev/null; rmdir "$inner" 2>/dev/null; fi',
                timeout=20
            )
        except Exception:
            pass

        update_deployment(dep["id"], source_type="archive", source=filename)
        dep = get_deployment(dep["id"]) or dep
        return await _reprovision_existing(dep, update_cb)
    except Exception as e:
        logger.exception("update_from_archive failed")
        return {"ok": False, "message": f"Unexpected error updating from archive: {_html_escape(str(e))}"}


async def delete_deployment(dep: dict) -> tuple[bool, str]:
    """Stop the process, remove all its files on the host, and drop the DB record."""
    try:
        if dep.get("pid"):
            await _kill_pid(dep["pid"])
        code, out = await ssh_exec_async(f'rm -rf "{dep["path"]}" 2>&1', timeout=60)
        _db_delete_deployment(dep["id"])
        if code != 0:
            return True, f"⚠️ Deleted from the deployment list, but host cleanup had issues:\n<pre>{_html_escape(out[-1000:])}</pre>"
        return True, "🗑️ Deployment deleted — process stopped, host files removed."
    except Exception as e:
        logger.exception("delete_deployment failed")
        try:
            _db_delete_deployment(dep["id"])
        except Exception:
            pass
        return False, f"Deleted the record, but hit an error cleaning up the host: {_html_escape(str(e))}"


async def start_deployment(dep: dict) -> tuple[bool, str]:
    try:
        path = dep["path"]
        venv_path = dep["venv_path"]
        entry_point = dep.get("entry_point") or ""
        run_command = dep.get("run_command")
        log_path = f"{path}/.pydeploy.log"
        pid_path = f"{path}/.pydeploy.pid"
        wrapper_path = f"{path}/.pydeploy_run.sh"

        if run_command and run_command.strip():
            # Custom run command — venv's bin/ goes first on PATH so bare
            # `python`/`pip` calls inside it resolve to the venv automatically.
            wrapper_script = (
                "#!/bin/bash\n"
                f'cd "{path}" || exit 1\n'
                f'export PATH="{venv_path}/bin:$PATH"\n'
                f'exec bash -lc {_shq(run_command)}\n'
            )
        else:
            # The wrapper `exec`s into the venv's python so the PID we capture
            # ($!) always matches the real running process — no PID-tracking
            # guesswork.
            wrapper_script = (
                "#!/bin/bash\n"
                f'cd "{path}" || exit 1\n'
                f'exec "{venv_path}/bin/python" "{entry_point}"\n'
            )
        await sftp_write_async(path, ".pydeploy_run.sh", wrapper_script.encode("utf-8"))
        await ssh_exec_async(f'chmod +x "{wrapper_path}"', timeout=15)

        launch_cmd = (
            f'cd "{path}" && '
            f'nohup bash "{wrapper_path}" < /dev/null >> "{log_path}" 2>&1 & '
            f'echo $! > "{pid_path}"; disown; sleep 1; echo LAUNCHED'
        )
        code, out = await ssh_exec_async(launch_cmd, timeout=20)
        if "LAUNCHED" not in out:
            return False, f"Launch command did not confirm startup:\n<pre>{_html_escape(out[-1000:])}</pre>"

        code, out = await ssh_exec_async(f'cat "{pid_path}" 2>/dev/null', timeout=10)
        pid_str = out.strip().splitlines()[-1].strip() if out.strip() else ""
        pid = int(pid_str) if pid_str.isdigit() else None

        if pid:
            alive = await is_alive(pid)
            if not alive:
                tail = await get_logs(dep, lines=40)
                update_deployment(dep["id"], pid=None, last_status="crashed")
                return False, f"Process exited immediately. Last log lines:\n<pre>{_html_escape(tail[-1200:])}</pre>"
            update_deployment(dep["id"], pid=pid, last_status="running")
            return True, f"🟢 Running (pid <code>{pid}</code>)."

        update_deployment(dep["id"], last_status="running")
        return True, "🟢 Started (pid unknown)."
    except Exception as e:
        logger.exception("start_deployment failed")
        return False, f"Unexpected error while starting: {_html_escape(str(e))}"


async def stop_deployment(dep: dict) -> tuple[bool, str]:
    try:
        # Mark desired_state stopped BEFORE killing so the supervisor never races to restart it.
        update_deployment(dep["id"], desired_state="stopped")
        if dep.get("pid"):
            await _kill_pid(dep["pid"])
        update_deployment(dep["id"], pid=None, last_status="stopped")
        return True, "⏹️ Stopped."
    except Exception as e:
        logger.exception("stop_deployment failed")
        return False, f"Unexpected error while stopping: {_html_escape(str(e))}"


async def restart_deployment(dep: dict) -> tuple[bool, str]:
    try:
        if dep.get("pid"):
            await _kill_pid(dep["pid"])
        ok, msg = await start_deployment(dep)
        if ok:
            update_deployment(dep["id"], desired_state="running")
        return ok, msg
    except Exception as e:
        logger.exception("restart_deployment failed")
        return False, f"Unexpected error while restarting: {_html_escape(str(e))}"


async def get_logs(dep: dict, lines: int = 200) -> str:
    try:
        log_path = f'{dep["path"]}/.pydeploy.log'
        code, out = await ssh_exec_async(f'tail -n {lines} "{log_path}" 2>&1', timeout=15)
        return out if out.strip() else "(no output yet)"
    except Exception as e:
        return f"Could not read logs: {e}"


# ── auto-restart supervisor ────────────────────────────────────────────────

async def supervisor_loop(bot, owner_id: int, interval: int = 30):
    """
    Background task: every `interval` seconds, verify each deployment whose
    desired_state is "running" is actually alive on the host, and restart it
    if not. If a deployment restarts more than _FLAP_LIMIT times within
    _FLAP_WINDOW_SECONDS, auto-restart is paused and the owner is notified,
    so a permanently-broken app can't spam restarts forever.

    Wrapped defensively at every step — a failure here must never crash the
    bot or interrupt any other feature.
    """
    logger.info("Python deployment supervisor loop starting...")
    while True:
        try:
            await asyncio.sleep(interval)
            try:
                deployments = list_deployments()
            except Exception:
                logger.exception("Supervisor: failed to list deployments, skipping this cycle")
                continue

            for dep in deployments:
                try:
                    if dep.get("desired_state") != "running":
                        continue
                    if await is_alive(dep.get("pid")):
                        continue

                    now = time.time()
                    history = _restart_history.setdefault(dep["id"], [])
                    history[:] = [t for t in history if now - t < _FLAP_WINDOW_SECONDS]

                    if len(history) >= _FLAP_LIMIT:
                        if dep.get("last_status") != "crash_looping":
                            update_deployment(dep["id"], desired_state="stopped", last_status="crash_looping")
                            try:
                                await bot.send_message(
                                    owner_id,
                                    f"🚨 <b>{_html_escape(dep['name'])}</b> is crash-looping "
                                    f"(restarted {len(history)}x in {_FLAP_WINDOW_SECONDS // 60} min).\n"
                                    f"Auto-restart paused — check /pyps → Logs, fix the issue, then tap ▶ Start.",
                                    parse_mode="HTML"
                                )
                            except Exception:
                                pass
                        continue

                    history.append(now)
                    logger.warning(f"Deployment '{dep['name']}' found dead — auto-restarting.")
                    ok, msg = await start_deployment(dep)
                    try:
                        if ok:
                            await bot.send_message(owner_id, f"♻️ Auto-restarted <b>{_html_escape(dep['name'])}</b>.", parse_mode="HTML")
                        else:
                            await bot.send_message(owner_id, f"⚠️ Auto-restart of <b>{_html_escape(dep['name'])}</b> failed:\n{msg}", parse_mode="HTML")
                    except Exception:
                        pass
                except Exception:
                    logger.exception(f"Supervisor: error handling deployment {dep.get('id')}")
        except Exception:
            logger.exception("Supervisor loop iteration failed")
