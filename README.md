# Kiwoom Auto-Trading Starter

This repository is a minimal starter for building your own Kiwoom REST auto-trading bot.

## Documentation Structure

- Docs hub (single index): `docs/README.md`
- Primary runbook: `SCALPING_AUTOMATION_GUIDE.md`
- Historical implementation/status reports are stored in `docs/history/` (root files are lightweight redirects)

## What is included

- OAuth login (`/api/oauth2/token`)
- Polling loop
- Simple strategy hook (BUY/SELL/HOLD by percent move)
- Risk guardrails:
  - `DRY_RUN=true` by default
  - max daily order count
- Pluggable endpoint config so you can map exact Kiwoom TR/order APIs
- **NEW:** Professional scalping scheduler with Slack notifications
- **NEW:** Automated weekday trading with top 5 stock selection

## 1) Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Set required values in `.env`:

- `KIWOOM_APP_KEY`
- `KIWOOM_SECRET_KEY`
- `PRICE_PATH=/api/dostk/mrkcond`
- `PRICE_API_ID=ka10007`
- `PRICE_FIELD=cur_prc`
- `ORDER_PATH=/api/dostk/ordr`
- `ORDER_BUY_API_ID=kt10000`
- `ORDER_SELL_API_ID=kt10001`
- `DMST_STEX_TP=KRX`
- `TRDE_TP=3`

## 2) Slack Integration Setup

Your Slack configuration is already in `.env`:

```bash
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/WEBHOOK/URL
SLACK_CHANNEL=#trading-reports
```

### Verify Slack Setup

```bash
./verify_slack.sh
```

This will test your webhook and send a confirmation message to Slack.

## 3) Scalping Scheduler (NEW)

### Quick Start

```bash
# Setup Slack notifications (recommended)
./setup_slack.sh

# Start automated weekday trading
./scalp_scheduler_start.sh start
```

### Manual Commands

```bash
# Check system status
./scalp_scheduler_start.sh check

# Run full day simulation (test mode)
./scalp_scheduler_start.sh test

# Pre-market validation only
./scalp_scheduler_start.sh validate

# Stock selection only
./scalp_scheduler_start.sh select

# Single trading session
./scalp_scheduler_start.sh session
```

### Slack Integration

Get real-time notifications for:
- 🔄 **Stock selection updates** (every 2 hours)
- ✅ **Pre-market validation results**
- 📊 **Hourly trading session P&L reports**
- 📈 **Daily performance summaries**
- 🚨 **Emergency alerts for loss limits**

See `docs/SLACK_INTEGRATION.md` for setup details.

## 3) Run Legacy Bot

```bash
python src/main.py
```

## 4) Web Control

```bash
python src/web_control.py
```

Then open:

`http://127.0.0.1:8080`

Available endpoints:

- `GET /` dashboard
- `POST /start` start bot
- `POST /stop` stop bot
- `GET /status` JSON status
- `GET /consistency-check` compare `.env` vs `data/runtime_config.json` and validate strategy/profile setup

Strategy/setting sync notes:

- Web Control saves update both `data/runtime_config.json` and `.env`.
- Runtime applies `data/runtime_config.json` first, then `.env` as fallback.
- Strategy mode:
  - `AUTO`: market regime + factor scores choose strategy dynamically
  - `MANUAL`: fixed profile (`TREND`, `BALANCED`, `DEFENSIVE`, `VOLATILITY`, `SCALPING`)
- Presets:
  - `AGGRESSIVE`, `BALANCED`, `DEFENSIVE`
  - `PROFIT_MAX` (one-click stronger profit-seeking preset with `AUTO` strategy mode)

Market vibe header config (`.env`):

- `MARKET_VIBE_REFRESH_SEC=600` (2 minutes)
- `MARKET_VIBE_API_ID=ka20003`
- `MARKET_INDEX_CODE=001` (`001` KOSPI, `101` KOSDAQ)

