# CLI Dashboard - Real-time Transaction Monitoring

The AITRADER bot now includes a real-time CLI dashboard for monitoring trading transactions, positions, and performance metrics directly from the terminal.

## Features

### 📊 Real-time Monitoring
- **Trading Status**: Current bot state, operating mode, session phase, market regime
- **Position Management**: Active positions, entry prices, current price, unrealized P&L
- **Portfolio Performance**: Equity, cash balance, realized/unrealized P&L, total return percentage
- **Order Journal**: Recent buy/sell transactions with timestamps, quantities, prices, and status
- **Event Log**: Real-time trading events color-coded by type
- **Risk Metrics**: Portfolio heat percentage relative to maximum threshold

### 🎨 Visual Features
- Color-coded status indicators (green for gains, red for losses, yellow for alerts)
- Real-time updates with configurable refresh intervals
- Clean, organized layout with separate panels for different information types
- Responsive terminal display that adapts to screen size

### 📈 Information Displayed
- **Status Panel**: Operating mode (DRY/LIVE), armed state, session phase, market regime, portfolio heat
- **Position Panel**: Symbol, quantity, average entry price, current price, unrealized P&L
- **P&L Panel**: Total equity, cash balance, unrealized and realized P&L, total return percentage
- **Orders Panel**: Last 10 trades showing time, type, symbol, quantity, price, amount, status
- **Events Panel**: Last 15 events with color-coded event types

## Usage

### Basic Usage

Run the bot with the dashboard enabled:

```bash
cd /Users/superarchi/aitrader
python src/main.py --dashboard
```

### Advanced Options

**Custom Update Interval** (default is 1 second):

```bash
# Update every 2 seconds
python src/main.py --dashboard --update-interval 2.0

# Update every 0.5 seconds (twice per second)
python src/main.py --dashboard --update-interval 0.5
```

### Without Dashboard

Run the bot normally without the dashboard:

```bash
python src/main.py
```

Logs will still be written to `data/bot_runtime.log` for offline analysis.

## Color Coding

The dashboard uses color coding for quick visual feedback:

### Status Colors
- **Green**: Positive values, good performance, active positions
- **Red**: Negative values, losses, risk conditions
- **Yellow**: Warnings, extreme portfolio heat
- **Magenta**: Trade orders and transactions
- **Cyan**: General information and headers

### Event Types
- **Green**: BUY signals, ENTRY signals
- **Yellow**: SELL signals, EXIT signals
- **Red**: RISK_EXIT, HALT conditions, errors
- **Magenta**: ORDER RESULT status updates
- **White**: Other events

## Display Panels

### Trading Status Panel
Shows the current operating state:
- **Status**: RUNNING/STOPPED
- **Mode**: DRY (paper trading) or LIVE (real money)
- **Armed**: Whether live trading is enabled
- **Session**: Current market session phase
- **Regime**: Current market regime (BULLISH, BEARISH, NEUTRAL)
- **Portfolio Heat**: Current exposure as percentage of maximum allowed

### Position Panel
Shows current open positions:
- **Symbol**: The stock symbol being traded
- **Quantity**: Number of shares held
- **Average Price**: Entry price for the position
- **Current Price**: Current market price
- **Position P&L**: Unrealized profit/loss in won and percentage

### P&L Summary Panel
Shows financial performance:
- **Equity**: Total account value (cash + position value)
- **Cash**: Available buying power
- **Unrealized P&L**: Profit/loss on open positions
- **Realized P&L**: Profit/loss from closed trades
- **Total P&L**: Combined realized and unrealized profits
- **Return**: Percentage return on invested capital

### Recent Orders Panel
Shows the last 10 trades executed:
- **Time**: Transaction timestamp
- **Type**: BUY or SELL order
- **Symbol**: Stock symbol
- **Qty**: Number of shares
- **Price**: Execution price per share
- **Amount**: Total transaction amount
- **Status**: FILLED, PENDING, or REJECTED

### Recent Events Panel
Shows the last 15 trading events:
- Order executions
- Risk exits and halts
- Signal generations
- Position entries and exits
- System alerts and errors

## Requirements

### Installation

The dashboard requires the `rich` library for terminal rendering:

```bash
pip install rich
```

This is included in the updated `requirements.txt`:

