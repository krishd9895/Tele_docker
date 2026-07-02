"""
ZrokTunnelEngine — manages zrok share public processes.

Each active tunnel is stored as:
  {
    "id":         short unique id  (e.g. "a1b2c3")
    "target":     "http://localhost:8080"
    "public_url": "https://xyz.yourdomain.com"
    "auth":       True / False
    "auth_user":  "user" or ""
    "process":    asyncio.subprocess.Process
    "created_at": datetime iso string
  }

Self-hosted zrok setup:
  Token comes from YOUR server:
    docker compose exec zrok-controller zrok admin create account you@domain.com password
  Copy the accountToken value into ZROK_PRIVATE_TOKEN in .env.
  If ZROK_API_ENDPOINT is set, enrollment points at your controller.
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

# Matches any https URL in zrok stdout (works for self-hosted domains too)
_URL_RE = re.compile(r'https?://[^\s]+\.[^\s]+')

ZROK_INSTALL_SCRIPT = "curl -sSf https://get.zrok.io | bash"


def _ssh_exec(command: str, timeout: int = 120) -> tuple[int, str]:
    """Synchronous SSH execution on the WSL host. Returns (exit_code, output)."""
    ssh_user = os.getenv("HOST_SSH_USER")
    ssh_pass = os.getenv("HOST_SSH_PASSWORD")
    if not ssh_user or not ssh_pass:
        return -1, "HOST_SSH_USER or HOST_SSH_PASSWORD not set in .env"
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect("127.0.0.1", username=ssh_user, password=ssh_pass, timeout=15)
        _, stdout, stderr = ssh.exec_command(command, timeout=timeout)
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

    # ── Setup helpers ─────────────────────────────────────────────────────────

    async def is_installed(self) -> bool:
        code, out = await asyncio.to_thread(_ssh_exec, "which zrok || command -v zrok")
        return code == 0 and bool(out.strip())

    async def is_enrolled(self) -> bool:
        code, out = await asyncio.to_thread(_ssh_exec, "zrok status 2>&1")
        return code == 0 and "not enabled" not in out.lower()

    async def install_zrok(self) -> tuple[bool, str]:
        """
        Download and install zrok binary on the WSL host.
        Tries self-hosted download first (no internet needed),
        falls back to the official install script.
        """
        endpoint = runtime_settings.ZROK_API_ENDPOINT

        if endpoint:
            # Self-hosted zrok controller serves its own binary at /api/download
            # Try to download directly from your controller — no internet needed
            host = endpoint.rstrip("/")
            install_cmd = (
                f'curl -sSfL "{host}/api/download?os=linux&arch=amd64" '
                f'-o /tmp/zrok.tar.gz 2>&1 && '
                f'tar -xzf /tmp/zrok.tar.gz -C /tmp 2>&1 && '
                f'sudo mv /tmp/zrok /usr/local/bin/zrok 2>&1 && '
                f'chmod +x /usr/local/bin/zrok && '
                f'echo "zrok installed from self-hosted controller"'
            )
            code, out = await asyncio.to_thread(_ssh_exec, install_cmd)
            if code == 0 and "zrok installed" in out:
                return True, out
            # Self-hosted download failed — log and try official script
            logger.warning(f"Self-hosted install failed ({out[:100]}), trying official script...")

        # Fallback: official install script (requires internet)
        code, out = await asyncio.to_thread(_ssh_exec, ZROK_INSTALL_SCRIPT)
        if code == 0:
            return True, out or "zrok installed successfully."
        return False, (
            f"{out}\n\n"
            "Could not install zrok automatically.\n"
            "Your host may not have internet access.\n\n"
            "Manual option — run this on your host:\n"
            f"  curl -sSfL {endpoint}/api/download?os=linux&arch=amd64 -o /tmp/zrok.tar.gz\n"
            f"  tar -xzf /tmp/zrok.tar.gz -C /tmp\n"
            f"  sudo mv /tmp/zrok /usr/local/bin/zrok"
        )

    async def enroll_zrok(self, token: str) -> tuple[bool, str]:
        token = token.strip()
        endpoint = runtime_settings.ZROK_API_ENDPOINT
        if endpoint:
            cmd = f"zrok enable --ctrl-endpoint {endpoint} {token}"
        else:
            cmd = f"zrok enable {token}"
        code, out = await asyncio.to_thread(_ssh_exec, cmd)
        if code == 0:
            return True, out or "Enrollment successful."
        return False, out or "Enrollment failed."

    async def create_account_and_get_token(self) -> tuple[bool, str]:
        """
        Runs docker compose exec zrok-controller zrok admin create account on the host,
        parses accountToken from output, returns (True, token) or (False, error).
        """
        email = runtime_settings.ZROK_ACCOUNT_EMAIL
        password = runtime_settings.ZROK_ACCOUNT_PASSWORD
        if not email or not password:
            return False, "ZROK_ACCOUNT_EMAIL or ZROK_ACCOUNT_PASSWORD not set in .env"

        controller_dir = runtime_settings.ZROK_CONTROLLER_DIR
        if not controller_dir:
            return False, (
                "ZROK_CONTROLLER_DIR not set in .env\n\n"
                "Set it to the folder on your host that contains your zrok docker-compose.yml\n"
                "Example: ZROK_CONTROLLER_DIR=/home/d/Kwz\n\n"
                "Check with /host ls to find the right folder."
            )

        cmd = (
            f'cd "{controller_dir}" && '
            f'docker compose exec -T zrok-controller '
            f'zrok admin create account {email} {password} 2>&1'
        )
        logger.info(f"Creating zrok account for {email} in {controller_dir}...")
        code, output = await asyncio.to_thread(_ssh_exec, cmd)

        token = None
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("accountToken:"):
                token = line.split("accountToken:", 1)[1].strip()
                break
            if "accountToken" in line and ":" in line:
                token = line.split(":", 1)[1].strip().strip('"').strip(",")
                break

        if token:
            logger.info(f"zrok accountToken obtained for {email}")
            runtime_settings.ZROK_PRIVATE_TOKEN = token
            return True, token

        return False, (
            f"Could not parse accountToken from output.\n\n"
            f"Raw output:\n{output[:1000]}\n\n"
            f"Make sure ZROK_CONTROLLER_DIR is correct and the zrok-controller container is running.\n"
            f"Try: /host ls {controller_dir}"
        )

    async def auto_enroll_if_needed(self) -> None:
        """
        Called at bot startup:
        1. If not installed — warn and skip.
        2. If already enrolled — skip.
        3. If no token — auto-create account on the host controller.
        4. Enroll with the token.
        """
        if not await self.is_installed():
            logger.warning("zrok not installed — skipping auto-enroll. Run /zrok_setup → Install.")
            return

        if await self.is_enrolled():
            logger.info("zrok already enrolled, skipping auto-enroll.")
            return

        token = runtime_settings.ZROK_PRIVATE_TOKEN
        if not token or token == "your_account_token_here":
            logger.info("No ZROK_PRIVATE_TOKEN set — attempting auto account creation...")
            success, result = await self.create_account_and_get_token()
            if not success:
                logger.error(f"zrok auto account creation failed: {result[:200]}")
                return
            token = result
            logger.info("zrok account created and token obtained automatically.")

        logger.info("Auto-enrolling zrok...")
        success, out = await self.enroll_zrok(token)
        if success:
            logger.info("zrok auto-enrollment successful.")
        else:
            logger.error(f"zrok auto-enrollment failed: {out[:200]}")

    async def get_zrok_status(self) -> str:
        _, out = await asyncio.to_thread(_ssh_exec, "zrok status 2>&1")
        return out or "(no output)"

    # ── Create tunnel ─────────────────────────────────────────────────────────

    async def create_share(
        self,
        target: str,
        basic_auth: str | None = None,
        timeout: float = 40.0,
    ) -> tuple[bool, str, str]:
        tunnel_id = uuid.uuid4().hex[:6]
        cmd = [self.zrok_binary, "share", "public", target]
        if basic_auth:
            cmd += ["--basic-auth", basic_auth]

        logger.info(f"Launching zrok [{tunnel_id}]: {' '.join(cmd)}")
        env = {**os.environ, "HOME": os.path.expanduser("~")}

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
        except FileNotFoundError:
            return False, "zrok binary not found.\n\nRun /zrok_setup → Install zrok, then Enroll Account.", ""
        except Exception as e:
            return False, f"Failed to start zrok: {e}", ""

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
                    candidate = match.group(0)
                    if "://" in candidate and not candidate.endswith((".log", ".conf")):
                        public_url = candidate
                        break
        except Exception as e:
            proc.kill()
            return False, f"Error reading zrok output: {e}", ""

        if not public_url:
            proc.kill()
            return False, (
                f"zrok started but no public URL detected within {int(timeout)}s.\n"
                "Check enrollment with /zrok_setup → Check Status."
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
