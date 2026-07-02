import docker
import asyncio
from pathlib import Path
from typing import AsyncGenerator
import logging

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

    async def compose_up(self, project_path: str, manifest_name: str = None) -> tuple[bool, str]:
        """Run docker compose up -d in the given path."""
        path = Path(project_path)
        if not path.exists():
            return False, f"Path not found: <code>{project_path}</code>"

        # Auto-detect manifest if not specified
        if not manifest_name:
            for candidate in ["docker-compose.yml", "docker-compose.yaml", "compose.yaml", "compose.yml"]:
                if (path / candidate).exists():
                    manifest_name = candidate
                    break
        if not manifest_name:
            return False, "No <code>docker-compose.yml</code> / <code>compose.yaml</code> found in that directory."

        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "compose", "-f", manifest_name, "up", "-d",
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
        """Run docker compose down in the given path."""
        path = Path(project_path)
        if not path.exists():
            return False, f"Path not found: <code>{project_path}</code>"

        if not manifest_name:
            for candidate in ["docker-compose.yml", "docker-compose.yaml", "compose.yaml", "compose.yml"]:
                if (path / candidate).exists():
                    manifest_name = candidate
                    break
        if not manifest_name:
            return False, "No <code>docker-compose.yml</code> / <code>compose.yaml</code> found in that directory."

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

    async def list_compose_projects(self, workspace_root: str = "data/workspaces") -> list[dict]:
        """Scan workspace for directories that contain a compose file."""
        root = Path(workspace_root)
        projects = []
        if not root.exists():
            return projects
        compose_files = ["docker-compose.yml", "docker-compose.yaml", "compose.yaml", "compose.yml"]
        for entry in sorted(root.iterdir()):
            if entry.is_dir():
                for cf in compose_files:
                    if (entry / cf).exists():
                        projects.append({"name": entry.name, "path": str(entry), "manifest": cf})
                        break
        return projects


docker_engine = DockerOrchestrationEngine()