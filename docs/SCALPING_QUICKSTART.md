# Quick Start: Scalping Day Price Data

Get intraday 2-minute bar data for scalping strategy simulation in 60 seconds.

##  🚀 Quick Commands

```bash
# Run scalping simulation on yesterday
python scalp_sim.py

# Simulate on specific date with details
python scalp_sim.py --date 2026-03-30 --verbose

# Check available data
python scalp_sim.py --available

# Simulate different stock
python scalp_sim.py --symbol 000660

# Save results automatically to data/ directory
```

## 📊 What You Get

Each simulation provides:
- **Trade count and win rate** - Success rate of strategy
- **P&L metrics** - Profit/loss, return percentage
- **Risk metrics** - Max drawdown, equity tracking
- **Detailed trade logs** - Entry/exit prices and reasons
- **Performance file** - JSON output for analysis

## 🔧 Use Cases

### 1. Test Strategy on Different Days

```bash
# Try multiple dates
for date in 2026-03-28 2026-03-29 2026-03-30; do
    python scalp_sim.py --date $date
done
```

### 2. Compare Stocks

```bash
# Compare Samsung vs LG
python scalp_sim.py --symbol 005930 --verbose
python scalp_sim.py --symbol 000660 --verbose
```

### 3. Programmatic Usage

```python
from src.scalping_data_loader import get_day_price_data, get_day_data_preview
from scalp_sim import run_scalping_simulation

# Get preview
preview = get_day_data_preview("005930", "2026-03-30")
print(f"Available: {preview['available']}")
print(f"Bars: {preview['bar_count']}")

# Run simulation
results = run_scalping_simulation(
    symbol="005930",
    date_str="2026-03-30",
    show_details=True
)

# Access results
print(f"Win Rate: {results['win_rate']:.1f}%")
print(f"Max Drawdown: {results['max_drawdown']:.2f}%")
print(f"P&L: ₩{results['pnl']:,.0f}")
```

## 📁 Data Sources

The system tries these sources in order:

1. **Stored Intraday** (`data/selected_intraday_prices.json`)
   - Pre-collected 2-minute bars
   - Most accurate

2. **Replay Reports** (`data/intraday_selected_replay.json`)
   - Historical strategy execution data
   - Includes performance notes

3. **Generated from Daily** (`data/backtest_cache/kr_005930_daily.json`)
   - Synthetic realistic bars
   - Available for 1000+ symbols

## 🎯 Scalping Parameters

Edit these in `src/scalping_strategy.py`:

```python
ScalpParams(
   rsi_entry_min=30,              # Buy RSI range
    rsi_entry_max=70,
    volume_spike_threshold=2.0,    # Volume requirement
    profit_target_pct=0.8,         # Take profit at +0.8%
    stop_loss_pct=0.5,             # Cut loss at -0.5%
   max_hold_bars=6,               # Max 12-minute hold
)
```

## 📈 Example Output

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

## 🔍 Data Format

Each 2-minute bar:
```json
{
  "timestamp": "2026-03-30 09:00",
  "open": 179000.0,
  "high": 179500.0,
  "low": 178800.0,
  "close": 179200.0,
  "volume": 5000000,
  "symbol": "005930"
}
```

## 📚 Key Modules

**`src/scalping_data_loader.py`**
- `get_day_price_data(symbol, date)` - Load intraday bars
- `get_day_data_preview(symbol, date)` - Check what's available
- `generate_intraday_bars_from_daily(...)` - Create synthetic bars

**`scalp_sim.py`**
- `run_scalping_simulation()` - Execute strategy
- `display_results()` - Format output
- `main()` - CLI entry point

**`src/scalping_strategy.py`**
- `calculate_scalp_metrics()` - RSI, volume, trend
- `scalp_entry_signal()` - Buy conditions
- `scalp_exit_signal()` - Sell conditions

## 🎓 Learning Path

1. **Start here**: `python scalp_sim.py` - Run first simulation
2. **Explore data**: Check `data/selected_intraday_prices.json`
3. **Understand output**: Review results JSON file
4. **Modify strategy**: Edit `ScalpParams` in `src/scalping_strategy.py`
5. **Automate testing**: Write Python scripts for batch runs

## ✅ Typical Workflow

```
1. python scalp_sim.py --available     # See what data exists
2. python scalp_sim.py --date 2026-03-30 --verbose  # Run simulation
3. Review output in data/scalp_sim_*.json file
4. Adjust strategy parameters in src/scalping_strategy.py
5. Run again to see improvement
6. Batch test multiple dates/symbols
7. Deploy best-performing configuration
```

## 🐛 Troubleshooting

**Q: "No data found"**
- A: The date may not be a trading day or data hasn't been collected yet
- Solution: `python scalp_sim.py --available` to see available dates

**Q: "ModuleNotFoundError: no module named 'scalping_data_loader'"**
- A: Python path not set correctly
- Solution: Run from project root: `cd /Users/superarchi/aitrader && python scalp_sim.py`

**Q: "ValueError: No data available"**
- A: Symbol doesn't exist in cache
- Solution: Check `data/backtest_cache/kr_SYMBOL_daily.json` exists

## 💡 Pro Tips

1. **Batch test all symbols:**
   ```bash
   ls data/backtest_cache/kr_*_daily.json | \
   sed 's/.*kr_//;s/_daily.*//' | \
   while read s; do
     echo "Testing $s..."
     python scalp_sim.py --symbol $s
   done
   ```

2. **Analyze results programmatically:**
   ```python
   import json, glob
   for file in glob.glob("data/scalp_sim_*.json"):
       with open(file) as f:
           results = json.load(f)
           if results['pnl_pct'] > 1:
               print(f"WIN: {file} - {results['pnl_pct']:.2f}%")
   ```

3. **Generate synthetic data for any daily bar:**
   ```python
   from src.scalping_data_loader import generate_intraday_bars_from_daily
   
   bars = generate_intraday_bars_from_daily(
       179000, 180000, 178500, 179500, 100000000,
       "2026-03-30", "005930"
   )
      # bars now contains 195 synthetic 2-minute bars
   ```

## 📞 Next Steps

- **Manual tuning**: Edit strategy parameters and retest
- **Parameter sweep**: Optimize RSI, profit target, stop loss
- **Statistical analysis**: Compare win rates across dates/symbols
- **Live deployment**: Transfer best parameters to live bot
- **Risk management**: Test with different position sizing

---

**Need help?** See [SCALPING_DATA_GUIDE.md](SCALPING_DATA_GUIDE.md) for complete documentation.
