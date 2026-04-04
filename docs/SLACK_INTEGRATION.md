# Slack Integration Setup Guide

## 🎯 Overview

Receive hourly performance reports and daily summaries via Slack during scalping automation.

## 📋 Features

- ✅ **Stock Selection Updates**: Real-time notifications when new stocks are selected
- ✅ **Hourly Reports**: Session-by-session P&L updates
- ✅ **Daily Summary**: End-of-day performance overview
- ✅ **Validation Alerts**: Pre-market strategy validation results
- ✅ **Emergency Alerts**: Loss limit warnings and trading halts
- ✅ **Rich Formatting**: Color-coded messages with attachments

---

## 🚀 Quick Setup (3 Steps)

### Step 1: Create Slack App & Webhook

1. Go to [Slack API](https://api.slack.com/apps)
2. Click "Create New App" → "From scratch"
3. Name: "Scalping Bot", Workspace: Your workspace
4. Go to "Incoming Webhooks" → "Add New Webhook to Workspace"
5. Select channel (e.g., #trading-reports)
6. Copy the **Webhook URL** (starts with `https://hooks.slack.com/...`)

### Step 2: Set Environment Variables

```bash
# Add to your ~/.bashrc or ~/.zshrc
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/YOUR/WEBHOOK/URL"
export SLACK_CHANNEL="#trading-reports"  # Optional, defaults to this

# Reload shell
source ~/.bashrc
```

### Step 3: Test Integration

```bash
cd /Users/superarchi/aitrader

# Test Slack connection
python scalp_scheduler.py --check

# Should show:
# ✅ Slack integration enabled
#    Channel: #trading-reports

# Send test message
python3 -c "
import os
from slack_sdk import WebhookClient
client = WebhookClient(os.getenv('SLACK_WEBHOOK_URL'))
client.send(text='🧪 Test message from Scalping Bot')
"
```

---

## 📊 Message Types & Timing

### 1. Pre-Market Validation (8:30 AM)

**Success Example:**
```
✅ Strategy Validated

Pre-market validation passed!
• Win Rate: 62.3%
• Threshold: 55.0%
• Total P&L: ₩295,000

🚀 Ready to trade today!
```

**Failure Example:**
```
⚠️ Strategy Validation Failed

Win rate below threshold
• Actual: 48.2%
• Required: 55.0%
• Total P&L: ₩-45,000

❌ Trading cancelled for today
```

### 2. Stock Selection Updates (Every 2 hours)

```
🔄 Stock Selection Update (09:00 KST)

📊 Top 5 Stocks Selected:
• #1 005930: Score 87.3, Range 2.1%, Vol 1.8x, RSI 65
• #2 000660: Score 82.1, Range 1.9%, Vol 2.2x, RSI 58
• #3 035720: Score 79.8, Range 2.5%, Vol 1.5x, RSI 72
• #4 051910: Score 76.4, Range 1.7%, Vol 1.9x, RSI 61
• #5 247540: Score 74.2, Range 2.3%, Vol 1.6x, RSI 68

💡 Next session: 11:00 KST
```

### 3. Hourly Session Reports (Every 2 hours)

```
📊 Session #2 Complete (11:00 KST)

📊 Summary:
• Stocks traded: 5
• Successful: 4
• Session P&L: ₩183,000

📈 Stock Performance:
• ✅ 005930: 6 trades, 66.7% WR, ₩75,000
• ✅ 000660: 4 trades, 75.0% WR, ₩55,000
• ⚠️  035720: 2 trades, 50.0% WR, ₩10,000
• 🔍 051910: 0 trades, 0.0% WR, ₩0
• ✅ 247540: 3 trades, 66.7% WR, ₩43,000
```

### 3. Emergency Alerts (Loss Limits)

```
🚨 Trading Stopped - Loss Limit

🚨 EMERGENCY STOP

Daily loss limit hit!
• Loss: ₩-52,000
• Limit: ₩-50,000
• Trading halted for today
```

### 4. Daily Summary (3:30 PM)

```
📊 Daily Summary: ₩612,000

Daily Trading Summary (2026-04-04)

📊 Overview:
• Sessions: 4
• Total P&L: ₩612,000
• Estimated Trades: ~92

🏆 Top Performers:
• 005930: ₩185,000 (62.0% WR)
• 034730: ₩125,000 (68.0% WR)
• 039490: ₩105,000 (59.0% WR)
```

---

## 📅 Notification Schedule (KST Timezone)

| Time | Event | Notification Type |
|------|-------|-------------------|
| 08:30 | Pre-market validation | Validation Success/Failure |
| 09:00 | Market open + stock selection | Stock Selection Update |
| 11:00 | Session #1 complete + re-selection | Hourly Report + Stock Selection |
| 13:00 | Session #2 complete + re-selection | Hourly Report + Stock Selection |
| 15:00 | Session #3 complete + re-selection | Hourly Report + Stock Selection |
| 15:30 | Market close summary | Daily Summary |
| Any time | Loss limit hit | Emergency Alert |

**Notes:**
- All times are in Korea Standard Time (UTC+9)
- Stock selection occurs at market open (9:00) and every 2 hours thereafter
- Notifications only sent on weekdays during market hours
- Emergency alerts can occur at any time if loss limits are breached

---

## ⚙️ Configuration Options

Edit `scalp_scheduler.py` to customize:

```python
# Slack configuration (lines ~60-70)
self.slack_config = {
    "webhook_url": os.getenv("SLACK_WEBHOOK_URL", ""),
    "channel": os.getenv("SLACK_CHANNEL", "#trading-reports"),
    "enabled": bool(os.getenv("SLACK_WEBHOOK_URL", "")),
    "send_hourly_reports": True,      # Session reports every 2 hours
    "send_daily_summary": True,       # End-of-day summary
    "send_validation_reports": True,  # Pre-market validation
}
```

### Disable Specific Notifications

```python
# Only daily summaries
"send_hourly_reports": False,
"send_daily_summary": True,
"send_validation_reports": False,
```

---

## 🎨 Message Formatting

### Colors
- 🟢 **Green** (`good`): Positive P&L, validation success
- 🟡 **Yellow** (`warning`): Neutral P&L, validation failure
- 🔴 **Red** (`danger`): Negative P&L, errors, emergency stops

### Emojis Used
- ✅ Success / Positive
- ⚠️ Warning / Neutral
- ❌ Error / Negative
- 🔍 No activity
- 🚀 New session
- 📊 Reports
- 💰 Money
- 🏆 Rankings

---

## 🔧 Troubleshooting

### "Slack integration disabled"

**Cause**: Missing `SLACK_WEBHOOK_URL` environment variable

**Fix**:
```bash
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/YOUR/WEBHOOK/URL"
```

### "Slack message failed: 400"

**Cause**: Invalid webhook URL or permissions

**Fix**:
1. Check webhook URL is correct
2. Ensure bot has permission to post in channel
3. Test with simple message

### "ModuleNotFoundError: slack_sdk"

**Cause**: Slack SDK not installed

**Fix**:
```bash
pip install slack-sdk
```

### Test Slack Connection

```bash
# Quick test
python3 -c "
import os
from slack_sdk import WebhookClient

url = os.getenv('SLACK_WEBHOOK_URL')
if not url:
    print('❌ SLACK_WEBHOOK_URL not set')
    exit(1)

client = WebhookClient(url)
response = client.send(text='🧪 Test from Scalping Bot')
print(f'✅ Test sent: {response.status_code}')
"
```

---

## 📱 Mobile Notifications

### Slack Mobile App
- Get instant notifications on your phone
- Tap to view detailed reports
- Set custom notification sounds

### Notification Settings
1. Open Slack app
2. Go to channel settings
3. Enable notifications for @ mentions and keywords
4. Add keywords: "Scalping", "Trading", "P&L"

---

## 🔒 Security Best Practices

### 1. Environment Variables
- Never hardcode webhook URLs in code
- Use environment variables for secrets
- Keep webhook URLs private

### 2. Channel Permissions
- Create dedicated channel (#trading-reports)
- Limit access to authorized users
- Use private channels for sensitive info

### 3. Rate Limiting
- Slack allows 1 message/second per webhook
- System sends max 1 message every 2 hours
- Well within limits

---

## 📊 Advanced: Custom Messages

Modify message formatting in `scalp_scheduler.py`:

```python
def format_hourly_report(self, session_results: Dict, session_num: int) -> str:
    """Customize hourly report format."""
    # Your custom formatting here
    return custom_message
```

---

## 🎯 Integration Status

✅ **Pre-market validation alerts** - READY
✅ **Hourly session reports** - READY
✅ **Daily summary reports** - READY
✅ **Emergency stop alerts** - READY
✅ **Rich formatting & colors** - READY
✅ **Mobile notifications** - READY

**Status**: 🟢 PRODUCTION READY

---

## 🚀 Next Steps

1. **Setup Slack webhook** (5 minutes)
2. **Set environment variables**
3. **Test connection**
4. **Start scheduler** with notifications enabled
5. **Monitor reports** in Slack

---

**Created**: 2026-04-04
**Version**: 1.0
**Integration**: Slack Webhooks
