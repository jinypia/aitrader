#!/bin/bash
# verify_slack.sh - Verify Slack integration is working

echo "🔍 Verifying Slack Integration"
echo "================================"

# Check if .env file exists
if [ ! -f ".env" ]; then
    echo "❌ .env file not found!"
    exit 1
fi

# Check Slack configuration in .env
echo "📄 Checking .env configuration..."
if grep -q "SLACK_WEBHOOK_URL=" .env && grep -q "SLACK_CHANNEL=" .env; then
    echo "✅ Slack variables found in .env"
else
    echo "❌ Slack variables missing in .env"
    echo "   Add these lines to your .env file:"
    echo "   SLACK_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/WEBHOOK/URL"
    echo "   SLACK_CHANNEL=#trading-reports"
    exit 1
fi

# Check if python-dotenv is installed
echo "🐍 Checking Python dependencies..."
if python3 -c "import dotenv, slack_sdk" 2>/dev/null; then
    echo "✅ Python dependencies installed"
else
    echo "❌ Missing dependencies. Install with:"
    echo "   pip install python-dotenv slack-sdk"
    exit 1
fi

# Test environment variable loading
echo "🔧 Testing environment loading..."
WEBHOOK_URL=$(python3 -c "from dotenv import load_dotenv; import os; load_dotenv(); print(os.getenv('SLACK_WEBHOOK_URL', ''))")
CHANNEL=$(python3 -c "from dotenv import load_dotenv; import os; load_dotenv(); print(os.getenv('SLACK_CHANNEL', '#trading-reports'))")

if [ -n "$WEBHOOK_URL" ]; then
    echo "✅ SLACK_WEBHOOK_URL loaded: ${WEBHOOK_URL:0:50}..."
    echo "✅ SLACK_CHANNEL: $CHANNEL"
else
    echo "❌ SLACK_WEBHOOK_URL not loaded from .env"
    exit 1
fi

# Test Slack webhook
echo "📡 Testing Slack webhook..."
if python3 -c "
from dotenv import load_dotenv
import os
from slack_sdk import WebhookClient
load_dotenv()
webhook_url = os.getenv('SLACK_WEBHOOK_URL')
if webhook_url:
    try:
        client = WebhookClient(webhook_url)
        response = client.send(text='🧪 *Slack Integration Test*\\nScalping scheduler is configured and ready!')
        print('✅ Test message sent to Slack!')
    except Exception as e:
        print(f'❌ Test failed: {e}')
else:
    print('❌ No webhook URL')
"; then
    echo "🎉 Slack integration is working!"
    echo ""
    echo "🚀 You can now start the scheduler:"
    echo "   ./scalp_scheduler_start.sh start"
else
    echo "❌ Slack test failed"
    exit 1
fi