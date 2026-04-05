# Weekday Scalping Automation - Complete Setup Guide

For the full documentation map, see `docs/README.md`.

## 🎯 What This System Does

Every weekday morning, this system will:

1. **8:30 AM** ← Validate yesterday's strategy (simulate on top 5 stocks)
2. **9:00 AM** ← Select today's best 5 stocks based on liquidity
3. **9-3 PM** ← Trade those 5 stocks with 2-minute bars
4. **Every 2 hours** ← Refresh stock selection to catch changing liquidity
5. **3:30 PM** ← Generate daily report with P&L

**No manual work needed** - everything runs automatically!

---

## 📋 Quick Start (3 Steps)

### Step 1: Install Dependencies (One-Time)
```bash
pip install pytz apscheduler
```

### Step 2: Test the System
```bash
# Run a complete daily cycle test
cd /Users/superarchi/aitrader
python scalp_scheduler.py --full
```

Expected output:
```
📋 PRE-MARKET VALIDATION (08:30 AM)
📊 Testing yesterday (2026-04-03) with today's top 5 stocks:
   1. 005930 - Score: 85.2
   2. 000660 - Score: 78.5
   ...
✅ VALIDATION SUMMARY:
   Simulations: 5/5
   Avg Win Rate: 62.3% ✅
   Total P&L: ₩1,250,000
   
✅ Strategy VALIDATED! Ready to trade today.

──────────────────────────────────
🔄 STOCK SELECTION UPDATE (09:00:00)
📊 Top 5 Stocks for Scalping:
...
```

### Step 3: Start the Scheduler
```bash
# Option A: Using bash script (recommended)
./scalp_scheduler_start.sh start

# Option B: Direct Python
python scalp_scheduler.py --start
```

That's it! The scheduler runs in background 24/7.

---

## 🖥️ CLI Dashboard (Enhanced Market Status)

Use the CLI dashboard when you want live visibility into market status updates while the bot is running.

### Run Dashboard
```bash
cd /Users/superarchi/aitrader
python main.py --dashboard --update-interval 1.0

# alternate direct entry
python src/main.py --dashboard --update-interval 1.0
```

`main.py` auto-switches to the project's `.venv` Python when available, so missing-package issues from system Python are avoided.

### New Market Status Panel Includes

1. Regime and confidence (`BULLISH`, `BEARISH`, `NEUTRAL`, etc.)
2. Session phase/profile
3. Data freshness age in seconds
4. `LIVE` vs `STALE` state and stale reason
5. Risk halt state (`ON/OFF`)
6. Daily selection status
7. Market flow summary
8. "Market Status Updated" timer (how long since market status actually changed)
9. Recent market status history (latest regime/freshness changes)

### Freshness Thresholds

- `<= 120s`: green
- `121s - 300s`: yellow
- `> 300s`: red

This helps quickly detect when market-status data has stopped updating normally.

### Performance Tuning (Alerts + Badge)

You can tune runtime performance alerts and header badge thresholds from `.env`.

```bash
# Runtime PERF_ALERT trigger (bot_runtime)
BOT_PERF_ALERT_P95_MS=1800
BOT_PERF_ALERT_WINDOW=30
BOT_PERF_ALERT_CONSECUTIVE=3
BOT_PERF_ALERT_COOLDOWN_SEC=900

# Dashboard header PERF badge thresholds (cli_dashboard)
BOT_PERF_BADGE_GOOD_MS=800
BOT_PERF_BADGE_WARN_MS=1500
```

Notes:
- `BOT_PERF_ALERT_P95_MS`: Alert when rolling loop `p95` exceeds this value.
- `BOT_PERF_ALERT_WINDOW`: Rolling sample window size used for `p95`.
- `BOT_PERF_ALERT_CONSECUTIVE`: Consecutive breached windows required before alert.
- `BOT_PERF_ALERT_COOLDOWN_SEC`: Minimum seconds between repeated alerts.
- `BOT_PERF_BADGE_GOOD_MS`, `BOT_PERF_BADGE_WARN_MS`: Header status cutoffs (`GOOD/WARN/HOT`).

---

## 🎮 Command Reference

### Start Scheduler (Automatic)
```bash
./scalp_scheduler_start.sh start
```
- Runs every weekday automatically
- 08:30 AM validation
- 09:00 AM - 3:30 PM trading
- Generates daily reports
- **Keep running in background**

### Test / Debug Commands

