# Simulation Dashboard - Strategy Backtesting & Visualization

The **Simulation Dashboard** replays stored historical market data through your trading strategy and visualizes the results in real-time. Perfect for backtesting, strategy validation, and performance analysis.

## Features

### 📊 Real-time Replay
- **Historical Data Playback**: Load and replay any historical data from the backtest cache
- **Strategy Simulation**: Apply your trading strategy to each bar of historical data
- **Live Metrics**: Watch P&L, equity, win rate, and drawdown update in real-time
- **Configurable Speed**: Replay at 0.5x (slow), 1x (normal), or 2x (fast) speed

### 📈 Performance Tracking
- **Position Tracking**: See entries, exits, prices, and unrealized P&L
- **P&L Summary**: Realize and unrealized profits displayed live
- **Trade Journal**: Review all trades with timestamps and reasons
- **Performance Metrics**: Win rate, max drawdown, total return percentage

### 🎯 Strategy Validation
- **Decision Visibility**: See why each trade was entered/exited
- **Trade Reasons**: Logged reason for every buy/sell signal
- **Equity Curve**: Watch your account equity grow or decline
- **Risk Metrics**: Monitor maximum drawdown during simulation

## Quick Start

### Basic Simulation

```bash
# Run simulation for Samsung (005930) at normal speed
python simulate.py

# Run with custom symbol
python simulate.py --symbol 000660

# Run at different speeds
python simulate.py --speed 0.5    # Half speed (watch carefully)
python simulate.py --speed 2.0    # Double speed (quick overview)
```

### Advanced Usage

**Custom data file:**
```bash
python simulate.py --symbol 005930 --data path/to/data.json
```

**Full example:**
```bash
# Simulate LG Electronics at double speed
python simulate.py --symbol 066570 --speed 2.0
```

## Display Layout

### Simulation Header
- Current symbol and progress
- Current bar number and total bars
- Progress percentage
- Status (RUNNING/PAUSED)

### Position Panel
- Symbol, quantity, entry price
- Current price and unrealized P&L
- Shows "No active position" when flat

### P&L Summary
- Available cash
- Total equity
- Realized P&L (from closed trades)
- Unrealized P&L (from open positions)
- Total P&L and return percentage

### Performance Metrics
- Total trades executed
- Win/loss count 
- Win rate percentage
- Maximum drawdown
- Current bar timestamp

### Recent Trades Table
- Trade type (BUY/SELL)
- Execution price
- Quantity
- Reason for trade

## Example Output

```
╔════════════════════════════════════════════════════════════════════════╗
║ Simulating 005930 | Bar 450/500 | Progress: 90.0% | [RUNNING]        ║
╚════════════════════════════════════════════════════════════════════════╝

┌─ Position ────────────────┐  ┌─ P&L Summary ──────────────────────┐
│ Symbol: 005930            │  │ Cash: ₩9,500,000                   │
│ Quantity: 100 shares      │  │ Equity: ₩10,250,000                │
│ Entry Price: ₩70,000      │  │ Realized P&L: ₩-50,000             │
│ Current Price: ₩72,500    │  │ Unrealized P&L: ₩250,000           │
│ Unrealized: +₩250,000     │  │ Total P&L: ₩200,000                │
│ (+3.57%)                  │  │ Return: +2.00%                     │
└───────────────────────────┘  └────────────────────────────────────┘

┌─ Metrics ─────────────────┐  ┌─ Recent Trades ────────────────────┐
│ Trades: 12                │  │ BUY  ₩70,000  100  Entry signal    │
│ Wins: 8 | Losses: 4       │  │ SELL ₩71,500  100  Take profit    │
│ Win Rate: 66.7%           │  │ BUY  ₩71,000  100  Entry signal    │
│ Max Drawdown: 5.23%       │  │ SELL ₩72,500  100  Take profit    │
│ 2026-04-04 14:30:00       │  └────────────────────────────────────┘
└───────────────────────────┘
```