Dashboard analysis now includes:

- Sentiment score (0-100) and regime (BULLISH/NEUTRAL/BEARISH)
- Breadth and momentum deltas vs previous snapshot
- Sector return mean/median/dispersion
- Sector breadth (up/down sector counts)
- Auto-generated market-internal interpretation notes

Auto-trading account/performance tracking:

- Position/cash/equity tracking
- Realized and unrealized PnL
- Daily/weekly/monthly performance (`1D/7D/30D`)
- Persistent ledger file (`LEDGER_PATH`) so performance survives restarts
- In `DRY_RUN=true`, simulated fills can be enabled via `SIMULATE_ON_DRY_RUN=true`

## Important

- Keep `DRY_RUN=true` until market data mapping and order payload are validated.
- Kiwoom REST endpoints can share URI and are distinguished by `api-id` header.
- Token is typically short-lived (often 24h), so production bots should refresh automatically.

## Git Secret Guard (Team)

This repository includes a commit-time secret scanner at `.githooks/pre-commit`.

Enable it once per clone:

```bash
git config core.hooksPath .githooks
chmod +x .githooks/pre-commit
```

What it blocks:

- Slack webhook tokens
- AWS-style access keys
- GitHub/Slack style tokens
- Common live API keys and leaked `Authorization: Bearer ...` headers
- PEM private key markers

## 4) Real-time CLI Dashboard (NEW)

Monitor trading transactions and performance in real-time directly from the terminal.

### Quick Start

```bash
# Run with CLI dashboard enabled
python src/main.py --dashboard

# Or use the quick start script
python quickstart_dashboard.py
```

### Features

- **Real-time Monitoring**: Displays current positions, P&L, equity, and cash balance
- **Order Journal**: Shows the last 10 trades with timestamps, prices, and status
- **Event Log**: Color-coded trading events updated in real-time
- **Portfolio Metrics**: Risk metrics including portfolio heat percentage
- **Customizable Updates**: Adjust refresh interval for your needs

### Options

```bash
# Default: updates every 1 second
python src/main.py --dashboard

# Faster updates (2 updates per second)
python src/main.py --dashboard --update-interval 0.5

# Slower updates (2 seconds between updates)
python src/main.py --dashboard --update-interval 2.0
```

### Dashboard Display

The dashboard shows:

| Panel | Information |
|-------|-------------|
| **Status** | Bot state, trade mode, session phase, market regime, portfolio heat |
| **Position** | Symbol, quantity, average price, current price, unrealized P&L |
| **P&L Summary** | Equity, cash, unrealized/realized P&L, total return percentage |
| **Recent Orders** | Last 10 trades with type, quantity, price, amount, and status |
| **Recent Events** | Last 15 trading events color-coded by type |

### Requirements

The CLI dashboard requires the `rich` library:

```bash
pip install rich
# Or install all requirements
pip install -r requirements.txt
```

### Runtime Lock Troubleshooting

If startup fails with "Another bot runtime is already active", use the lock helper:

```bash
# Show lock metadata and active holder PID/command
python scripts/runtime_lock_ctl.py status

# Stop the active holder safely (SIGTERM, then SIGKILL if needed)
python scripts/runtime_lock_ctl.py stop
```

By default, `stop` only targets processes that look like the bot runtime (`main.py`).

### Color Coding

- **Green**: Gains, active positions, successful trades
- **Red**: Losses, risk conditions, errors
- **Yellow**: Warnings, extreme portfolio heat
- **Magenta**: Trade orders and transactions
- **Cyan**: General information and headers

For more details, see [docs/CLI_DASHBOARD.md](docs/CLI_DASHBOARD.md).

## 5) Strategy Simulation Dashboard (NEW)

Run your trading strategy on historical data and watch it execute in real-time.

## 6) AI Company Mode (Manager + Multi-Agent)

You can run the bot as an "AI company" where a manager agent coordinates specialist agents.