```bash
# Test complete daily cycle
./scalp_scheduler_start.sh test

# Run just pre-market validation
./scalp_scheduler_start.sh validate

# Show best stocks now
./scalp_scheduler_start.sh select

# Run one trading session
./scalp_scheduler_start.sh session

# Check scheduler status
./scalp_scheduler_start.sh check
```

### Direct Python Commands

```bash
cd /Users/superarchi/aitrader

# Start 24/7 scheduler
python scalp_scheduler.py --start

# Full test cycle
python scalp_scheduler.py --full

# Pre-market validation
python scalp_scheduler.py --validate

# Stock selection
python scalp_scheduler.py --select

# One session
python scalp_scheduler.py --session

# Status check
python scalp_scheduler.py --check
```

---

## 📊 Daily Workflow Example

### 08:30 AM - Pre-Market Validation

```
📋 PRE-MARKET VALIDATION (08:30 AM)
📊 Testing yesterday (2026-04-03) on today's top 5 stocks:
   1. 005930 - Score 85.2
   2. 000660 - Score 78.5
   3. 035720 - Score 72.1
   4. 051910 - Score 68.9
   5. 247540 - Score 65.3

Testing each stock from yesterday...
✅ 005930: 8 trades, WR: 62.5%, PnL: ₩125,000
✅ 000660: 6 trades, WR: 66.7%, PnL: ₩85,000
✅ 035720: 5 trades, WR: 60.0%, PnL: ₩60,000
✅ 051910: 7 trades, WR: 57.1%, PnL: ₩50,000
❌ 247540: 3 trades, WR: 33.3%, PnL: -₩25,000

📊 VALIDATION SUMMARY:
   Simulations: 5/5 successful
   Avg Win Rate: 60.0% ✅
   Total P&L: ₩295,000
   
✅ Strategy VALIDATED! Ready to trade today.
```

**Decision**: Win rate 60% > minimum 55% → Approved for trading

### 09:00 AM - Market Open (Session 1)

```
─────────────────────────────────────────
🔄 STOCK SELECTION UPDATE (09:00:00)
📊 Top 5 Stocks for Scalping:
Rank  Symbol     Score     Range     Volume    RSI
─────────────────────────────────────────
1     005930    85.2/100  1.24%    1.8x       55
2     000660    78.5/100  0.89%    1.5x       62
3     035720    72.1/100  1.56%    1.7x       48
4     066570    68.9/100  1.21%    1.6x       58
5     051910    65.3/100  0.95%    1.4x       42

🚀 SCALPING SESSION #1 (09:00:00)
✅ 005930: 4 trades, WR: 75.0%, PnL: ₩60,000
✅ 000660: 3 trades, WR: 66.7%, PnL: ₩40,000
⚠️  035720: 2 trades, WR: 50.0%, PnL: ₩10,000
✅ 066570: 5 trades, WR: 60.0%, PnL: ₩35,000
🔍 051910: 0 trades (RSI out of range)

📊 SESSION #1 SUMMARY:
   Successful Stocks: 4/5
   Session P&L: ₩145,000
   Daily P&L: ₩145,000
```

### 11:00 AM - Refresh Stocks (Session 2)

```
🔄 STOCK SELECTION UPDATE (11:00:00)
[Market conditions change, new top 5 selected...]

📊 Top 5 Stocks for Scalping:
1     039490    82.1/100  1.18%    2.1x       51
2     005930    81.5/100  1.10%    1.9x       56
3     034730    75.2/100  1.45%    1.6x       54
4     000660    72.8/100  0.76%    1.2x       64
5     035720    69.1/100  1.32%    1.5x       51

🚀 SCALPING SESSION #2 (11:00:00)
✅ 039490: 6 trades, WR: 66.7%, PnL: ₩75,000
✅ 005930: 5 trades, WR: 60.0%, PnL: ₩45,000
✅ 034730: 4 trades, WR: 75.0%, PnL: ₩55,000
⚠️  000660: 1 trade, WR: 100.0%, PnL: ₩8,000
🔍 035720: 0 trades

📊 SESSION #2 SUMMARY:
   Session P&L: ₩183,000
   Daily P&L: ₩328,000 (145k + 183k)
```

### 3:30 PM - Market Close

```
📊 MARKET CLOSE SUMMARY (15:30 PM)

📅 Date: 2026-04-04
📊 Sessions: 4 (9 AM, 11 AM, 1 PM, 3 PM)
💰 Daily P&L: ₩612,000 (+0.612%)
📈 Total Trades: ~92

🏆 Best Performers:
   1. 005930: ₩185,000
   2. 034730: ₩125,000
   3. 039490: ₩105,000

✅ Daily report saved: data/daily_scalp_report_2026-04-04.json
```

