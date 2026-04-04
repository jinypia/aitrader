#!/usr/bin/env python3
"""
Test stock selection Slack notification
"""

import os
from dotenv import load_dotenv
from scalp_scheduler import ScalpingScheduler

# Load environment variables
load_dotenv()

def test_stock_selection_notification():
    print("🧪 Testing Stock Selection Slack Notification")
    print("=" * 50)

    # Create scheduler instance
    scheduler = ScalpingScheduler()

    if not scheduler.slack_config["enabled"]:
        print("❌ Slack not enabled. Check SLACK_WEBHOOK_URL in .env")
        return False

    print("✅ Slack integration enabled")

    # Mock selected stocks data
    mock_stocks = [
        {
            "symbol": "005930",
            "score": 87.3,
            "price_range_pct": 2.1,
            "volume_spike": 1.8,
            "rsi": 65
        },
        {
            "symbol": "000660",
            "score": 82.1,
            "price_range_pct": 1.9,
            "volume_spike": 2.2,
            "rsi": 58
        },
        {
            "symbol": "035720",
            "score": 79.8,
            "price_range_pct": 2.5,
            "volume_spike": 1.5,
            "rsi": 72
        }
    ]

    print(f"📊 Sending notification for {len(mock_stocks)} selected stocks...")

    try:
        # Send the notification
        scheduler.send_stock_selection_notification(mock_stocks)
        print("✅ Stock selection notification sent!")
        print("📱 Check your Slack channel for the message")
        return True

    except Exception as e:
        print(f"❌ Failed to send notification: {e}")
        return False

if __name__ == "__main__":
    success = test_stock_selection_notification()
    exit(0 if success else 1)