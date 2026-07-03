"""
TunnelEngine — exposes local ports via cloudflared quick tunnels.

URL type: https://<random>.trycloudflare.com  — TEMPORARY, changes each run.
For permanent URLs, use Cloudflare named tunnels (requires CF account).

HTTP Basic Auth is implemented via a local aiohttp reverse proxy:
  [Browser] → [cloudflared] → [auth proxy :random_port] → [real service :port]
The proxy script runs on the WSL host as a background process.
"""

import asyncio
import re
import logging
import uuid
import os
import paramiko
from datetime import datetime, timezone

import aiohttp

from config.settings import runtime_settings

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r'https://[a-z0-9\-]+\.trycloudflare\.com')

CLOUDFLARED_INSTALL_CMD = (
    'curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg '
    '| sudo tee /usr/share/keyrings/cloudflare-main.gpg > /dev/null && '
    'echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] '
    'https://pkg.cloudflare.com/cloudflared any main" '
    '| sudo tee /etc/apt/sources.list.d/cloudflared.list && '
    'sudo apt-get update -qq && sudo apt-get install -y cloudflared 2>&1 && '
    'cloudflared --version'
)

CLOUDFLARED_INSTALL_FALLBACK = (
    'curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/'
    'cloudflared-linux-amd64 -o /usr/local/bin/cloudflared && '
    'chmod +x /usr/local/bin/cloudflared && '
    'cloudflared --version 2>&1'
)

# Auth proxy script written to host and run with Python 3
_AUTH_PROXY_SCRIPT = '''
import sys, base64, asyncio
from aiohttp import web, ClientSession

USERNAME = sys.argv[1]
PASSWORD = sys.argv[2]
PROXY_PORT = int(sys.argv[3])
BACKEND = sys.argv[4]
EXPECTED = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()

async def handle(request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic ") or auth[6:] != EXPECTED:
        return web.Response(
            status=401,
            headers={"WWW-Authenticate": "Basic realm=\\"Protected\\""},
            text="Unauthorized"
        )
    async with ClientSession() as session:
        url = BACKEND.rstrip("/") + str(request.rel_url)
        async with session.request(
            request.method, url,
            headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
            data=await request.read()
        ) as resp:
            body = await resp.read()
            return web.Response(status=resp.status, headers=dict(resp.headers), body=body)

app = web.Application()
app.router.add_route("*", "/{path_info:.*}", handle)
web.run_app(app, host="127.0.0.1", port=PROXY_PORT, print=None)
'''


def _ssh_exec(command: str, timeout: int = 60) -> tuple[int, str]:
    ssh_user = os.getenv("HOST_SSH_USER")
    ssh_pass = os.getenv("HOST_SSH_PASSWORD")
    if not ssh_user or not ssh_pass:
        return -1, "HOST_SSH_USER / HOST_SSH_PASSWORD not set"
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect("127.0.0.1", username=ssh_user, password=ssh_pass, timeout=15)
        _, stdout, stderr = ssh.exec_command(command, timeout=timeout)
        code = stdout.channel.recv_exit_status()
        out = stdout.read().decode(errors="ignore") + stderr.read().decode(errors="ignore")
        return code, out.strip()
    except Exception as e:
        return -1, str(e)
    finally:
        ssh.close()


def _find_free_port_on_host() -> int | None:
    """Find a free port on the WSL host between 19000-19999."""
    code, out = _ssh_exec(
        "python3 -c \""
        "import socket; s=socket.socket(); s.bind(('127.0.0.1',0)); "
        "print(s.getsockname()[1]); s.close()\""
    )
    if code == 0 and out.strip().isdigit():
        return int(out.strip())
    return None


