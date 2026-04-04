# Weekday Stock Selection & Scalping Automation

## 📅 Daily Workflow Overview

```
WEEKDAY MORNING
├─ 08:30 AM  → Pre-market validation (simulate yesterday's top 5)
├─ 09:00 AM  → Market open + Stock selection
│
MARKET HOURS (9 AM - 3:30 PM)
├─ 09:00 AM  → Session 1: Select top 5 stocks
├─ 11:00 AM  → Session 2: Refresh top 5 stocks
├─ 13:00 PM  → Session 3: Refresh top 5 stocks  
├─ 15:00 PM  → Session 4: Final refresh
│
MARKET CLOSE
└─ 15:30 PM  → Close summary + Daily report
```

## 🚀 Quick Start

### 1. Run Full Daily Cycle (Test Mode)

```bash
# Simulate a complete day: validation → selection → trading → summary
python scalp_scheduler.py --full
```

Output:
```
📋 PRE-MARKET VALIDATION (08:30 AM)
📊 Testing yesterday (2026-04-03) with today's top 5 stocks:
   1. 005930 - Score: 85.2
   2. ...
   
✅ VALIDATION SUMMARY:
   Simulations: 5/5
   Avg Win Rate: 62.3% ✅
   Total P&L: ₩1,250,000

✅ Strategy VALIDATED! Ready to trade today.

─────────────────────────────────────────
🔄 STOCK SELECTION UPDATE (09:00:00)
📊 Top 5 Stocks for Scalping:
Rank  Symbol     Score     Range      Volume    RSI
─────────────────────────────────────────
1     005930    85.2/100  1.24%    1.8x       55
2     000660    78.5/100  0.89%    1.5x       62
...
```

### 2. Test Individual Steps

```bash
# Just validate yesterday's stocks
python scalp_scheduler.py --validate

# Just select today's best stocks
python scalp_scheduler.py --select

# Run one scalping session
python scalp_scheduler.py --session

# Check scheduler status
python scalp_scheduler.py --check
```

### 3. Start 24/7 Automatic Scheduler ⏰

```bash
# Install dependencies first (one-time)
pip install pytz apscheduler

# Start scheduler - runs automatically every weekday
python scalp_scheduler.py --start
```

Once started, scheduler runs automatically:
- ✅ **08:30 AM** - Pre-market validation
- ✅ **09:00 AM** - Stock selection  
- ✅ **11:00 AM** - Refresh stocks + scalp session
- ✅ **13:00 PM** - Refresh stocks + scalp session
- ✅ **15:00 PM** - Final refresh + scalp session
- ✅ **15:30 PM** - Market close summary

## 📊 Configuration

Edit default settings in `scalp_scheduler.py`:

```python
self.config = {
    "min_win_rate": 55.0,        # Min win rate to validate strategy
    "max_daily_loss": -50000,    # Stop loss for entire day (-₩50k)
    "session_interval": 2,       # Re-select stocks every 2 hours
    "top_n_stocks": 5,          # Trade exactly 5 stocks
    "bar_interval": 2,          # Use 2-minute bars
}
```

### Important Settings

| Setting | Current | Meaning |
|---------|---------|---------|
| `min_win_rate` | 55.0% | Only trade if yesterday validation showed 55%+ win rate |
| `max_daily_loss` | -₩50k | Stop trading if daily loss exceeds -₩50,000 |
| `top_n_stocks` | 5 | Always trade top 5 stocks selected each session |
| `bar_interval` | 2 min | Use 2-minute bars (vs 5 or 10) |

## 📈 Daily Reports

After each day, a JSON report is saved:

```bash
data/daily_scalp_report_2026-04-04.json
```

Contents:
```json
{
  "date": "2026-04-04",
  "sessions": [
    {
      "session_num": 1,
      "timestamp": "2026-04-04T09:00:00+09:00",
      "total_pnl": 500000,
      "stocks": [
        {
          "symbol": "005930",
          "trades": 8,
          "win_rate": 62.5,
          "pnl": 125000
        },
        ...
      ]
    },
    ...
  ]
}
```

## 🔄 How Stock Selection Works Each Session

### Every 2 Hours During Market Hours:

1. **Load current intraday data** → last 30 min of each stock
2. **Calculate metrics** → volatility, volume spike, RSI status
3. **Score each stock** → 0-100 formula:
   - Volatility (40%) - current price movement
   - Volume Spike (40%) - vs average volume
   - RSI (20%) - is it tradeable?
