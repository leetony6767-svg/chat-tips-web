#!/bin/bash
# Quick GitHub Push Script for Telegram Bot Repo
# Usage: ./push_github.sh [GITHUB_TOKEN]
# If no token provided, you'll be prompted

REPO_DIR="/c/Users/master/WorkBuddy AI/spaces/telegram-render-bot"
REMOTE="https://github.com/babywu519-eng/tg-gemini-assistant.git"

echo "=========================================="
echo "Telegram Bot GitHub Push Helper"
echo "=========================================="
echo ""
echo "Target repo: $REMOTE"
echo ""

if [ -n "$1" ]; then
    TOKEN="$1"
else
    echo "Please enter your GitHub Personal Access Token (PAT):"
    echo "  Create at: https://github.com/settings/tokens"
    echo "  Required scopes: repo"
    read -s TOKEN
    echo ""
fi

if [ -z "$TOKEN" ]; then
    echo "Error: No token provided"
    exit 1
fi

cd "$REPO_DIR" || { echo "Error: Directory not found"; exit 1; }

echo ""
echo "Pushing to GitHub..."
echo ""

git push -u origin main 2>&1 <<EOF
$TOKEN
${TOKEN}
EOF

EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo ""
    echo "✅ SUCCESS! Repository pushed."
    echo ""
    echo "Repository URL: https://github.com/babywu519-eng/tg-gemini-assistant"
    echo ""
    echo "Next steps:"
    echo "  1. Go to https://render.com"
    echo "  2. New → Web Service"
    echo "  3. Connect repository: babywu519-eng/tg-gemini-assistant"
    echo "  4. Add environment variables:"
    echo "     - TELEGRAM_BOT_TOKEN=8778107946:AAGv4kbl23X_9l77xcEGDKUqyVcyw-rUh7Y"
    echo "     - GEMINI_API_KEY=<your_gemini_api_key>"
    echo "  5. Click Deploy!"
else
    echo ""
    echo "❌ Push failed. Please check:"
    echo "   - Token is valid"
    echo "   - You have write access to the repo"
    echo "   - Repo name is correct"
fi

exit $EXIT_CODE