Hierarchy (recommended):

- Manager Agent: orchestrates all agents and publishes hourly reports
- Market Analysis Agent: evaluates regime, confidence, and session conditions
- Investment Strategy Agent: converts analysis + selection quality into action hints
- Risk Guard Agent: checks heat, stale data, and halt conditions
- Performance Feedback Agent: learns from realized outcomes and updates sleeve biases over time
- Capital Allocation Agent: dynamically splits budget across invest-agent pods
- Trend Invest Agent: manages trend-following allocation slice
- Scalping Invest Agent: manages short-horizon allocation slice
- Defensive Invest Agent: manages cash/hedge allocation slice
- Order Policy Agent: converts consensus/risk into ALLOW/BLOCK and order-limit factor
- Execution Agent: monitors runtime health and detects startup/runtime failures
- Reporting Agent: writes structured manager reports to JSON and log stream
- Manager Slack Notifier: sends each hourly manager report to Slack

Run it:

```bash
# Manager + agents + dashboard, hourly reports by default
python main.py --ai-company --dashboard

# Custom cadence (example: report every 30 minutes)
python main.py --ai-company --manager-report-minutes 30 --manager-cycle-seconds 15

# Push hourly manager reports to Slack
python main.py --ai-company --manager-slack

# Use a dedicated manager-report webhook
python main.py --ai-company --manager-slack --manager-slack-webhook https://hooks.slack.com/services/...
```

Hourly reports are saved to `data/hourly_manager_reports.json` (change with `--manager-report-path`).

Manager communication mode is hybrid:

- Regular: full agent coordination every manager cycle (`--manager-cycle-seconds`)
- Event-driven: immediate manager report when key signals change (regime/risk/policy/allocation/symbol)

Manager command model:

- On every cycle, the manager issues purpose-specific work orders to each agent.
- Agents acknowledge manager directives in their payload (`manager_order`) and execute with role-specific logic.
- When important signals change, manager raises urgency and updates directives before the next decision/report.
- Learning state is persisted in `data/manager_learning_state.json` and used to tune allocation bias.
- Learning updates are driven by realized-trade outcomes (`state.realized_pnl` delta) with fill activity attribution.
- Sleeve-specific realized attribution is tracked from `data/ledger.json` SELL trades and persisted as:
  - `last_processed_trade_index`
  - `sleeve_realized_totals`
  - cycle-level `sleeve_realized_delta`
- Runtime now writes explicit `ai_sleeve` tags on fills (`trend`/`scalping`/`defensive`) to improve attribution accuracy.

Tune event cadence:

```bash
python main.py --ai-company --manager-event-cooldown-seconds 60
```

### Quick Start

```bash
# Interactive mode - choose stocks from menu
python simulate.py

# Or run directly on specific symbol
python simulate.py --symbol 005930 --speed 1.0
```

### Stock Selection

The simulator now supports **interactive stock switching**. Run `python simulate.py` with no arguments to enter interactive mode where you can:

- 📊 **Select from available symbols** - See all cached symbols with one command
- ⚡ **Change speed dynamically** - Adjust replay speed (0.25x to 4.0x) between simulations
- 🔄 **Switch stocks mid-session** - Run multiple simulations without restarting
- 📁 **Load custom data** - Point to external data files for testing

This is perfect for comparing multiple stocks or rapid strategy testing.

**Detailed guide**: [docs/STOCK_SELECTION.md](docs/STOCK_SELECTION.md)

### Features

- **Historical Replay**: Play back stored market data through your strategy
- **Live Metrics**: Watch P&L, equity, win rate, and drawdown update in real-time
- **Trade Journal**: See every buy/sell with entry/exit reasons
- **Performance Analysis**: Validate strategy before live trading
- **Configurable Speed**: Normal (1x), fast (2x), or slow (0.5x) replay

### Strategy Validation Workflow

