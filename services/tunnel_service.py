"""
TunnelEngine — exposes local ports to the internet using cloudflared quick tunnels.

No account, no setup, no config files needed.
Cloudflared is installed on the WSL host once via /expose_setup.

Each tunnel:
  cloudflared tunnel --url http://localhost:<port>
  → outputs a public https://<random>.trycloudflare.com URL

Basic auth is handled at the bot level using a lightweight Python proxy
that sits between the public URL and the local service.
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

# Regex to parse the trycloudflare URL from cloudflared output
_URL_RE = re.compile(r'https://[a-z0-9\-]+\.trycloudflare\.com')

CLOUDFLARED_INSTALL_CMD = (
    # Method 1: Official Cloudflare apt repo (most reliable, no internet issues)
    'curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg '
    '| sudo tee /usr/share/keyrings/cloudflare-main.gpg > /dev/null && '
    'echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] '
    'https://pkg.cloudflare.com/cloudflared any main" '
    '| sudo tee /etc/apt/sources.list.d/cloudflared.list && '
    'sudo apt-get update -qq && sudo apt-get install -y cloudflared 2>&1 && '
    'cloudflared --version'
)

CLOUDFLARED_INSTALL_FALLBACK = (
    # Method 2: Direct binary download from GitHub releases
    'curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/'
    'cloudflared-linux-amd64 -o /usr/local/bin/cloudflared && '
    'chmod +x /usr/local/bin/cloudflared && '
    'cloudflared --version 2>&1'
)


def _ssh_exec(command: str, timeout: int = 60) -> tuple[int, str]:
    """Run a command on the WSL host via SSH."""
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


class TunnelEngine:
    def __init__(self):
        # tunnel_id -> dict
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

    # ── Install ───────────────────────────────────────────────────────────────

    async def is_installed(self) -> bool:
        code, out = await asyncio.to_thread(
            _ssh_exec, "which cloudflared 2>/dev/null || command -v cloudflared 2>/dev/null"
        )
        return code == 0 and bool(out.strip())

    async def install(self) -> tuple[bool, str]:
        """Install cloudflared on the WSL host. Tries apt repo first, then direct download."""
        # Try apt repo (official, most reliable)
        code, out = await asyncio.to_thread(_ssh_exec, CLOUDFLARED_INSTALL_CMD, timeout=180)
        if code == 0 and "cloudflared" in out.lower():
            return True, out

        # Fallback: direct binary download
        logger.info("apt install failed, trying direct download fallback...")
        code2, out2 = await asyncio.to_thread(_ssh_exec, CLOUDFLARED_INSTALL_FALLBACK, timeout=120)
        if code2 == 0:
            return True, out2

        return False, f"apt method:\n{out}\n\nDirect download:\n{out2}"

    # ── Create tunnel ─────────────────────────────────────────────────────────

    async def create_tunnel(
        self,
        target: str,
        timeout: float = 35.0,
    ) -> tuple[bool, str, str]:
        """
        Launch cloudflared quick tunnel to target URL.
        Returns (True, public_url, tunnel_id) or (False, error, "").
        """
        tunnel_id = uuid.uuid4().hex[:6]

        # cloudflared must run on the host (has access to localhost ports)
        # We launch it via SSH in background and capture the URL from output
        cmd = f'cloudflared tunnel --url {target} --no-autoupdate 2>&1'

        logger.info(f"Launching cloudflared tunnel [{tunnel_id}] → {target}")

        def _start_tunnel():
            ssh_user = os.getenv("HOST_SSH_USER")
            ssh_pass = os.getenv("HOST_SSH_PASSWORD")
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect("127.0.0.1", username=ssh_user, password=ssh_pass, timeout=15)
            # Keep channel open, transport keeps running
            transport = ssh.get_transport()
            channel = transport.open_session()
            channel.exec_command(cmd)
            return ssh, channel

        try:
            ssh_conn, channel = await asyncio.to_thread(_start_tunnel)
        except Exception as e:
            return False, f"Failed to start tunnel: {e}", ""

        # Read output lines until public URL appears or timeout
        public_url = ""
        output_buf = []
        try:
            loop = asyncio.get_event_loop()
            deadline = loop.time() + timeout

            def _read_lines():
                lines = []
                import select, time
                while loop.time() < deadline:
                    r, _, _ = select.select([channel], [], [], 1.0)
                    if r:
                        chunk = channel.recv(4096).decode(errors="replace")
                        if not chunk:
                            break
                        lines.append(chunk)
                        combined = "".join(lines)
                        m = _URL_RE.search(combined)
                        if m:
                            return m.group(0), combined
                    if channel.exit_status_ready():
                        break
                return None, "".join(lines)

            public_url, raw_output = await asyncio.to_thread(_read_lines)
            output_buf = [raw_output]

        except Exception as e:
            return False, f"Error reading tunnel output: {e}", ""

        if not public_url:
            # Try to kill the channel
            try:
                channel.close()
                ssh_conn.close()
            except Exception:
                pass
            return False, (
                "cloudflared started but no public URL detected within "
                f"{int(timeout)}s.\n\n"
                "Make sure cloudflared is installed: use /expose_setup first."
            ), ""

        self._tunnels[tunnel_id] = {
            "id":         tunnel_id,
            "target":     target,
            "public_url": public_url,
            "auth":       False,
            "auth_user":  "",
            "ssh":        ssh_conn,
            "channel":    channel,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        logger.info(f"Tunnel [{tunnel_id}] active: {target} → {public_url}")
        return True, public_url, tunnel_id

    # ── Revoke tunnel ─────────────────────────────────────────────────────────

    async def revoke_tunnel(self, tunnel_id: str) -> tuple[bool, str]:
        record = self._tunnels.get(tunnel_id)
        if not record:
            return False, f"No active tunnel with ID <code>{tunnel_id}</code>."
        try:
            record["channel"].close()
            record["ssh"].close()
        except Exception as e:
            logger.warning(f"Error closing tunnel [{tunnel_id}]: {e}")

        # Also kill any leftover cloudflared processes on host targeting this port
        target = record["target"]
        port = target.split(":")[-1].rstrip("/")
        await asyncio.to_thread(
            _ssh_exec, f"pkill -f 'cloudflared.*{port}' 2>/dev/null || true"
        )

        del self._tunnels[tunnel_id]
        logger.info(f"Tunnel [{tunnel_id}] revoked.")
        return True, record["public_url"]

    # ── List / lookup ─────────────────────────────────────────────────────────

    def list_tunnels(self) -> list[dict]:
        # Prune dead channels
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

    def get_tunnel(self, tunnel_id: str) -> dict | None:
        return self._tunnels.get(tunnel_id)

    def find_by_url(self, fragment: str) -> dict | None:
        fragment = fragment.strip().lower()
        for t in self._tunnels.values():
            if fragment in t["public_url"].lower():
                return t
        return None


tunnel_engine = TunnelEngine()