4. **Rank and filter** → select top 5 by score
5. **Execute scalping** → run 2-minute simulations on selected 5

### Why Every 2 Hours?

- **Captures liquidity changes** - stocks become active/inactive
- **Adapts to market conditions** - morning vs afternoon volatility different
- **Doesn't over-trade** - 2 hour window is professional interval
- **Matches human monitoring** - realistic for retail trader

## 💡 Examples

### Example 1: Morning Validation Shows Bad Strategy Performance

```
PRE-MARKET VALIDATION:
   Avg Win Rate: 48.0% ❌ (below 55% minimum)
   
⚠️  Win rate 48.0% below threshold 55%
```

**Action**: NOT approved for live trading. Review strategy on data/debug files.

### Example 2: Daily Loss Limit Hit

```
SESSION #3 SUMMARY:
   Daily P&L: ₩-52,000

🛑 STOP LOSS HIT! Stop trading for rest of day.
```

**Action**: Scheduler halts all additional trading. Resume tomorrow.

### Example 3: Successful Session

```
SESSION #1 (09:00):
✅ 005930: 8 trades, WR: 62.5%, PnL: ₩125,000
✅ 000660: 6 trades, WR: 66.7%, PnL: ₩85,000
✅ 035720: 5 trades, WR: 60.0%, PnL: ₩60,000
❌ 066570: 4 trades, WR: 25.0%, PnL: -₩20,000
⚠️  051910: 0 trades (no signals)

SESSION #1 SUMMARY:
   Successful Stocks: 5/5
   Session P&L: ₩250,000
   Daily P&L: ₩250,000
```

## 🎯 Typical Day Performance (Target)

```
Time         Action              Expected Result
─────────────────────────────────────────────────
08:30 AM     Validate            ✅ 60%+ win rate
09:00 AM     Select stocks       ✅ Top 5 identified
11:00 AM     Session 2           P&L +100k to +500k
13:00 PM     Session 3           P&L +50k to +300k
15:00 PM     Session 4           P&L +30k to +200k
15:30 PM     Close               Daily P&L: +₩500k-₩2M
```

## ⚠️ Important Notes

### 1. Weekend & Holidays
- Scheduler **skips** Saturdays/Sundays
- Skips Korean holidays automatically
- Resume Tuesday after Monday holiday

### 2. Market Hours (KST UTC+9)
- **9:00 AM** - Market opens
- **11:30 AM - 12:30 PM** - Lunch break
- **3:30 PM** - Market closes (KRX official close)

### 3. Pre-market Requirement
The scheduler requires:
- `selected_intraday_prices.json` - Current prices
- `simulation_run_config.json` - Config
- Data from yesterday for validation

## 📝 Troubleshooting

### Issue: "APScheduler not installed"

```bash
pip install apscheduler
```

### Issue: "No data found for symbol"

Check data availability:
```bash
python scalp_scheduler.py --check
```

Ensure `data/selected_intraday_prices.json` is current.

### Issue: "Can't import modules"

Make sure you're in the correct directory:
```bash
cd /Users/superarchi/aitrader
python scalp_scheduler.py --full
```

## 🎓 Advanced: Custom Scheduling

Modify `schedule_daily_jobs()` method for custom times:

```python
# Change pre-market from 08:30 to 08:00
self.scheduler.add_job(
    self.run_pre_market_validation,
    CronTrigger(
        hour=8,      # Changed from 8
        minute=0,    # Changed from 30
        day_of_week="0-4",
        timezone=str(self.tz)
    ),
)
```

## 📊 Success Metrics

Track daily performance:

```bash
# See all daily reports
ls -ltr data/daily_scalp_report_*.json

# Analyze win rates over time
python3 -c "
import json, glob
reports = sorted(glob.glob('data/daily_scalp_report_*.json'))
for report in reports[-5:]:
    with open(report) as f:
        data = json.load(f)
        total = sum(s['total_pnl'] for s in data['sessions'])
        print(f\"{data['date']}: ₩{total:+,.0f}\")
"
```

## 🚨 Safety Features

1. **Pre-market validation** - Only trade if strategy proved profitable yesterday
2. **Daily loss limit** - Stop trading if daily loss exceeds -₩50k
3. **Stock score minimum** - Only trade stocks with score > 50
4. **Win rate tracking** - Each session reports actual win rates
5. **Per-stock limit** - Max 5 stocks per session

---

**Status**: ✅ Fully Automated | ✅ Scheduler Ready | ✅ Daily Reports Enabled