## How It Works

### 1. Data Loading
```
Historical Data → Load bars from backtest_cache/ → Create list of OHLCV bars
```

### 2. Simulation Loop
```
For each bar:
  1. Update current price
  2. Apply strategy logic
  3. Execute BUY/SELL if signal
  4. Update position and P&L
  5. Display dashboard
  6. Wait based on replay speed
```

### 3. Strategy Application
The simulation uses a simple momentum-based strategy:
- **Entry**: BUY when signal detected (customizable)
- **Exit**: SELL when:
  - Take profit reached (+2.0%)
  - Stop loss hit (-1.0%)
- You can modify the strategy in `_apply_strategy_decision()` method

### 4. P&L Calculation
```
Equity = Cash + (Position Qty × Current Price) + Unrealized P&L
Realized P&L = Sum of closed trades P&L
Unrealized P&L = (Current Price - Entry Price) × Position Qty
Return % = Total P&L / Initial Cash × 100
```

## Configuration

### Replay Speed Options

| Speed | Duration | Use Case |
|-------|----------|----------|
| 0.25x | 4x longer | Deep analysis, careful review |
| 0.5x | 2x longer | Detailed inspection |
| 1.0x | Normal | Real-time feel |
| 2.0x | 2x faster | Quick overview |
| 4.0x | 4x faster | Batch analysis |

### Historical Data Sources

The simulation dashboard looks for data in this order:

1. **Custom file** (via `--data` flag)
2. **Backtest cache** (`data/backtest_cache/kr_SYMBOL_daily.json`)
3. **Error** if no data found

### Available Symbols

Check what historical data is cached:

```bash
ls data/backtest_cache/kr_*_daily.json | wc -l
# Shows number of symbols with cached data
```

Common symbols:
- 005930 - Samsung Electronics
- 000660 - LG Electronics  
- 035720 - Kakao
- 066570 - LG Electronics (alternative)

## Strategy Customization

To use your own strategy, modify the `_apply_strategy_decision()` method in `src/simulation_dashboard.py`:

```python
def _apply_strategy_decision(self, bar: dict) -> tuple[str, str]:
    """Apply YOUR strategy logic here"""
    close = float(bar.get("close", 0.0))
    volume = float(bar.get("volume", 0.0))
    
    # Your strategy logic
    if your_buy_condition:
        return "BUY", f"Buy reason: {reason}"
    elif your_sell_condition:
        return "SELL", f"Sell reason: {reason}"
    
    return "HOLD", "Waiting for signal"
```

## Performance Interpretation

### Key Metrics

| Metric | Good | Warning | Bad |
|--------|------|---------|-----|
| Win Rate | >50% | 40-50% | <40% |
| Max Drawdown | <5% | 5-15% | >15% |
| Return % | >5% | 0-5% | <0% |
| Profit Factor | >1.5 | 1.0-1.5 | <1.0 |

### Understanding Results

**High Return, High Drawdown**: Risky strategy
```
Return: +15% but Max Drawdown: 20% → Too risky for most traders
```

**Low Return, Low Drawdown**: Conservative strategy
```
Return: +2% and Max Drawdown: 1% → Safe but slow growth
```

**Balanced**: Moderate returns with controlled risk
```
Return: +8% with Max Drawdown: 5% → Good risk/reward
```

## Troubleshooting

### No Data Found for Symbol

**Problem**: `Error: No data available for SYMBOL`

**Solutions**:
1. Check symbol spelling: use 6-digit code like `005930`
2. Pre-fill cache: `python src/fill_krx_cache.py`
3. Provide custom data: `python simulate.py --data custom_data.json`

### Simulation is Too Fast

**Problem**: Bars flash by too quickly

**Solution**: Use slower replay speed
```bash
python simulate.py --speed 0.5
```

### Simulation is Too Slow

**Problem**: Waiting too long for results

**Solution**: Use faster replay speed
```bash
python simulate.py --speed 2.0
```

