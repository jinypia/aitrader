#!/bin/bash
# setup_slack.sh - Easy Slack integration setup for scalping scheduler

set -e

echo "═══════════════════════════════════════════════════"
echo "  SLACK INTEGRATION SETUP"
echo "═══════════════════════════════════════════════════"
echo

# Check if Slack SDK is installed
echo "📦 Checking Slack SDK..."
if ! python3 -c "import slack_sdk" 2>/dev/null; then
    echo "❌ Slack SDK not installed. Installing..."
    pip install slack-sdk
    echo "✅ Slack SDK installed"
else
    echo "✅ Slack SDK already installed"
fi

echo
echo "🔗 Slack Webhook Setup:"
echo "───────────────────────"
echo "1. Go to: https://api.slack.com/apps"
echo "2. Create new app 'Scalping Bot'"
echo "3. Enable 'Incoming Webhooks'"
echo "4. Add webhook to your channel"
echo "5. Copy the webhook URL"
echo

# Prompt for webhook URL
read -p "📋 Paste your Slack webhook URL: " webhook_url

if [ -z "$webhook_url" ]; then
    echo "❌ No webhook URL provided. Setup cancelled."
    exit 1
fi

# Validate URL format
if [[ ! $webhook_url =~ ^https://hooks\.slack\.com/services/ ]]; then
    echo "❌ Invalid webhook URL format. Should start with https://hooks.slack.com/services/"
    exit 1
fi

echo
echo "📝 Setting environment variables..."

# Add to shell profile
SHELL_PROFILE=""
if [ -n "$ZSH_VERSION" ]; then
    SHELL_PROFILE="$HOME/.zshrc"
elif [ -n "$BASH_VERSION" ]; then
    SHELL_PROFILE="$HOME/.bashrc"
else
    SHELL_PROFILE="$HOME/.profile"
fi

# Backup existing profile
if [ -f "$SHELL_PROFILE" ]; then
    cp "$SHELL_PROFILE" "${SHELL_PROFILE}.backup.$(date +%Y%m%d_%H%M%S)"
fi

# Add environment variables
echo "" >> "$SHELL_PROFILE"
echo "# Scalping Bot Slack Integration" >> "$SHELL_PROFILE"
echo "export SLACK_WEBHOOK_URL=\"$webhook_url\"" >> "$SHELL_PROFILE"
echo "export SLACK_CHANNEL=\"#trading-reports\"" >> "$SHELL_PROFILE"

echo "✅ Environment variables added to $SHELL_PROFILE"

# Source the profile
echo
echo "🔄 Reloading shell configuration..."
source "$SHELL_PROFILE"

echo
echo "🧪 Testing Slack connection..."

# Test the webhook
python3 << EOF
import os
from slack_sdk import WebhookClient

webhook_url = os.getenv('SLACK_WEBHOOK_URL')
if not webhook_url:
    print("❌ SLACK_WEBHOOK_URL not set")
    exit(1)

try:
    client = WebhookClient(webhook_url)
    response = client.send(
        text="🧪 *Scalping Bot Connected!*",
        attachments=[{
            "color": "good",
            "text": "Slack integration setup complete!\n\nYou will now receive:\n• Pre-market validation reports\n• Hourly trading session updates\n• Daily performance summaries\n• Emergency alerts",
            "footer": "Scalping Scheduler",
            "ts": __import__('time').time()
        }]
    )
    
    if response.status_code == 200:
        print("✅ Test message sent to Slack!")
        print("📱 Check your Slack channel for the test message")
    else:
        print(f"❌ Test failed: HTTP {response.status_code}")
        
except Exception as e:
    print(f"❌ Test error: {e}")
EOF

echo
echo "🎉 Slack integration setup complete!"
echo
echo "📋 Summary:"
echo "• Webhook URL configured"
echo "• Environment variables set"
echo "• Test message sent"
echo
echo "🚀 Ready to use:"
echo "• Start scheduler: ./scalp_scheduler_start.sh start"
echo "• Check status: python scalp_scheduler.py --check"
echo
echo "📖 Documentation: docs/SLACK_INTEGRATION.md"
echo
echo "═══════════════════════════════════════════════════"