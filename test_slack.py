#!/usr/bin/env python3
"""
Test Slack integration configuration
"""

import os
from dotenv import load_dotenv
from slack_sdk import WebhookClient

# Load environment variables
load_dotenv()

def test_slack_config():
    print("🔍 Testing Slack Configuration")
    print("=" * 40)

    # Check environment variables
    webhook_url = os.getenv('SLACK_WEBHOOK_URL')
    channel = os.getenv('SLACK_CHANNEL', '#trading-reports')

    print(f"SLACK_WEBHOOK_URL: {'✅ Set' if webhook_url else '❌ Not set'}")
    print(f"SLACK_CHANNEL: {channel}")

    if not webhook_url:
        print("\n❌ Slack integration not configured!")
        print("Please set SLACK_WEBHOOK_URL in your .env file")
        return False

    # Test webhook connection
    print("
🧪 Testing webhook connection..."    try:
        client = WebhookClient(webhook_url)
        response = client.send(
            text="🧪 *Slack Integration Test*",
            attachments=[{
                "color": "good",
                "text": "Your scalping scheduler Slack integration is working!\n\nYou will receive:\n• Pre-market validation reports\n• Hourly trading session updates\n• Daily performance summaries\n• Emergency alerts",
                "footer": "Scalping Scheduler Test",
                "ts": __import__('time').time()
            }]
        )

        if response.status_code == 200:
            print("✅ Test message sent successfully!")
            print("📱 Check your Slack channel for the test message")
            return True
        else:
            print(f"❌ Test failed: HTTP {response.status_code}")
            return False

    except Exception as e:
        print(f"❌ Test error: {e}")
        return False

if __name__ == "__main__":
    success = test_slack_config()
    exit(0 if success else 1)