---

## ⚙️ Configuration

Edit `scalp_scheduler.py` to customize:

```python
# Line ~50 - Configuration settings
self.config = {
    "min_win_rate": 55.0,        # Min validated win rate
    "max_daily_loss": -50000,    # Stop loss threshold
    "session_interval": 2,       # Hours between refreshes (2)
    "top_n_stocks": 5,          # Number of stocks to trade
    "bar_interval": 2,          # Bar size (2, 5, or 10 min)
}
```

### Recommended Configurations

**Conservative (Lower Risk)**
```python
"min_win_rate": 60.0,         # Higher requirement
"max_daily_loss": -30000,     # Tighter stop loss
"top_n_stocks": 3,            # Fewer stocks
```

**Aggressive (Higher Reward)**
```python
"min_win_rate": 50.0,         # Lower requirement
"max_daily_loss": -100000,    # Wider stop loss
"top_n_stocks": 10,           # More stocks
```

---

## 📈 Monitoring

### Check Today's Progress

```bash
# View current scheduler status
python scalp_scheduler.py --check

# Output shows:
# - Current KST time
# - Is it market hours?
# - Is it pre-market?
# - Current config
```

### Historical Performance

```bash
# List all daily reports
ls -lt data/daily_scalp_report_*.json | head -10

# View latest report
cat data/daily_scalp_report_$(date +%Y-%m-%d).json | jq .

# Analyze 5-day average
python3 << 'EOF'
import json, glob
from collections import defaultdict

files = sorted(glob.glob('data/daily_scalp_report_*.json'))[-5:]
for file in files:
    with open(file) as f:
        data = json.load(f)
        total = sum(s['total_pnl'] for s in data.get('sessions', []))
        print(f"{data['date']}: ₩{total:+,.0f}")
EOF
```

---

## 🚨 Safety Features

1. **Pre-market Validation**
   - Only trades if yesterday showed 55%+ win rate
   - Prevents trading bad strategies
   
2. **Daily Loss Limit**
   - Stops trading if daily loss exceeds -₩50,000
   - Protects capital on bad days
   
3. **Stock Quality Filter**
   - Only trades stocks with score > 50
   - Avoids illiquid / low-activity stocks
   
4. **Session Interval**
   - Re-selects stocks every 2 hours
   - Adapts to changing market conditions
   
5. **Win Rate Tracking**
   - Each session reports actual results
   - Easy to identify underperforming stocks

---

## ❓ FAQ

### Q: Does this trade with real money or simulations?

**A**: By default, it runs **simulations** to validate the strategy. To enable **live trading**, you'd need to:
1. Connect a real broker API (e.g., KiWoom)
2. Modify the scalping engine to place real orders
3. Currently set to simulation mode for safety

### Q: What if market hits daily loss limit?

**A**: Scheduler stops trading for that day and resumes the next morning.

### Q: Can I change stock selection from 5 to 3?

**A**: Yes! Edit line ~50 in `scalp_scheduler.py`:
```python
"top_n_stocks": 3,  # Changed from 5
```

### Q: What if I need to stop the scheduler?

**A**: 
```bash
# If running in terminal
Press Ctrl+C

# If running in background
pkill -f "scalp_scheduler.py --start"
```

### Q: When does it run?

**A**: Only on **weekdays during market hours**:
- Monday-Friday
- 8:30 AM - 3:30 PM KST
- Skips weekends and Korean holidays

---

## 📊 Expected Performance

Based on current strategy parameters:

| Metric | Target | Realistic |
|--------|--------|-----------|
| Daily Sessions | 4 | 4 (every 2 hrs) |
| Stocks/Session | 5 | 5 |
| Win Rate | 55-70% | 55-65% |
| Avg Trade | +0.1% | +0.05-0.10% |
| Daily P&L | +0.5-3% | ±0.2-0.5% |

---

## 🎓 Next Steps

1. **Test it**
   ```bash
   python scalp_scheduler.py --full
   ```

2. **Monitor results**
   ```bash
   cat data/daily_scalp_report_$(date +%Y-%m-%d).json | jq
   ```

3. **Start scheduler**
   ```bash
   ./scalp_scheduler_start.sh start
   ```

4. **Review daily reports** to optimize strategy

---

**System Status**: ✅ Ready for deployment