```bash
pip install -r requirements.txt
```

## Examples

### Example 1: Monitor with default 1-second updates

```bash
python src/main.py --dashboard
```

Output will show:
```
┏━━━━━━━━━━━━━━━━ AITRADER - Real-time Trading Dashboard ━━━━━━━━━━━━━━━━┓
┃                      2026-04-04 14:30:45                              ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛

┌─ Trading Status ───────────────┐  ┌─ P&L Summary ──────────────────┐
│ Status: RUNNING                │  │ Equity: ₩10,250,000            │
│ Mode: DRY / Armed: False       │  │ Cash: ₩10,000,000              │
│ Session: REGULAR / Regime: ... │  │ Unrealized P&L: ₩250,000       │
│ Portfolio Heat: 15.0% / 35.0%  │  │ Realized P&L: ₩0               │
└────────────────────────────────┘  │ Total P&L: ₩250,000            │
                                     │ Return: +2.50%                 │
┌─ Position ─────────────────────┐  └────────────────────────────────┘
│ Symbol: 005930 (Samsung)       │
│ Quantity: 100                  │  ┌─ Recent Events ────────────────┐
│ Average Price: ₩70,000         │  │ 14:30:42 ORDER RESULT: FILLED  │
│ Current Price: ₩72,500         │  │ 14:30:40 BUY ENTRY: 005930     │
│ Position P&L: +₩250,000 (+3.6%)│  │ 14:30:35 SIGNAL: ENTRY         │
└────────────────────────────────┘  └────────────────────────────────┘
```

### Example 2: Monitor with faster updates (0.5 seconds)

```bash
python src/main.py --dashboard --update-interval 0.5
```

This provides more responsive updates, useful during high-frequency trading sessions.

### Example 3: Enable scalping mode with dashboard

```bash
export STRATEGY_MODE=SCALPING
python src/main.py --dashboard
```

Monitor scalping trades in real-time with the dashboard showing quick entry/exit signals.

## Troubleshooting

### Dashboard doesn't appear
1. Ensure `rich` is installed: `pip install rich`
2. Check that your terminal supports ANSI colors
3. Verify the bot is actually running by checking logs

### Updates are too slow/fast
Use `--update-interval` flag to adjust:
```bash
# Faster updates (caution: may impact performance)
python src/main.py --dashboard --update-interval 0.2

# Slower updates (reduces terminal I/O)
python src/main.py --dashboard --update-interval 2.0
```

### Text appears garbled
This usually indicates your terminal doesn't support ANSI colors. Try:
```bash
# Force ANSI support
export TERM=xterm-256color
python src/main.py --dashboard
```

### Dashboard freezes
If the dashboard appears to freeze:
1. Press Ctrl+C to exit
2. Check the log file: `data/bot_runtime.log`
3. Verify bot is still running in the background

## Integration with Logs

The dashboard complements the traditional log file output:

- **Dashboard**: Real-time visual monitoring during active trading
- **Log File** (`data/bot_runtime.log`): Complete record for offline analysis, debugging, and compliance

Both run simultaneously when dashboard is enabled.

## Performance Impact

The dashboard is designed to have minimal performance impact:
- Updates run in a separate daemon thread
- Terminal rendering is non-blocking
- CPU usage typically <1% per dashboard update
- Network requests and trading logic are unaffected

## Exit Dashboard

To stop the dashboard and bot:
1. Press `Ctrl+C` in the terminal
2. Dashboard will cleanly shutdown
3. Bot will exit gracefully

## Advanced Use: Custom Dashboard Modifications

To customize the dashboard appearance or add additional metrics, edit `src/cli_dashboard.py`:

```python
# Example: Add custom metric to status panel
def _build_status_panel(self) -> Panel:
    status_text = Text()
    status_text.append("Custom Metric: ", style="bold")
    status_text.append(f"{your_value}", style="cyan")
    # ... rest of panel
```

## Support

For issues or feature requests:
1. Check `data/bot_runtime.log` for error messages
2. Verify all parameters are correctly set via environment variables
3. Ensure `rich` library is up to date: `pip install --upgrade rich`

---

**Dashboard Version**: 1.0  
**Last Updated**: 2026-04-04  
**Compatible with**: AITRADER v3.0+