### Terminal Display Issues

**Problem**: Colors look wrong or text garbled

**Solution**: Set terminal type
```bash
export TERM=xterm-256color
python simulate.py
```

## Comparing Multiple Simulations

Run simulations with different parameters:

```bash
# Strategy v1 - Conservative
python simulate.py --symbol 005930 --speed 2.0

# Strategy v2 - Aggressive
python simulate.py --symbol 005930 --speed 2.0
```

Compare the results from the printed summaries.

## Integration with Live Bot

After validating your strategy in simulation:

1. ✅ Confirm strategy works in simulation
2. ✅ Validate win rate and drawdown
3. ✅ Verify risk management
4. ➡️ Update strategy in your code
5. ➡️ Test in dry run mode
6. ➡️ Deploy to live trading

## API Reference

### SimulationDashboard Class

```python
from simulation_dashboard import create_simulation_dashboard

# Create dashboard
dashboard = create_simulation_dashboard(
    symbol="005930",
    speed=1.0,
    data_file=None
)

# Start simulation
dashboard.start()

# Access results
print(f"Final P&L: {dashboard.state.realized_pnl}")
print(f"Win Rate: {dashboard.state.win_count}")
print(f"Max Drawdown: {dashboard.state.max_drawdown}")
```

### Key Attributes

```python
dashboard.state.equity           # Current account equity
dashboard.state.cash             # Available cash
dashboard.state.position_qty     # Current position size
dashboard.state.trades           # All trades executed
dashboard.state.realized_pnl     # Closed trade P&L
dashboard.state.unrealized_pnl   # Open position P&L
dashboard.state.win_count        # Number of winning trades
dashboard.state.loss_count       # Number of losing trades
dashboard.state.max_drawdown     # Largest equity drawdown
```

## Advanced Usage

### Batch Simulations

```bash
#!/bin/bash
# Test strategy on multiple symbols

for symbol in 005930 000660 035720 066570; do
    echo "Testing $symbol..."
    python simulate.py --symbol $symbol --speed 2.0 >> results.log
done
```

### Custom Data Format

The simulation accepts JSON with bars/rows array:

```json
{
  "symbol": "005930",
  "bars": [
    {
      "timestamp": "2026-04-01",
      "open": 70000,
      "high": 72000,
      "low": 69500,
      "close": 71500,
      "volume": 1000000
    }
  ]
}
```

## Examples

### Example 1: Quick Validation

```bash
# Test strategy on Samsung, 2x speed
python simulate.py --symbol 005930 --speed 2.0
```

Result: See if strategy makes profit or loses

### Example 2: Detailed Analysis

```bash
# Watch strategy step by step at half speed
python simulate.py --symbol 005930 --speed 0.5
```

Result: Understand each decision and trade reason

### Example 3: Multiple Symbol Test

```bash
python simulate.py --symbol 005930
python simulate.py --symbol 000660
python simulate.py --symbol 035720
```

Compare performance across different stocks

## Keyboard Controls

| Key | Action |
|-----|--------|
| Ctrl+C | Stop simulation |
| (Future) Space | Pause/Resume |
| (Future) → | Next bar |
| (Future) ← | Previous bar |

## Output Files

When simulation completes, check:
- **Terminal**: Live dashboard during replay
- **Logs**: `data/bot_runtime.log` (if integrated)
- **Console**: Printed summary at end

## Performance Impact

| Aspect | Impact |
|--------|--------|
| CPU | <5% during replay |
| Memory | ~10-50 MB |
| Time | Depends on speed setting |
| Disk I/O | Minimal (data cached) |

## Next Steps

1. ✅ Run simulation on historical data
2. ✅ Validate strategy performance
3. ✅ Tweak parameters if needed
4. ➡️ Deploy to dry run mode
5. ➡️ Validate in live trading

---

**Version**: 1.0  
**Last Updated**: 2026-04-04  
**Status**: Production Ready
