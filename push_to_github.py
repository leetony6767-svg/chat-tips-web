#!/usr/bin/env python3
"""
GitHub Push Helper
Run this to push the Telegram Bot repo to GitHub.
You'll be prompted for your GitHub Personal Access Token (PAT).

To create a PAT:
  1. Go to https://github.com/settings/tokens
  2. Click "Generate new token (classic)"
  3. Check: repo (full control)
  4. Generate and copy the token
"""

import os
import sys
import subprocess
from pathlib import Path

REPO_DIR = "/c/Users/master/WorkBuddy AI/spaces/telegram-render-bot"
REMOTE_URL = "https://github.com/babywu519-eng/tg-gemini-assistant.git"

print("Telegram + Gemini Bot - GitHub Push Helper")
print("=" * 50)
print(f"\nTarget repo: {REMOTE_URL}")
print()

# Check if we're in the right directory
if not Path(REPO_DIR).joinpath(".git").exists():
    print(f"❌ Repo not found at {REPO_DIR}")
    sys.exit(1)

# Try to get token from environment first
token = os.environ.get("GITHUB_TOKEN", "")
if not token:
    # Ask user for token via secure prompt
    import getpass
    print("Please enter your GitHub Personal Access Token (PAT):")
    print("  Create one at: https://github.com/settings/tokens")
    print("  Required scope: repo (full control)")
    print("  (The token will be used once and not stored)\n")
    token = getpass.getpass("GitHub Token: ")

if not token:
    print("❌ No token provided.")
    sys.exit(1)

print("\nPushing to GitHub...")
print()

# Set credential helper to use token
env = os.environ.copy()
env["GIT_ASKPASS"] = "/bin/echo"
env["GIT_USERNAME"] = "babywu519-eng"
env["GIT_PASSWORD"] = token

result = subprocess.run(
    ["git", "push", "-u", f"https://{token}@github.com/babywu519-eng/tg-gemini-assistant.git", "main"],
    cwd=REPO_DIR,
    capture_output=True,
    text=True,
    timeout=60
)

if result.returncode == 0:
    print("✅ SUCCESS! Repository pushed to GitHub.")
    print(f"\n📦 Repository URL: https://github.com/babywu519-eng/tg-gemini-assistant")
    print(f"\nNext steps:")
    print(f"  1. Go to https://render.com")
    print(f"  2. New → Web Service")
    print(f"  3. Connect GitHub repo: babywu519-eng/tg-gemini-assistant")
    print(f"  4. Add environment variables:")
    print(f"     - TELEGRAM_BOT_TOKEN=8778107946:AAHSs1D43_Qmlr-s6UV5Oia9jMFO_-DNqiY")
    print(f"     - GEMINI_API_KEY=<your_gemini_api_key>")
    print(f"  5. Deploy!")
else:
    print(f"❌ Failed to push:")
    print(result.stderr)
    sys.exit(1)
