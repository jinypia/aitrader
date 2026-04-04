# Scalping Simulation - Day Price Data Guide

Get intraday 2-minute bar data for scalping strategy simulation and analysis.

## Quick Start

```bash
# Simulate on yesterday's data
python scalp_sim.py

# Simulate on specific date
python scalp_sim.py --date 2026-03-30

# Show available data
python scalp_sim.py --available

# Run with detailed trade logs
python scalp_sim.py --date 2026-03-30 --verbose

# Simulate different symbol
python scalp_sim.py --symbol 000660 --date 2026-03-30
```

## Features

### 1. Day Price Data Loading

The system loads intraday 2-minute bar data through multiple sources:

**Priority Order:**
1. **Stored Intraday Data** (`data/selected_intraday_prices.json`)
   - Pre-collected 2-minute bars with RSI, volume analysis
   - Most reliable for accurate historical replay

2. **Intraday Replay Reports** (`data/intraday_selected_replay.json`)
   - Trading reports with bar-by-bar performance data
   - Useful for understanding past strategy execution

3. **Synthetic Generation from Daily**
   - Generated 2-minute bars from daily OHLCV data
   - Realistic price movement within daily bounds
   - Uses volatility simulation and market hours distribution

### 2. Data Module: `src/scalping_data_loader.py`

**Main Functions:**

```python
from src.scalping_data_loader import get_day_price_data, get_day_data_preview

# Get intraday bars for a specific day
bars, source = get_day_price_data("005930", "2026-03-30")
# Returns: (list of 2-min bars, "data/selected_intraday_prices.json (36 bars)")

# Get preview without loading all data
preview = get_day_data_preview("005930", "2026-03-30")
# Returns: {
#   "available": True,
#   "bar_count": 36,
#   "price_range": 500.0,
#   "day_volume": 50000000,
#   ...
# }

# Get today's data or latest available
bars, source = get_today_scalping_data("005930")
```

## Available Data Sources

### Stored Intraday Prices

**File:** `data/selected_intraday_prices.json`

Contains 2-minute bars with comprehensive analysis:
- Timestamp, OHLCV data
- RSI indicators
- Volume spike detection
- Trading signals (BUY/SELL/HOLD)

Example bar:
```json
{
  "symbol": "005930",
  "bar_ts": "2026-03-29 23:40:00",
  "bar_interval_minutes": 10,
  "price": 179700.0,
  "volume": 50000000,
  "action": "HOLD",
  "decision_reason": "rsi=50.0 | blocked:trend"
}
```

### Daily Backtest Cache

**Directory:** `data/backtest_cache/`

Can be automatically converted to intraday bars:
- File format: `kr_{SYMBOL}_daily.json`
- Each daily bar generates 195 synthetic 2-minute bars
- Covers market hours: 09:00 - 15:30

### Generate Synthetic Intraday

The system can generate realistic 2-minute bars from daily data:

```python
from src.scalping_data_loader import generate_intraday_bars_from_daily

bars = generate_intraday_bars_from_daily(
    daily_open=179000,
    daily_high=180000,
    daily_low=178500,
    daily_close=179500,
    daily_volume=100000000,
    date_str="2026-03-30",
    symbol="005930",
    volatility=0.005  # 0.5% intraday volatility
)
# Returns: 195 bars covering market hours with realistic price movement
```

## Bar Data Format

Each 2-minute bar contains:

```python
{
    "timestamp": "2026-03-30 09:00",  # Market hour timestamp
    "open": 179000.0,                 # Opening price
    "high": 179500.0,                 # Highest in period
    "low": 178800.0,                  # Lowest in period
    "close": 179200.0,                # Closing price
    "volume": 5000000,                # Trading volume
    "symbol": "005930"                # Stock symbol
}
```

## Scalping Simulation

The `scalp_sim.py` script runs scalping strategy on day price data:

### Basic Usage

```bash
# Default: Samsung (005930) on yesterday
python scalp_sim.py

# Specific date
python scalp_sim.py --date 2026-03-30

# Specific symbol
python scalp_sim.py --symbol 000660 --date 2026-03-30

# With detailed output
python scalp_sim.py --date 2026-03-30 --verbose
```

### Output Example

```
🔄 Running scalping simulation...
   Symbol: 005930
   Date:   2026-03-30

📊 Loaded 195 bars from data/selected_intraday_prices.json

================================================================================
SCALPING SIMULATION RESULTS
================================================================================

📈 Symbol:        005930
📅 Date:          2026-03-30
📊 Bars:          195
📁 Source:        data/selected_intraday_prices.json (195 bars)

PERFORMANCE METRICS
────────────────────────────────────────────────────────────────────────────────
💰 Equity:       ₩10,250,000
📊 P&L:          ₩250,000 (+2.50%)
⬇️  Max Drawdown:  1.23%

TRADE STATISTICS
────────────────────────────────────────────────────────────────────────────────
📍 Total Trades:  3
✅ Wins:          2
❌ Losses:        1
🎯 Win Rate:      66.7%

✅ Results saved to: data/scalp_sim_005930_2026-03-30.json
```

## Strategy Configuration

The scalping strategy uses `src/scalping_strategy.py` with optimized parameters:

