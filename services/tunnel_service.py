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
from datetime import datetime, timezone

import aiohttp

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

# Auth proxy script — written via SFTP then run on host
# Uses only stdlib (http.server) so no pip install needed on host
_AUTH_PROXY_SCRIPT = r'''#!/usr/bin/env python3
"""Minimal HTTP Basic Auth reverse proxy using only stdlib."""
import sys
import base64
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen, Request
from urllib.error import URLError

USERNAME = sys.argv[1]
PASSWORD = sys.argv[2]
PROXY_PORT = int(sys.argv[3])
BACKEND = sys.argv[4].rstrip("/")
EXPECTED = "Basic " + base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()

class AuthProxy(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # silence access log

    def _send_401(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Protected"')
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Unauthorized")

    def do_request(self):
        auth = self.headers.get("Authorization", "")
        if auth != EXPECTED:
            self._send_401()
            return

        url = BACKEND + self.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else None

        # Build forwarded headers (skip hop-by-hop)
        skip = {"host", "connection", "keep-alive", "transfer-encoding",
                "te", "trailer", "upgrade", "proxy-authorization"}
        fwd_headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in skip}

        try:
            req = Request(url, data=body, headers=fwd_headers, method=self.command)
            with urlopen(req, timeout=30) as resp:
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() not in {"transfer-encoding", "connection"}:
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(resp.read())
        except URLError as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(str(e).encode())

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = do_request

server = HTTPServer(("127.0.0.1", PROXY_PORT), AuthProxy)
server.serve_forever()
'''


def _ssh_exec(command: str, timeout: int = 60) -> tuple[int, str]:
    from utils.ssh_helper import ssh_exec as _helper
    return _helper(command, timeout=timeout)


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
        Write the proxy script to the host via SFTP and start it.
        Credentials passed as positional args (stdlib only, no pip needed).
        Returns (proxy_port, pid_str) or (None, error_message).
        """
        script_path = "/tmp/tele_auth_proxy.py"

        # Write script file via SFTP
        def _write_script():
            import io
            from utils.ssh_helper import get_ssh_creds, get_ssh_host
            import paramiko as _pm
            user, password = get_ssh_creds()
            host, port_ssh = get_ssh_host()
            ssh = _pm.SSHClient()
            ssh.set_missing_host_key_policy(_pm.AutoAddPolicy())
            ssh.connect(host, port=port_ssh, username=user, password=password, timeout=15)
            sftp = ssh.open_sftp()
            sftp.putfo(io.BytesIO(_AUTH_PROXY_SCRIPT.encode()), script_path)
            sftp.close()
            ssh.close()

        try:
            await asyncio.to_thread(_write_script)
        except Exception as e:
            return None, f"Could not write proxy script: {e}"

        # Find a free port on the host
        port = await asyncio.to_thread(_find_free_port_on_host)
        if not port:
            return None, "Could not find a free port on host"

        # Escape credentials for shell — wrap in single quotes, escape single quotes
        def _sh_escape(s: str) -> str:
            return "'" + s.replace("'", "'\\''") + "'"

        u = _sh_escape(username)
        p = _sh_escape(password)
        b = _sh_escape(backend)

        start_cmd = (
            f'nohup python3 {script_path} {u} {p} {port} {b} '
            f'> /tmp/tele_proxy_{port}.log 2>&1 & echo $!'
        )
        code, out = await asyncio.to_thread(_ssh_exec, start_cmd, 10)
        if code != 0:
            return None, f"Could not start proxy: {out}"

        pid = out.strip()

        # Verify proxy is actually listening (up to 3 seconds)
        for _ in range(6):
            await asyncio.sleep(0.5)
            check_code, _ = await asyncio.to_thread(
                _ssh_exec,
                f"ss -tlnp 2>/dev/null | grep '127.0.0.1:{port}' || "
                f"netstat -tlnp 2>/dev/null | grep ':{port}'",
                5
            )
            if check_code == 0:
                logger.info(f"Auth proxy verified listening on :{port} (pid {pid})")
                return port, pid

        # Check proxy log for errors
        _, log = await asyncio.to_thread(_ssh_exec, f"cat /tmp/tele_proxy_{port}.log 2>/dev/null", 5)
        return None, f"Proxy started (pid {pid}) but not listening on :{port}.\nLog: {log[:300]}"

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
        target_for_tunnel = target  # default: point cloudflared straight at the service
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

        cmd = f'cloudflared tunnel --url {target_for_tunnel} --no-autoupdate 2>&1'
        logger.info(f"Launching cloudflared [{tunnel_id}] → {target_for_tunnel}")

        def _start_cf():
            from utils.ssh_helper import get_ssh_creds, get_ssh_host
            import paramiko as _pm
            user, pwd = get_ssh_creds()
            host, port_ssh = get_ssh_host()
            ssh = _pm.SSHClient()
            ssh.set_missing_host_key_policy(_pm.AutoAddPolicy())
            ssh.connect(host, port=port_ssh, username=user, password=pwd, timeout=15)
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
