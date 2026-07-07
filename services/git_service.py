import os
import git
import shutil
import asyncio
import logging
from pathlib import Path
from utils.ssh_helper import ssh_exec as _ssh_exec_raw

logger = logging.getLogger(__name__)


def _ssh_exec(command: str) -> tuple[int, str]:
    return _ssh_exec_raw(command, timeout=120)


class GitDeploymentEngine:
    def __init__(self, workspace_root: str = "data/workspaces"):
        self.workspace_root = Path(workspace_root)
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    # ── Deploy pipeline (existing) ────────────────────────────────────────────

    async def process_deployment(self, repo_url: str, update_msg_callback, token: str = None) -> dict:
        subfolder_path = ""
        path_parts = []
        repo_name = repo_url.split("/")[-1].replace(".git", "")

        if "/tree/" in repo_url:
            parts = repo_url.split("/tree/")
            base_url = parts[0]
            path_parts = parts[1].split("/")
            subfolder_path = "/".join(path_parts[1:])
            repo_url = f"{base_url}.git"
            repo_name = base_url.split("/")[-1].replace(".git", "")

        target_path = self.workspace_root / repo_name

        env_config = {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "echo",
            "HOME": "/tmp"
        }

        if token:
            if "github.com" in repo_url:
                clean_url = repo_url.replace("https://", "").replace("git://", "")
                repo_url = f"https://{token}@{clean_url}"
        else:
            if repo_url.startswith("https://github.com/"):
                clean_url = repo_url.replace("https://", "")
                repo_url = f"https://nobody@{clean_url}"

        if target_path.exists():
            try:
                git.Repo(target_path)
                is_valid_repo = True
            except (git.exc.InvalidGitRepositoryError, git.exc.NoSuchPathError):
                is_valid_repo = False

            if is_valid_repo:
                await update_msg_callback("🔄 Step 1/4: Target path occupied. Syncing repository...")
                def run_pull():
                    repo = git.Repo(target_path)
                    with repo.git.custom_environment(**env_config):
                        repo.remotes.origin.pull()
                try:
                    await asyncio.to_thread(run_pull)
                except Exception:
                    is_valid_repo = False

            if not is_valid_repo:
                await update_msg_callback("⚠️ Wiping corrupted or broken path footprint...")
                def purge_dir():
                    shutil.rmtree(target_path, ignore_errors=True)
                await asyncio.to_thread(purge_dir)

        if not target_path.exists():
            await update_msg_callback("📥 Step 1/4: Cloning remote repository workspace...")
            def run_clone():
                git.Repo.clone_from(repo_url, target_path, env=env_config)
            await asyncio.to_thread(run_clone)

        analysis_target = target_path / subfolder_path if subfolder_path else target_path
        await update_msg_callback("🔍 Step 2/4: Analyzing manifest structures inside workspace...")
        analysis = await self._analyze_manifests(analysis_target)
        analysis["repo_path"] = str(analysis_target)
        analysis["project_name"] = path_parts[-1] if subfolder_path else repo_name
        return analysis

    async def _analyze_manifests(self, path: Path) -> dict:
        if not path.exists():
            return {"type": "Unknown", "manifest": None}
        files = os.listdir(path)
        result = {"type": "Unknown", "manifest": None}
        for comp in ["docker-compose.yml", "docker-compose.yaml", "compose.yaml", "compose.yml"]:
            if comp in files:
                result["type"] = "Docker Compose"
                result["manifest"] = comp
                return result
        if "Dockerfile" in files:
            result["type"] = "Dockerfile Native Project"
            result["manifest"] = "Dockerfile"
            content = (path / "Dockerfile").read_text()
            if "python" in content.lower():
                result["type"] += " (Python)"
            elif "node" in content.lower():
                result["type"] += " (NodeJS)"
            elif "golang" in content.lower() or "go " in content.lower():
                result["type"] += " (Go)"
        return result

    # ── List repos ────────────────────────────────────────────────────────────

    async def list_repos(self) -> list[dict]:
        """
        Returns git repos from two sources:
        1. Container's data/workspaces
        2. GIT_SCAN_PATHS on the WSL host via SSH (if configured in .env)
        """
        repos = []

        # Container-local workspaces
        if self.workspace_root.exists():
            for entry in sorted(self.workspace_root.iterdir()):
                if entry.is_dir():
                    try:
                        repo = git.Repo(entry)
                        remote_url = ""
                        if repo.remotes:
                            remote_url = repo.remotes.origin.url
                        if "@" in remote_url and "https://" in remote_url:
                            remote_url = "https://" + remote_url.split("@")[-1]
                        repos.append({
                            "name":     entry.name,
                            "path":     str(entry),
                            "remote":   remote_url,
                            "location": "container",
                        })
                    except (git.exc.InvalidGitRepositoryError, git.exc.NoSuchPathError):
                        pass

        # Host paths via SSH
        from config.settings import runtime_settings
        scan_paths_raw = runtime_settings.GIT_SCAN_PATHS
        if scan_paths_raw:
            for scan_path in [p.strip() for p in scan_paths_raw.split(",") if p.strip()]:
                repos.extend(await self._list_host_repos(scan_path))

        return repos

    async def _list_host_repos(self, scan_path: str) -> list[dict]:
        """Find .git directories up to 2 levels deep inside scan_path on the host."""
        cmd = f'find "{scan_path}" -maxdepth 2 -name ".git" -type d 2>/dev/null'
        code, output = await asyncio.to_thread(_ssh_exec, cmd)
        if code != 0 or not output.strip():
            return []

        repos = []
        for git_dir in output.strip().splitlines():
            repo_path = git_dir.replace("/.git", "").strip()
            repo_name = repo_path.rstrip("/").split("/")[-1]
            _, remote_url = await asyncio.to_thread(
                _ssh_exec, f'git -C "{repo_path}" remote get-url origin 2>/dev/null'
            )
            remote_url = remote_url.strip()
            if "@" in remote_url and "https://" in remote_url:
                remote_url = "https://" + remote_url.split("@")[-1]
            repos.append({
                "name":     repo_name,
                "path":     repo_path,
                "remote":   remote_url,
                "location": "host",
            })
        return repos

    # ── Clone ─────────────────────────────────────────────────────────────────

    async def clone(self, repo_url: str, dest_path: str = None, token: str = None) -> tuple[bool, str]:
        """Clone a repo to container workspace or a custom path."""
        repo_name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")
        target_path = Path(dest_path) if dest_path else self.workspace_root / repo_name

        env_config = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo", "HOME": "/tmp"}

        if token and "github.com" in repo_url:
            clean_url = repo_url.replace("https://", "").replace("git://", "")
            repo_url = f"https://{token}@{clean_url}"

        if target_path.exists():
            return False, f"⚠️ Path already exists: <code>{target_path}</code>\nUse /gitpull to update it."

        try:
            def run_clone():
                git.Repo.clone_from(repo_url, target_path, env=env_config)
            await asyncio.to_thread(run_clone)
            return True, str(target_path)
        except Exception as e:
            return False, str(e)

    # ── Pull ──────────────────────────────────────────────────────────────────

    async def pull(self, repo_path: str) -> tuple[bool, str]:
        """
        Pull latest changes.
        Routes to local GitPython if path exists in container,
        otherwise runs git pull on the WSL host via SSH.
        """
        path = Path(repo_path)
        if path.exists():
            return await self._pull_local(path)
        logger.info(f"Path not in container, routing pull to host: {repo_path}")
        return await self._pull_host(repo_path)

    async def _pull_local(self, path: Path) -> tuple[bool, str]:
        try:
            def run():
                repo = git.Repo(path)
                with repo.git.custom_environment(GIT_TERMINAL_PROMPT="0"):
                    result = repo.remotes.origin.pull()
                summary = [f"• {i.ref}: {i.note}" if i.note else f"• {i.ref}" for i in result]
                return summary or ["Already up to date."]
            lines = await asyncio.to_thread(run)
            return True, "\n".join(lines)
        except git.exc.InvalidGitRepositoryError:
            return False, f"❌ Not a git repository: <code>{path}</code>"
        except Exception as e:
            return False, str(e)

    async def _pull_host(self, repo_path: str) -> tuple[bool, str]:
        code, output = await asyncio.to_thread(_ssh_exec, f'git -C "{repo_path}" pull 2>&1')
        output = output.strip() or "(no output)"
        if code == 0:
            return True, output
        return False, f"Host git pull failed (exit {code}):\n{output}"


git_engine = GitDeploymentEngine()
