import os
import git
import shutil
import asyncio
import logging
from pathlib import Path

class GitDeploymentEngine:
    def __init__(self, workspace_root: str = "data/workspaces"):
        self.workspace_root = Path(workspace_root)
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    async def process_deployment(self, repo_url: str, update_msg_callback, token: str = None) -> dict:
        subfolder_path = ""
        path_parts = []
        repo_name = repo_url.split("/")[-1].replace(".git", "")
        
        # Parse GitHub subfolder paths (.../tree/master/subfolder)
        if "/tree/" in repo_url:
            parts = repo_url.split("/tree/")
            base_url = parts[0]  # https://github.com/owner/repo
            path_parts = parts[1].split("/")
            # path_parts[0] is the branch name (e.g., 'master' or 'main')
            subfolder_path = "/".join(path_parts[1:])  # e.g., 'flask' or 'go-app'
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
        
        # Check repository viability
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

        # Shift inspection context to the subfolder if one was parsed
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
        
        compose_options = ["docker-compose.yml", "docker-compose.yaml", "compose.yaml", "compose.yml"]
        for comp in compose_options:
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
                # ◄ RESTORED GO LANGUAGE AUTO-DETECTION
                result["type"] += " (Go)"
            
        return result

    async def list_repos(self) -> list[dict]:
        """Return all valid git repos under workspace_root."""
        repos = []
        if not self.workspace_root.exists():
            return repos
        for entry in sorted(self.workspace_root.iterdir()):
            if entry.is_dir():
                try:
                    repo = git.Repo(entry)
                    remote_url = ""
                    if repo.remotes:
                        remote_url = repo.remotes.origin.url
                    # Strip embedded tokens from display URL
                    if "@" in remote_url and "https://" in remote_url:
                        remote_url = "https://" + remote_url.split("@")[-1]
                    repos.append({
                        "name": entry.name,
                        "path": str(entry),
                        "remote": remote_url,
                    })
                except (git.exc.InvalidGitRepositoryError, git.exc.NoSuchPathError):
                    pass
        return repos

    async def clone(self, repo_url: str, dest_path: str = None, token: str = None) -> tuple[bool, str]:
        repo_name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")
        target_path = Path(dest_path) if dest_path else self.workspace_root / repo_name

        env_config = {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "echo",
            "HOME": "/tmp"
        }

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

    async def pull(self, repo_path: str) -> tuple[bool, str]:
        """Pull latest changes in an existing local repo."""
        path = Path(repo_path)
        if not path.exists():
            return False, f"❌ Path not found: <code>{repo_path}</code>"

        try:
            def run_pull():
                repo = git.Repo(path)
                with repo.git.custom_environment(GIT_TERMINAL_PROMPT="0"):
                    result = repo.remotes.origin.pull()
                # Summarize what changed
                summary = []
                for info in result:
                    summary.append(f"• {info.ref}: {info.note}" if info.note else f"• {info.ref}")
                return summary or ["Already up to date."]
            lines = await asyncio.to_thread(run_pull)
            return True, "\n".join(lines)
        except git.exc.InvalidGitRepositoryError:
            return False, f"❌ Not a git repository: <code>{repo_path}</code>"
        except Exception as e:
            return False, str(e)


git_engine = GitDeploymentEngine()