class TunnelEngine:
    def __init__(self):
        self._tunnels: dict[str, dict] = {}

    # ── Health check ──────────────────────────────────────────────────────────

    async def ping_target(self, url: str, timeout: float = 8.0) -> tuple[bool, str]:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                    allow_redirects=True,
                    ssl=False
                ) as resp:
                    return True, f"HTTP {resp.status}"
        except aiohttp.ClientConnectorError as e:
            return False, f"Connection refused: {e}"
        except asyncio.TimeoutError:
            return False, f"Timed out after {timeout}s"
        except Exception as e:
            return False, str(e)

    # ── Install cloudflared ───────────────────────────────────────────────────

    async def is_installed(self) -> bool:
        code, out = await asyncio.to_thread(
            _ssh_exec, "which cloudflared 2>/dev/null || command -v cloudflared 2>/dev/null"
        )
        return code == 0 and bool(out.strip())

    async def install(self) -> tuple[bool, str]:
        code, out = await asyncio.to_thread(_ssh_exec, CLOUDFLARED_INSTALL_CMD, timeout=180)
        if code == 0 and "cloudflared" in out.lower():
            return True, out
        logger.info("apt install failed, trying direct download...")
        code2, out2 = await asyncio.to_thread(_ssh_exec, CLOUDFLARED_INSTALL_FALLBACK, timeout=120)
        if code2 == 0:
            return True, out2
        return False, f"apt:\n{out}\n\ndirect:\n{out2}"

    # ── Auth proxy ────────────────────────────────────────────────────────────

    async def _start_auth_proxy(
        self, backend: str, username: str, password: str
    ) -> tuple[int | None, str]:
        """
        Write the proxy script to the host and start it as a background process.
        Returns (proxy_port, pid_str) or (None, error).
        """
        script_path = "/tmp/tele_auth_proxy.py"

        # Write script via SSH
        escaped = _AUTH_PROXY_SCRIPT.replace('"', '\\"').replace('\n', '\\n')
        write_cmd = f'printf "%s" "{escaped}" > {script_path}'
        # Use heredoc approach — safer for multiline
        def _write_script():
            ssh_user = os.getenv("HOST_SSH_USER")
            ssh_pass = os.getenv("HOST_SSH_PASSWORD")
            import paramiko as _pm
            ssh = _pm.SSHClient()
            ssh.set_missing_host_key_policy(_pm.AutoAddPolicy())
            ssh.connect("127.0.0.1", username=ssh_user, password=ssh_pass, timeout=15)
            sftp = ssh.open_sftp()
            import io
            sftp.putfo(io.BytesIO(_AUTH_PROXY_SCRIPT.encode()), script_path)
            sftp.close()
            ssh.close()

        try:
            await asyncio.to_thread(_write_script)
        except Exception as e:
            return None, f"Could not write proxy script: {e}"

        # Find free port
        port = await asyncio.to_thread(_find_free_port_on_host)
        if not port:
            return None, "Could not find a free port on host"

        # Start proxy in background, capture PID
        start_cmd = (
            f'nohup python3 {script_path} '
            f'"{username}" "{password}" {port} "{backend}" '
            f'> /tmp/tele_proxy_{port}.log 2>&1 & echo $!'
        )
        code, out = await asyncio.to_thread(_ssh_exec, start_cmd, timeout=10)
        if code != 0:
            return None, f"Could not start proxy: {out}"

        pid = out.strip()
        # Give it a moment to start
        await asyncio.sleep(1.5)
        return port, pid

    # ── Create tunnel ─────────────────────────────────────────────────────────

    async def create_tunnel(
        self,
        target: str,
        auth_user: str = "",
        auth_pass: str = "",
        timeout: float = 40.0,
    ) -> tuple[bool, str, str]:
        """
        Launch cloudflared quick tunnel.
        If auth_user/auth_pass set, starts a local auth proxy first.
        Returns (True, public_url, tunnel_id) or (False, error, "").
        """
        tunnel_id = uuid.uuid4().hex[:6]
        has_auth = bool(auth_user and auth_pass)
        proxy_port = None
        proxy_pid = None

        # Start auth proxy if needed
        if has_auth:
            proxy_port, proxy_pid_or_err = await self._start_auth_proxy(
                target, auth_user, auth_pass
            )
            if proxy_port is None:
                logger.warning(f"Auth proxy failed: {proxy_pid_or_err} — falling back to no-auth")
                has_auth = False
            else:
                proxy_pid = proxy_pid_or_err
                target_for_tunnel = f"http://127.0.0.1:{proxy_port}"
                logger.info(f"Auth proxy started on :{proxy_port} (pid {proxy_pid})")
        
        target_for_tunnel = f"http://127.0.0.1:{proxy_port}" if proxy_port else target

        cmd = f'cloudflared tunnel --url {target_for_tunnel} --no-autoupdate 2>&1'
        logger.info(f"Launching cloudflared [{tunnel_id}] → {target_for_tunnel}")

        def _start_cf():
            ssh_user = os.getenv("HOST_SSH_USER")
            ssh_pass = os.getenv("HOST_SSH_PASSWORD")
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect("127.0.0.1", username=ssh_user, password=ssh_pass, timeout=15)
            transport = ssh.get_transport()
            channel = transport.open_session()
            channel.exec_command(cmd)
            return ssh, channel

        try:
            ssh_conn, channel = await asyncio.to_thread(_start_cf)
        except Exception as e:
            return False, f"Failed to start cloudflared: {e}", ""

        # Read output until URL appears
        public_url = None
        try:
            loop = asyncio.get_event_loop()
            deadline = loop.time() + timeout

            def _read():
                import select
                buf = []
                while loop.time() < deadline:
                    r, _, _ = select.select([channel], [], [], 1.0)
                    if r:
                        chunk = channel.recv(4096).decode(errors="replace")
                        if not chunk:
                            break
                        buf.append(chunk)
                        m = _URL_RE.search("".join(buf))
                        if m:
                            return m.group(0), "".join(buf)
                    if channel.exit_status_ready():
                        break
                return None, "".join(buf)

            public_url, raw = await asyncio.to_thread(_read)
        except Exception as e:
            channel.close(); ssh_conn.close()
            return False, f"Error reading cloudflared output: {e}", ""

        if not public_url:
            channel.close(); ssh_conn.close()
            return False, (
                f"cloudflared started but no URL detected within {int(timeout)}s.\n"
                "Run /expose_setup to check installation."
            ), ""

        self._tunnels[tunnel_id] = {
            "id":          tunnel_id,
            "target":      target,
            "public_url":  public_url,
            "auth":        has_auth,
            "auth_user":   auth_user if has_auth else "",
            "proxy_port":  proxy_port,
            "proxy_pid":   proxy_pid,
            "ssh":         ssh_conn,
            "channel":     channel,
            "created_at":  datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        logger.info(f"Tunnel [{tunnel_id}] active: {target} → {public_url} (auth={has_auth})")
        return True, public_url, tunnel_id

    # ── Revoke tunnel ─────────────────────────────────────────────────────────

    async def revoke_tunnel(self, tunnel_id: str) -> tuple[bool, str]:
        record = self._tunnels.get(tunnel_id)
        if not record:
            return False, f"No active tunnel with ID <code>{tunnel_id}</code>."

        # Kill cloudflared channel
        try:
            record["channel"].close()
            record["ssh"].close()
        except Exception as e:
            logger.warning(f"Error closing cloudflared [{tunnel_id}]: {e}")

        # Kill auth proxy if running
        if record.get("proxy_pid"):
            await asyncio.to_thread(_ssh_exec, f"kill {record['proxy_pid']} 2>/dev/null || true")
        if record.get("proxy_port"):
            port = record["proxy_port"]
            await asyncio.to_thread(_ssh_exec, f"pkill -f 'tele_auth_proxy.*{port}' 2>/dev/null || true")

        # Kill lingering cloudflared for this target
        port = record["target"].split(":")[-1].rstrip("/")
        await asyncio.to_thread(_ssh_exec, f"pkill -f 'cloudflared.*{port}' 2>/dev/null || true")

        del self._tunnels[tunnel_id]
        logger.info(f"Tunnel [{tunnel_id}] revoked.")
        return True, record["public_url"]

    # ── List / lookup ─────────────────────────────────────────────────────────

    def list_tunnels(self) -> list[dict]:
        dead = []
        for tid, t in self._tunnels.items():
            try:
                if t["channel"].exit_status_ready():
                    dead.append(tid)
            except Exception:
                dead.append(tid)
        for tid in dead:
            logger.warning(f"Tunnel [{tid}] died, removing.")
            del self._tunnels[tid]
        return list(self._tunnels.values())

    def get_tunnel(self, tid: str) -> dict | None:
        return self._tunnels.get(tid)

    def find_by_url(self, fragment: str) -> dict | None:
        fragment = fragment.strip().lower()
        for t in self._tunnels.values():
            if fragment in t["public_url"].lower():
                return t


tunnel_engine = TunnelEngine()
