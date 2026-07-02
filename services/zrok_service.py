"""
ZrokTunnelEngine — manages zrok share public processes.

Each active tunnel is stored as:
  {
    "id":         short unique id  (e.g. "a1b2c3")
    "target":     "http://localhost:8080"
    "public_url": "https://xyz.share.zrok.io"
    "auth":       True / False
    "auth_user":  "user" or ""
    "process":    asyncio.subprocess.Process
    "created_at": datetime iso string
  }
"""

import asyncio
import re
import logging
import uuid
import os
from datetime import datetime, timezone

import aiohttp
import paramiko

from config.settings import runtime_settings

logger = logging.getLogger(__name__)

# Matches any https zrok share URL in stdout output
_URL_RE = re.compile(r'https?://[^\s]+\.zrok\.io[^\s]*')

# zrok install script (official)
ZROK_INSTALL_SCRIPT = "curl -sSf https://get.zrok.io | bash"


def _ssh_exec(command: str) -> tuple[int, str]:
    """Synchronous SSH execution on the WSL host. Returns (exit_code, output)."""
    ssh_user = os.getenv("HOST_SSH_USER")
    ssh_pass = os.getenv("HOST_SSH_PASSWORD")
    if not ssh_user or not ssh_pass:
        return -1, "HOST_SSH_USER or HOST_SSH_PASSWORD not set in .env"

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect("127.0.0.1", username=ssh_user, password=ssh_pass, timeout=15)
        stdin, stdout, stderr = ssh.exec_command(command, timeout=120)
        exit_code = stdout.channel.recv_exit_status()
        output = stdout.read().decode(errors="ignore") + stderr.read().decode(errors="ignore")
        return exit_code, output.strip()
    except Exception as e:
        return -1, str(e)
    finally:
        ssh.close()


class ZrokTunnelEngine:
    def __init__(self, zrok_binary: str = "zrok"):
        self.zrok_binary = zrok_binary
        self._tunnels: dict[str, dict] = {}

    # ── Health check ──────────────────────────────────────────────────────────

    async def ping_target(self, target_url: str, timeout: float = 8.0) -> tuple[bool, str]:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    target_url,
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

    # ── Setup helpers (run on host via SSH) ───────────────────────────────────

    async def is_installed(self) -> bool:
        """Check if zrok binary exists on the host."""
        code, out = await asyncio.to_thread(_ssh_exec, "which zrok || command -v zrok")
        return code == 0 and bool(out.strip())

    async def is_enrolled(self) -> bool:
        """Check if zrok has been enrolled (account token set)."""
        code, out = await asyncio.to_thread(_ssh_exec, "zrok status 2>&1")
        # enrolled: no "not enabled" in output and exit 0
        return code == 0 and "not enabled" not in out.lower()

    async def install_zrok(self) -> tuple[bool, str]:
        """Download and install zrok on the WSL host."""
        code, out = await asyncio.to_thread(_ssh_exec, ZROK_INSTALL_SCRIPT)
        if code == 0:
            return True, out or "zrok installed successfully."
        return False, out or "Install failed with no output."

    async def enroll_zrok(self, token: str) -> tuple[bool, str]:
        """Run `zrok enable <token>` on the WSL host."""
        token = token.strip()
        code, out = await asyncio.to_thread(_ssh_exec, f"zrok enable {token}")
        if code == 0:
            return True, out or "Enrollment successful."
        return False, out or "Enrollment failed."

    async def get_zrok_status(self) -> str:
        """Return raw output of `zrok status` from the host."""
        _, out = await asyncio.to_thread(_ssh_exec, "zrok status 2>&1")
        return out or "(no output)"

    # ── Create tunnel ─────────────────────────────────────────────────────────

    async def create_share(
        self,
        target: str,
        basic_auth: str | None = None,
        timeout: float = 40.0,
    ) -> tuple[bool, str, str]:
        """
        Launches `zrok share public <target>` as a persistent subprocess
        inside the container (which shares the host network).
        Returns (True, public_url, tunnel_id) or (False, error, "").
        """
        tunnel_id = uuid.uuid4().hex[:6]
        cmd = [self.zrok_binary, "share", "public", target]
        if basic_auth:
            cmd += ["--basic-auth", basic_auth]

        logger.info(f"Launching zrok [{tunnel_id}]: {' '.join(cmd)}")

        # Pass the host's HOME so zrok can find ~/.zrok enrollment
        env = {**os.environ, "HOME": os.path.expanduser("~")}

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
        except FileNotFoundError:
            return False, (
                "zrok binary not found in container.\n\n"
                "Run /zrok_setup to install and enroll zrok on your host."
            ), ""
        except Exception as e:
            return False, f"Failed to start zrok: {e}", ""

        # Read stdout until public URL appears or timeout
        public_url = ""
        try:
            loop = asyncio.get_event_loop()
            deadline = loop.time() + timeout
            while loop.time() < deadline:
                try:
                    line_bytes = await asyncio.wait_for(proc.stdout.readline(), timeout=2.0)
                except asyncio.TimeoutError:
                    if proc.returncode is not None:
                        break
                    continue
                if not line_bytes:
                    break
                line = line_bytes.decode(errors="replace").strip()
                logger.debug(f"zrok[{tunnel_id}]: {line}")
                match = _URL_RE.search(line)
                if match:
                    public_url = match.group(0)
                    break
        except Exception as e:
            proc.kill()
            return False, f"Error reading zrok output: {e}", ""

        if not public_url:
            proc.kill()
            return False, (
                f"zrok started but no public URL detected within {int(timeout)}s.\n"
                "Check enrollment with /zrok_setup → Status."
            ), ""

        self._tunnels[tunnel_id] = {
            "id":         tunnel_id,
            "target":     target,
            "public_url": public_url,
            "auth":       basic_auth is not None,
            "auth_user":  basic_auth.split(":")[0] if basic_auth else "",
            "process":    proc,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        logger.info(f"Tunnel [{tunnel_id}] active: {target} → {public_url}")
        return True, public_url, tunnel_id

    # ── Revoke tunnel ─────────────────────────────────────────────────────────

    async def revoke_share(self, tunnel_id: str) -> tuple[bool, str]:
        record = self._tunnels.get(tunnel_id)
        if not record:
            return False, f"No active tunnel with ID <code>{tunnel_id}</code>."
        proc: asyncio.subprocess.Process = record["process"]
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
        except Exception as e:
            logger.warning(f"Error terminating tunnel [{tunnel_id}]: {e}")
        del self._tunnels[tunnel_id]
        logger.info(f"Tunnel [{tunnel_id}] revoked.")
        return True, record["public_url"]

    # ── List / lookup ─────────────────────────────────────────────────────────

    def list_tunnels(self) -> list[dict]:
        dead = [tid for tid, t in self._tunnels.items() if t["process"].returncode is not None]
        for tid in dead:
            logger.warning(f"Tunnel [{tid}] died unexpectedly, removing.")
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


zrok_engine = ZrokTunnelEngine()