1. Run simulation: `python simulate.py`
2. Review performance metrics (win rate, max drawdown, return %)
3. Adjust strategy if needed
4. Compare multiple simulations
5. Deploy validated strategy to live bot

### Configuration Options

```bash
# Interactive mode (recommended for choosing between stocks)
python simulate.py

# Direct mode - specific symbol and settings
python simulate.py --symbol 005930 --speed 1.0

# Different symbols
python simulate.py --symbol 005930      # Samsung
python simulate.py --symbol 000660      # LG Electronics  
python simulate.py --symbol 035720      # Kakao

# Different speeds
python simulate.py --speed 0.5           # Half speed (detailed analysis)
python simulate.py --speed 2.0           # Double speed (quick overview)

# Custom data
python simulate.py --data path/to/data.json
```

### Example Output

```
╔═══ SIMULATION SUMMARY ═══╗
│ Symbol: 005930           │
│ Bars Processed: 500/500  │
│ Total Trades: 12         │
│ Wins/Losses: 8/4         │
│ Win Rate: 66.7%          │
│ Final Equity: ₩10.25M    │
│ Final P&L: +₩250K (+2.5%)│
│ Max Drawdown: 5.23%      │
└──────────────────────────┘
```

For detailed instructions, see [docs/SIMULATION_DASHBOARD.md](docs/SIMULATION_DASHBOARD.md).

## 6) Scalping Simulation - Day Price Data (NEW)

Test your scalping strategy on intraday 2-minute bar data with day price history.

### Quick Start

```bash
# Run scalping simulation (loads yesterday's data)
python scalp_sim.py

# Simulate on specific date
python scalp_sim.py --date 2026-03-30

# Show detailed trades
python scalp_sim.py --date 2026-03-30 --verbose

# Check available data
python scalp_sim.py --available
```

### What This Does

The scalping simulator:

- **Loads Day Price Data**: Fetches or generates 2-minute intraday bars
- **Tests Scalping Strategy**: Applies optimized RSI, volume, and trend rules
- **Measures Performance**: Calculates win rate, P&L, drawdown, and trade statistics
- **Multiple Data Sources**:
  - Stored intraday prices (`data/selected_intraday_prices.json`)
  - Generated from daily data (1000+ cached symbols)
  - Custom JSON files

### Data Sources

The system automatically loads intraday data from:

1. **Stored Intraday** (up to 195 bars/day)
   - Pre-collected 2-minute data
   - Most accurate for replays

2. **Generated from Daily**
   - Synthetic realistic bars
   - Available for any symbol in `data/backtest_cache/`

### Example Output

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
PERFORMANCE METRICS
📊 P&L:          ₩250,000 (+2.50%)
⬇️  Max Drawdown:  1.23%

TRADE STATISTICS
📍 Total Trades:  3
✅ Wins:          2
❌ Losses:        1
🎯 Win Rate:      66.7%
```

### Scalping Configuration

Edit `ScalpParams` in `src/scalping_strategy.py`:

```python
ScalpParams(
    rsi_entry_min=30,              # Buy when RSI in this range
    rsi_entry_max=70,
    volume_spike_threshold=2.0,    # Require 2x volume spike
    profit_target_pct=0.8,         # Exit at +0.8% gain
    stop_loss_pct=0.5,             # Exit at -0.5% loss
    max_hold_bars=6                # Hold max 12 minutes (6 × 2-min)
)
```

### Typical Workflow

```bash
1. python scalp_sim.py --available        # See available data
2. python scalp_sim.py --date 2026-03-30  # Run simulation
3. Review results in data/scalp_sim_*.json
4. Adjust parameters in src/scalping_strategy.py
5. Run again to compare performance
6. Batch test multiple dates/symbols
7. Deploy best configuration
```

For complete guide, see [docs/SCALPING_DATA_GUIDE.md](docs/SCALPING_DATA_GUIDE.md) and [docs/SCALPING_QUICKSTART.md](docs/SCALPING_QUICKSTART.md).
