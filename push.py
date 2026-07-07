#!/usr/bin/env python3
import subprocess
import sys


def main():
    print("🚀 Starting git push process...")
    
    # Add all changes
    print("📝 Adding changes...")
    subprocess.run(["git", "add", "-A"], check=True)
    
    # Get commit message
    if len(sys.argv) > 1:
        commit_msg = " ".join(sys.argv[1:])
    else:
        commit_msg = input("📝 Enter commit message: ")
    
    # Commit
    print(f"💬 Committing with message: {commit_msg}")
    subprocess.run(["git", "commit", "-m", commit_msg], check=True)
    
    # Push
    print("⬆️ Pushing to remote...")
    subprocess.run(["git", "push"], check=True)
    
    print("✅ Done!")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        print(f"❌ Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n⚠️ Operation cancelled by user.")
        sys.exit(0)
