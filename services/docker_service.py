import docker
import asyncio
import os
from pathlib import Path
from typing import AsyncGenerator
import logging

from utils.ssh_helper import ssh_exec as _ssh_exec_base

logger = logging.getLogger(__name__)

COMPOSE_FILES = ["docker-compose.yml", "docker-compose.yaml", "compose.yaml", "compose.yml"]


def _ssh_exec(command: str, timeout: int = 120) -> tuple[int, str]:
    return _ssh_exec_base(command, timeout=timeout)

class DockerOrchestrationEngine:
    def __init__(self):
        self.client = docker.from_env()

    async def get_running_containers(self):
        return await asyncio.to_thread(self.client.containers.list, all=False)

    async def get_all_containers(self):
        return await asyncio.to_thread(self.client.containers.list, all=True)

    async def get_system_stats(self):
        return await asyncio.to_thread(self.client.info)

    async def stream_live_logs(self, container_id: str) -> AsyncGenerator[str, None]:
        container = self.client.containers.get(container_id)
        loop = asyncio.get_running_loop()
        def get_stream():
            return container.logs(stream=True, tail=50, follow=True)
        
        stream = await loop.run_in_executor(None, get_stream)
        for line in stream:
            yield line.decode('utf-8')
            await asyncio.sleep(0.1)

    async def deploy_sandbox_verify(self, image_tag: str, target_port: int, host_port: int) -> bool:
        try:
            container = await asyncio.to_thread(
                self.client.containers.run,
                image=image_tag,
                detach=True,
                ports={f'{target_port}/tcp': host_port},
                network_mode="bridge"
            )
            return True
        except Exception:
            return False

    async def deploy_compose_sandbox(self, repo_path: str, manifest_name: str = "compose.yaml") -> bool:
        """Launches cluster images and bubbles raw compilation errors up to the handler layer."""
        try:
            pure_path = Path(repo_path)
            if not str(pure_path).startswith("/app"):
                if "data/workspaces" in str(pure_path):
                    relative_part = str(pure_path).split("data/workspaces")[-1].lstrip("/")
                    absolute_cwd = f"/app/data/workspaces/{relative_part}"
                else:
                    absolute_cwd = f"/app/{str(pure_path).lstrip('/')}"
            else:
                absolute_cwd = str(pure_path)

            logging.info(f"Orchestrating stack deployment inside absolute path: {absolute_cwd}")

            proc = await asyncio.create_subprocess_exec(
                "docker", "compose", "-f", manifest_name, "up", "-d",
                cwd=absolute_cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=200.0)
            
            if proc.returncode == 0:
                logging.info(f"Compose stack up verified successfully: {absolute_cwd}")
                return True
                
            # RAISE ERROR WITH RAW ENGINE STDERR TEXT SO HANDLERS CAN REVEAL IT TO THE USER
            error_msg = stderr.decode().strip() if stderr else "Unknown initialization error"
            raise RuntimeError(error_msg)
            
        except Exception as ex:
            if isinstance(ex, RuntimeError):
                raise ex
            raise RuntimeError(f"Orchestration sequence exception crashed: {ex}")

    async def list_compose_projects(self, workspace_root: str = "data/workspaces") -> list[dict]:
        """
        Scan for compose projects from two sources:
        1. Container's data/workspaces
        2. GIT_SCAN_PATHS on the WSL host via SSH (same setting as gitpull)
        """
        projects = []

        # ── 1. Container-local workspace ──────────────────────────────────
        root = Path(workspace_root)
        if root.exists():
            for entry in sorted(root.iterdir()):
                if entry.is_dir():
                    for cf in COMPOSE_FILES:
                        if (entry / cf).exists():
                            projects.append({
                                "name":     entry.name,
                                "path":     str(entry),
                                "manifest": cf,
                                "location": "container",
                            })
                            break

        # ── 2. Host paths via SSH ──────────────────────────────────────────
        from config.settings import runtime_settings
        scan_paths_raw = runtime_settings.GIT_SCAN_PATHS
        if scan_paths_raw:
            for scan_path in [p.strip() for p in scan_paths_raw.split(",") if p.strip()]:
                host_projects = await self._list_host_compose_projects(scan_path)
                projects.extend(host_projects)

        return projects

    async def _list_host_compose_projects(self, scan_path: str) -> list[dict]:
        """Find folders containing a compose file up to 2 levels deep on the host."""
        # Build a find command that looks for any compose filename
        names = " -o ".join(f'-name "{cf}"' for cf in COMPOSE_FILES)
        cmd = f'find "{scan_path}" -maxdepth 2 \\( {names} \\) -type f 2>/dev/null'
        code, output = await asyncio.to_thread(_ssh_exec, cmd)
        if code != 0 or not output.strip():
            return []

        projects = []
        seen = set()
        for compose_file_path in output.strip().splitlines():
            compose_file_path = compose_file_path.strip()
            folder = compose_file_path.rsplit("/", 1)[0]
            manifest = compose_file_path.rsplit("/", 1)[1]
            if folder in seen:
                continue
            seen.add(folder)
            name = folder.rstrip("/").split("/")[-1]
            projects.append({
                "name":     name,
                "path":     folder,
                "manifest": manifest,
                "location": "host",
            })
        return projects

    async def compose_build(self, project_path: str, manifest_name: str = None) -> tuple[bool, str]:
        """Run docker compose build in the given path."""
        path = Path(project_path)
        project_name = str(path).rstrip("/").split("/")[-1]
        is_teledocker_repo = project_name == "Tele_docker" or project_name == "Tele_docker-main"

        if not path.exists():
            if is_teledocker_repo:
                return await self._compose_cmd_on_host(project_path, "build tg-manager-bot", manifest_name)
            return await self._compose_cmd_on_host(project_path, "build", manifest_name)

        if not manifest_name:
            for cf in COMPOSE_FILES:
                if (path / cf).exists():
                    manifest_name = cf
                    break
        if not manifest_name:
            return False, "No compose file found in that directory."

        try:
            cmd_parts = ["docker", "compose", "-f", manifest_name, "build"]
            if is_teledocker_repo:
                cmd_parts.append("tg-manager-bot")
            proc = await asyncio.create_subprocess_exec(
                *cmd_parts,
                cwd=str(path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600.0)
            out = (stdout.decode(errors="replace") + stderr.decode(errors="replace")).strip()
            if proc.returncode == 0:
                return True, out or "Build successful."
            return False, out or "Build failed."
        except asyncio.TimeoutError:
            return False, "Timed out after 10 minutes."
        except Exception as e:
            return False, str(e)

    async def compose_up(self, project_path: str, manifest_name: str = None) -> tuple[bool, str]:
        """Run docker compose up -d. Routes to host via SSH if path not in container."""
        path = Path(project_path)
        project_name = str(path).rstrip("/").split("/")[-1]
        is_teledocker_repo = project_name == "Tele_docker" or project_name == "Tele_docker-main"

        if not path.exists():
            # Path not in container — run on host
            if is_teledocker_repo:
                return await self._compose_cmd_on_host(project_path, "up -d --no-deps tg-manager-bot", manifest_name)
            return await self._compose_cmd_on_host(project_path, "up -d", manifest_name)

        if not manifest_name:
            for cf in COMPOSE_FILES:
                if (path / cf).exists():
                    manifest_name = cf
                    break
        if not manifest_name:
            return False, "No compose file found in that directory."

        try:
            cmd_parts = ["docker", "compose", "-f", manifest_name, "up", "-d"]
            if is_teledocker_repo:
                cmd_parts.extend(["--no-deps", "tg-manager-bot"])
            proc = await asyncio.create_subprocess_exec(
                *cmd_parts,
                cwd=str(path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300.0)
            out = (stdout.decode(errors="replace") + stderr.decode(errors="replace")).strip()
            if proc.returncode == 0:
                return True, out or "Stack brought up successfully."
            return False, out or "Unknown error."
        except asyncio.TimeoutError:
            return False, "Timed out after 5 minutes."
        except Exception as e:
            return False, str(e)

    async def compose_down(self, project_path: str, manifest_name: str = None) -> tuple[bool, str]:
        """Run docker compose down. Routes to host via SSH if path not in container."""
        path = Path(project_path)

        if not path.exists():
            return await self._compose_cmd_on_host(project_path, "down", manifest_name)

        if not manifest_name:
            for cf in COMPOSE_FILES:
                if (path / cf).exists():
                    manifest_name = cf
                    break
        if not manifest_name:
            return False, "No compose file found in that directory."

        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "compose", "-f", manifest_name, "down",
                cwd=str(path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120.0)
            out = (stdout.decode(errors="replace") + stderr.decode(errors="replace")).strip()
            if proc.returncode == 0:
                return True, out or "Stack brought down successfully."
            return False, out or "Unknown error."
        except asyncio.TimeoutError:
            return False, "Timed out after 2 minutes."
        except Exception as e:
            return False, str(e)

    async def _compose_cmd_on_host(
        self, project_path: str, sub_cmd: str, manifest_name: str = None
    ) -> tuple[bool, str]:
        """Run `docker compose <sub_cmd>` on the WSL host via SSH."""
        # Auto-detect manifest on host if not given
        if not manifest_name:
            for cf in COMPOSE_FILES:
                check_cmd = f'test -f "{project_path}/{cf}" && echo "{cf}"'
                code, out = await asyncio.to_thread(_ssh_exec, check_cmd)
                if code == 0 and out.strip():
                    manifest_name = out.strip()
                    break
        if not manifest_name:
            return False, f"No compose file found in <code>{project_path}</code> on the host."

        ssh_timeout = 320 if "up" in sub_cmd else 130
        cmd = f'cd "{project_path}" && docker compose -f "{manifest_name}" {sub_cmd} 2>&1'
        code, output = await asyncio.to_thread(_ssh_exec, cmd, timeout=ssh_timeout)
        output = output.strip() or "(no output)"
        if code == 0:
            return True, output
        return False, output


docker_engine = DockerOrchestrationEngine()