```python
from src.scalping_strategy import ScalpParams

params = ScalpParams(
    rsi_entry_min=30.0,              # Buy when RSI below this
    rsi_entry_max=70.0,              # Buy when RSI above this
    rsi_exit_overbought=75.0,        # Exit when RSI exceeds this
    rsi_exit_oversold=25.0,          # Exit when RSI drops below this
    volume_spike_threshold=2.0,      # Volume must spike 2x average
    profit_target_pct=0.8,           # Exit at +0.8% profit
    stop_loss_pct=0.5,               # Exit at -0.5% loss
    max_hold_bars=6,                 # Maximum 12-minute hold (6 × 2-min)
    min_volume_ratio=1.5,            # Volume must be 1.5x average
    trend_strength_threshold=0.2     # Trend strength minimum 0.2%
)
```

### Entry Conditions

Trade enters when ALL conditions met:
- RSI in range AND
- Volume spike >= 1.5x average AND
- Trend strength >= 0.2% AND
- Momentum >= 0.1% in last 30 minutes

### Exit Conditions

Trade exits on FIRST matching:
- **Profit Target**: Reaches +0.8% gain
- **Stop Loss**: Reaches -0.5% loss
- **Time Exit**: Held for 60 minutes (6 bars)
- **RSI Overbought**: RSI >= 75
- **RSI Oversold**: RSI <= 25

## Available Commands

### Check Data Availability

```bash
# See what dates have data available
python scalp_sim.py --available

# Check specific dates through code
python3 -c "
from src.scalping_data_loader import get_day_data_preview
from datetime import datetime, timedelta

for days in range(10):
    date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    preview = get_day_data_preview('005930', date)
    if preview.get('available'):
        print(f\"✓ {date}: {preview['bar_count']} bars\")
"
```

### Run Multiple Symbols

```bash
# Batch simulation
for symbol in 005930 000660 035720; do
    echo "Simulating $symbol..."
    python scalp_sim.py --symbol $symbol --verbose
done
```

### Generate Results File

Results automatically saved to `data/scalp_sim_{SYMBOL}_{DATE}.json`:

```json
{
  "success": true,
  "symbol": "005930",
  "date": "2026-03-30",
  "bars_processed": 39,
  "equity": 10250000,
  "pnl": 250000,
  "pnl_pct": 2.5,
  "trade_count": 3,
  "win_count": 2,
  "loss_count": 1,
  "win_rate": 66.7,
  "max_drawdown": 1.23,
  "trades": [
    {
      "timestamp": "2026-03-30 10:15",
      "type": "BUY",
      "price": 179000,
      "reason": "Scalp entry signal"
    },
    {
      "timestamp": "2026-03-30 10:45",
      "type": "SELL",
      "price": 179720,
      "pnl": 560000,
      "pnl_pct": 0.82,
      "hold_bars": 3,
      "reason": "PROFIT_TARGET"
    }
  ]
}
```

## Advanced Usage

### Custom Intraday Data

```bash
# Use custom JSON file with your own bars
python scalp_sim.py --symbol 005930 --date 2026-03-30 --data path/to/custom_bars.json
```

Expected format:
```json
{
  "bars": [
    {
      "timestamp": "2026-03-30 09:00",
      "open": 179000,
      "high": 179500,
      "low": 178800,
      "close": 179200,
      "volume": 5000000
    }
  ]
}
```

### Generate Data for Missing Dates

```python
from src.scalping_data_loader import generate_intraday_bars_from_daily
from datetime import datetime
import json

# Get daily bar from backtest cache
daily_bars = json.load(open("data/backtest_cache/kr_005930_daily.json"))

# Generate intraday bars for any daily bar
for daily_bar in daily_bars[:5]:  # First 5 days
    date_str = daily_bar["timestamp"].split()[0]
    bars = generate_intraday_bars_from_daily(
        daily_bar["open"],
        daily_bar["high"],
        daily_bar["low"],
        daily_bar["close"],
        daily_bar["volume"],
        date_str,
        "005930"
    )
    print(f"{date_str}: Generated {len(bars)} intraday bars")
```

## Troubleshooting

### No Data Found

```
❌ Error: No data found for 005930 on 2026-03-30
```

**Solutions:**
1. Check if date is trading day (not weekend)
2. Verify symbol exists: `ls data/backtest_cache/kr_005930_daily.json`
3. Try generating from daily: `python scalp_sim.py --symbol 005930 --date 2026-03-28`

### Missing Rich Library (for GUI simulation)

```bash
pip install rich
```

### Bars Count Mismatch

If generated bars don't match expected 39:
- Market holidays reduce bar count
- Early market close = fewer bars
- Check actual trading hours for that day

## Integration with Main Simulator

To combine scalping with the regular simulation dashboard:

```python
from simulate import run_simulation_loop
from src.scalping_data_loader import get_day_price_data

# Load scalping data
bars, source = get_day_price_data("005930", "2026-03-30")

# Use in dashboard
from src.simulation_dashboard import SimulationDashboard
dashboard = SimulationDashboard(bars, "005930", speed=1.0, data_source=source)
dashboard.start()
```

## Performance Tips

1. **Fast Testing**: Use `--speed 2.0` to replay 2x faster
2. **Slow Analysis**: Use `--speed 0.5` for detailed observation
3. **Batch Testing**: Run multiple dates/symbols programmatically
4. **Memory**: Set random seed for reproducible synthetic generation

```python
import random
random.seed(42)  # For reproducible synthetic bars
```

## Next Steps

- ✅ Load and replay day price data
- ✅ Test scalping strategy on historical data
- ✅ Analyze performance metrics
- 🔄 Optimize strategy parameters
- 🔄 Compare multiple dates/symbols
- 🔄 Deploy validated strategy to live trading

