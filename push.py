#!/usr/bin/env python3
import getpass
import os
import subprocess
import sys


def get_authenticated_url():
    """Retrieves the GITHUB_PAT from the environment, or prompts the user in the shell if missing."""
    token = os.environ.get("GITHUB_PAT")

    # If not found in environment, ask for it securely in the shell
    if not token:
        print("🔑 GITHUB_PAT not found in Replit Secrets.")
        # getpass hides the characters while you type so your token isn't visible on screen
        token = getpass.getpass(
            "📋 Paste your GitHub Personal Access Token (PAT) and press Enter: "
        ).strip()

        if not token:
            print("❌ Error: A token is required to authenticate with GitHub.")
            sys.exit(1)

        # Optional: Attempt to write it to Replit's .env configuration for future runs
        try:
            with open(".env", "a") as env_file:
                env_file.write(f"\nGITHUB_PAT={token}\n")
            print("💾 Token saved to .env for future pushes!")
        except Exception:
            print(
                "⚠️ Could not auto-save token to .env, you'll need to enter it again next time."
            )

    try:
        # Get the current remote URL
        remote_url = subprocess.check_output(
            ["git", "remote", "get-url", "origin"], text=True
        ).strip()

        # Inject the token safely
        if remote_url.startswith("https://") and "@" not in remote_url:
            return remote_url.replace("https://", f"https://{token}@")
    except subprocess.CalledProcessError:
        print("❌ Error: Could not retrieve git remote 'origin'.")

    return None


def main():
    print("🚀 Starting git push process...")

    # 1. Add all changes
    print("📝 Adding changes...")
    subprocess.run(["git", "add", "-A"], check=True)

    # 2. Get commit message
    if len(sys.argv) > 1:
        commit_msg = " ".join(sys.argv[1:])
    else:
        commit_msg = input("📝 Enter commit message: ")

    # 3. Commit
    print(f"💬 Committing with message: {commit_msg}")
    try:
        subprocess.run(["git", "commit", "-m", commit_msg], check=True)
    except subprocess.CalledProcessError:
        print("ℹ️ Nothing to commit, working tree clean. Proceeding to push...")

    # 4. Authenticated Push
    auth_url = get_authenticated_url()
    print("⬆️ Pushing to remote...")

    try:
        if auth_url:
            # Run and capture errors to prevent leaking the token if it fails
            result = subprocess.run(
                ["git", "push", auth_url, "main"],
                capture_output=True,
                text=True,
                check=True,
            )
        else:
            result = subprocess.run(
                ["git", "push", "origin", "main"],
                capture_output=True,
                text=True,
                check=True,
            )

        print("✅ Done!")
    except subprocess.CalledProcessError as e:
        print("❌ Push failed.")
        # Ensure the raw token is completely masked out of the final error dump
        token_to_mask = os.environ.get("GITHUB_PAT", "TOKEN_NOT_FOUND")
        clean_error = e.stderr.replace(token_to_mask, "****")
        print(f"Git Error Output:\n{clean_error}")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        print(f"❌ Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n⚠️ Operation cancelled by user.")
        sys.exit(0)
