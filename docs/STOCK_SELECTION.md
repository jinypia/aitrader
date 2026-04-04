# Stock Selection & Interactive Simulation

The simulation dashboard now supports dynamic stock switching during your trading session. You have **two modes**:

## Mode 1: Interactive Menu (Default)

Run without arguments to enter interactive mode:

```bash
python simulate.py
```

This displays a menu where you can:

```
======================================================================
AITRADER - Strategy Simulation Dashboard
======================================================================

📊 Current Settings:
   Symbol: 005930
   Speed: 1.0x
   Custom Data: No

Options:
  1. Run simulation with current symbol
  2. Change symbol  
  3. Change speed
  4. Load custom data file
  5. View available symbols
  6. Exit
```

### Interactive Commands Explained:

**Option 1: Run Simulation**
- Starts the simulation with current configuration
- Press `Ctrl+C` to stop and return to menu
- Switch symbols without restarting

**Option 2: Change Symbol**
- Lists all available symbols (with cached data)
- Enter any symbol code (e.g., `005930`, `000660`, `035720`)
- Validates symbol exists in backtest cache
- Updates current setting

**Option 3: Change Replay Speed**
- Adjust simulation speed: `0.25` to `4.0`
- `0.5` = Half speed (slower replay)
- `1.0` = Normal speed (one bar per second)
- `2.0` = Double speed (faster replay)

**Option 4: Load Custom Data**
- Point to a custom JSON file with historical data
- Format: `{"timestamp": "2024-01-01", "open": 100, ...}`
- Useful for testing on external datasets

**Option 5: View Available Symbols**
- Shows all symbols with cached data
- Displays count, shows first 20, indicates if more exist
- Useful for discovering available symbols

---

## Mode 2: Direct Simulation

Run with specific parameters to skip interactive menu:

```bash
# Run simulation on specific symbol
python simulate.py --symbol 005930 --speed 1.0

# Run at double speed
python simulate.py --symbol 000660 --speed 2.0

# Use custom data file
python simulate.py --symbol 005930 --data path/to/data.json
```

### Command-line Options:

| Option | Default | Description |
|--------|---------|-------------|
| `--symbol` | (none) | Stock symbol (skips interactive mode if provided) |
| `--speed` | `1.0` | Replay speed (0.25-4.0) |
| `--data` | (auto) | Path to custom historical data JSON |
| `--interactive` | `true` | Force interactive mode (default) |

---

## Available Symbols

The system automatically detects symbols from cached data in `data/backtest_cache/`.

**Common Korean Stocks:**
- `005930` - Samsung Electronics
- `000660` - LG Electronics  
- `035720` - Kakao
- `068270` - Naver
- `051910` - LG Chem

**To check all available:**
1. Run `python simulate.py`
2. Select option 5 (View available symbols)
3. Or check: `ls data/backtest_cache/kr_*_daily.json`

---

## Example Workflow

```bash
# Start interactive mode
$ python simulate.py

# Menu appears, select option 2 to change symbol
Select option (1-6): 2

# See available options
📊 Available Symbols (247 total):
────────────────────────────────────────────────────────────
   1. 000020
   2. 000040
   3. 000050
   ...
   20. 032640
   ... and 227 more
────────────────────────────────────────────────────────────

Enter symbol (e.g., 005930) or press Enter to cancel: 000660

✓ Symbol changed to 000660

# Back to main menu, select option 1 to run
Select option (1-6): 1

🚀 Starting simulation for 000660 at 1.0x speed...
📊 Simulating LG Electronics (000660)
⚡ Speed: 1.0x
📁 Data: Auto-loading from backtest cache...
   → Found: data/backtest_cache/kr_000660_daily.json

# Simulation dashboard displays...
[Dashboard renders with real-time data]

# Press Ctrl+C to stop
^C⏹️  Stopping simulation...

# Returns to menu - change symbol again or exit
```

---

## Adding New Cached Data

To add more symbols to test on:

```bash
python src/fill_krx_cache.py
```

This fills `data/backtest_cache/` with historical data for available KRX symbols, making them available for selection in the simulator.

---

## Tips & Tricks

- **Batch Testing**: Run multiple symbols sequentially using interactive mode
- **Speed Comparison**: Run same symbol at 0.5x then 2.0x to see different strategies
- **Custom Data**: Load alternative datasets with `--data` or option 4
- **Symbol Not Found?**: Ensure data exists: `ls data/backtest_cache/kr_SYMBOL_daily.json`
- **Quick Test**: `python simulate.py --symbol 005930` runs directly without menu

---

## Data Source Display

During simulation, the dashboard automatically shows:

```
📊 Data Source:
   File: data/backtest_cache/kr_005930_daily.json
   Date Range: 2020-01-01 → 2024-12-31  
   Bars: 1,247
   Speed: 1.0x
```

This helps you confirm exactly which data is being replayed.
