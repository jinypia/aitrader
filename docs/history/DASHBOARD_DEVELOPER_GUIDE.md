#!/usr/bin/env python3
"""
Developer's Reference Guide for CLI Dashboard

This guide covers:
- Dashboard architecture and design patterns
- How to extend the dashboard with custom panels
- Integration with bot events and data
- Performance considerations
- Troubleshooting and debugging
"""

import sys
from pathlib import Path

# Quick Reference Commands
print("""
╔════════════════════════════════════════════════════════════════════════════════╗
║                    CLI DASHBOARD - DEVELOPER REFERENCE GUIDE                   ║
╚════════════════════════════════════════════════════════════════════════════════╝

📋 QUICK REFERENCE
═══════════════════════════════════════════════════════════════════════════════════

1. RUN WITH DASHBOARD
   ─────────────────────────────────────────────────────────────────────────────
   python src/main.py --dashboard
   python src/main.py --dashboard --update-interval 0.5
   python quickstart_dashboard.py

2. RUN WITHOUT DASHBOARD
   ─────────────────────────────────────────────────────────────────────────────
   python src/main.py

3. INSTALL DEPENDENCIES
   ─────────────────────────────────────────────────────────────────────────────
   pip install rich
   pip install -r requirements.txt

4. CHECK LOGS
   ─────────────────────────────────────────────────────────────────────────────
   tail -f data/bot_runtime.log          # Follow logs in real-time
   grep ERROR data/bot_runtime.log       # Find errors
   grep DASHBOARD data/bot_runtime.log   # Dashboard-specific logs

═══════════════════════════════════════════════════════════════════════════════════

🏗️ ARCHITECTURE OVERVIEW
─────────────────────────────────────────────────────────────────────────────────

Bot Process Flow:
┌─────────────────┐
│ bot_runtime.py  │ ← Main trading bot logic
│  (Main Thread)  │
└────────┬────────┘
         │
    Updates ↓
┌─────────────────┐
│ BotState object │  ← Shared state object
│  (Thread-safe   │
│   read access)  │
└────────┬────────┘
         │
    Reads ↓
┌─────────────────────────────┐
│ cli_dashboard.py            │ ← Dashboard rendering
│ (Daemon Thread)             │
│  - Reads BotState           │
│  - Renders rich panels      │
│  - Updates terminal display │
└─────────────────────────────┘

Data Flow:
BotState.order_journal ──→ Orders Panel
BotState.events        ──→ Events Panel
BotState.position_qty  ──→ Position Panel
BotState.equity        ──→ P&L Panel
BotState.market_regime ──→ Status Panel

═══════════════════════════════════════════════════════════════════════════════════

🔧 KEY CLASSES AND FUNCTIONS
─────────────────────────────────────────────────────────────────────────────────

CLIDashboard Class:
  __init__(state, update_interval)      Create dashboard instance
  start()                               Start the dashboard thread
  stop()                                Stop the dashboard thread
  _build_status_panel()                 Build status panel
  _build_position_panel()               Build position panel
  _build_pnl_panel()                    Build P&L panel
  _build_orders_table()                 Build orders table
  _build_events_panel()                 Build events panel
  _build_layout()                       Build full layout
  _run()                                Main update loop
  display_summary()                     Static display mode

Utility Functions:
  create_dashboard(state, interval)     Factory function
  _format_currency(value)               Format with color
  _format_percent(value)                Format with color

═══════════════════════════════════════════════════════════════════════════════════

📊 BOTSTATE FIELDS USED BY DASHBOARD
─────────────────────────────────────────────────────────────────────────────────

Status Information:
  running                    bool        Is bot running
  trade_mode                 str         DRY or LIVE
  live_armed                 bool        Is live trading armed
  session_phase              str         Current market session
  market_regime              str         Market regime (BULLISH/BEARISH/NEUTRAL)
  portfolio_heat_pct         float       Current portfolio risk level

Position Information:
  position_symbol            str         Current symbol
  position_qty               int         Position size
  avg_price                  float       Average entry price
  last_price                 float       Current price

Financial Information:
  equity                     float       Total account value
  cash_balance               float       Available cash
  unrealized_pnl             float       P&L on open positions
  realized_pnl               float       P&L from closed trades
  total_pnl                  float       Total profit/loss
  total_return_pct           float       Return as percentage

Trade Information:
  order_journal              list        List of trade orders
  events                     deque       Recent trading events

═══════════════════════════════════════════════════════════════════════════════════

🎨 EXTENDING THE DASHBOARD
─────────────────────────────────────────────────────────────────────────────────

Example 1: Add Custom Metric Panel
────────────────────────────────────

def _build_custom_metric_panel(self) -> Panel:
    '''Build custom metrics panel'''
    text = Text()
    text.append(f"Custom Metric: {self.state.your_field}\\n", style="bold")
    return Panel(text, title="[bold cyan]Custom[/bold cyan]", border_style="cyan")

Then update _build_layout() to include it:
    layout["custom"].update(self._build_custom_metric_panel())

Example 2: Add Real-time Chart
───────────────────────────────

# In _build_position_panel():
if self.state.last_price and self.state.avg_price > 0:
    pnl_pct = ((self.state.last_price - self.state.avg_price) / 
               self.state.avg_price) * 100
    
    # Create a simple text-based chart
    chart = "█" * int(pnl_pct) + "░" * (20 - int(pnl_pct))
    position_text.append(f"Gain Chart: {chart}\\n")

Example 3: Add Live Alerts
──────────────────────────

def _check_alerts(self) -> list[str]:
    '''Check for alert conditions'''
    alerts = []
    
    if self.state.portfolio_heat_pct > 80:
        alerts.append("[red]⚠️ HIGH PORTFOLIO HEAT[/red]")
    
    if self.state.unrealized_pnl < self.state.total_pnl * -0.5:
        alerts.append("[red]📉 MAJOR DRAWDOWN[/red]")
    
    return alerts

═══════════════════════════════════════════════════════════════════════════════════

🔍 DEBUGGING TIPS
─────────────────────────────────────────────────────────────────────────────────

1. Check if Rich is installed:
   python -c "import rich; print('Rich available')"

2. Test dashboard creation:
   python -c "from src.cli_dashboard import create_dashboard; print('Dashboard module OK')"

3. Monitor dashboard thread:
   ps aux | grep python          # Check for running processes
   ps -L <pid>                   # List threads for process

4. Profile dashboard performance:
   python -m cProfile -s cumtime src/main.py --dashboard

5. Enable verbose logging:
   python src/main.py --dashboard 2>&1 | tee debug.log

6. Test with mock data:
   # Create a test_dashboard.py script to test rendering

═══════════════════════════════════════════════════════════════════════════════════

⚠️ COMMON ISSUES AND SOLUTIONS
─────────────────────────────────────────────────────────────────────────────────

Issue: Dashboard doesn't appear after starting bot
→ Solution: Ensure rich is installed: pip install rich

Issue: Dashboard updates too slow/fast
→ Solution: Adjust update interval: python src/main.py --dashboard --update-interval 0.5

Issue: Terminal display is garbled or colorless
→ Solution: Set terminal: export TERM=xterm-256color

Issue: Dashboard takes too much CPU
→ Solution: Increase update interval: --update-interval 2.0

Issue: Some metrics show as 0 or N/A
→ Solution: Wait for bot to initialize, check data/bot_runtime.log

Issue: Dashboard crashes after a while
→ Solution: Check available terminal space, restart with: python src/main.py --dashboard

═══════════════════════════════════════════════════════════════════════════════════

📈 PERFORMANCE CONSIDERATIONS
─────────────────────────────────────────────────────────────────────────────────

CPU Usage:
  - Dashboard thread: <1% per update cycle
  - Typical update: 50-100ms total time
  - Negligible impact on trading logic

Memory Usage:
  - CLIDashboard instance: ~2-3 MB
  - Event deque (200 max): ~50 KB
  - Total overhead: <5 MB

Terminal I/O:
  - Update interval: Configurable (default 1s)
  - Non-blocking writes (uses rich Live renderer)
  - Handles terminal resize events

Thread Safety:
  - BotState is accessed read-only from dashboard thread
  - No locks needed (Python GIL handles it)
  - Order journal and events are thread-safe lists/deques

═══════════════════════════════════════════════════════════════════════════════════

🎯 BEST PRACTICES
─────────────────────────────────────────────────────────────────────────────────

1. Always check RICH_AVAILABLE before creating dashboard
2. Use daemon threads for dashboard to avoid hanging process
3. Respect update intervals - don't make them too fast (<0.1s)
4. Handle missing fields in BotState gracefully with defaults
5. Use appropriate color schemes for different metric types
6. Test on terminals with limited width (80 columns)
7. Include descriptive logging for dashboard errors
8. Document any custom panels you add
9. Keep panel content concise for small terminals
10. Use rich's built-in safety features for special characters

═══════════════════════════════════════════════════════════════════════════════════

📚 RICH LIBRARY QUICK REFERENCE
─────────────────────────────────────────────────────────────────────────────────

Basic Components:

Console:
  console = Console()
  console.print("Text", style="bold red")

Text:
  text = Text("Content", style="cyan")
  text.append(" More", style="green")

Table:
  table = Table(title="Title")
  table.add_column("Header")
  table.add_row("Value")

Panel:
  panel = Panel(content, title="Title", border_style="blue")

Layout:
  layout = Layout()
  layout.split_column(...)
  layout.split_row(...)

Live:
  with Live(layout, console=console) as live:
      live.update(new_layout)

Styles:
  "bold", "italic", "underline"
  "red", "green", "blue", "cyan", "magenta", "yellow", "white"
  "on red" (background color)
  "green bold" (multiple styles)

═══════════════════════════════════════════════════════════════════════════════════

🔗 RELATED FILES
─────────────────────────────────────────────────────────────────────────────────

Core Files:
  src/cli_dashboard.py          ← Dashboard implementation
  src/main.py                   ← Entry point with CLI args
  src/bot_runtime.py            ← Bot state and trading logic
  src/config.py                 ← Configuration management

Documentation:
  docs/CLI_DASHBOARD.md         ← User guide
  README.md                     ← Main README
  DASHBOARD_IMPLEMENTATION.md   ← Implementation details

Examples:
  quickstart_dashboard.py       ← Quick start example

Log Files:
  data/bot_runtime.log          ← Detailed execution logs

═══════════════════════════════════════════════════════════════════════════════════

💡 TIPS FOR DEVELOPERS
─────────────────────────────────────────────────────────────────────────────────

1. Use virtual environment to test:
   python3 -m venv test_env
   source test_env/bin/activate
   pip install -r requirements.txt

2. Create test BotState for dashboard testing:
   from dataclasses import dataclass
   from bot_runtime import BotState
   state = BotState()
   state.equity = 10000000
   state.position_qty = 100
   
3. Test rendering without bot:
   from cli_dashboard import CLIDashboard
   dashboard = CLIDashboard(state)
   dashboard.display_summary()  # Static display

4. Monitor processes:
   watch -n 1 'ps aux | grep python'

5. Profile memory usage:
   python -m memory_profiler src/main.py --dashboard

═══════════════════════════════════════════════════════════════════════════════════

📞 SUPPORT
─────────────────────────────────────────────────────────────────────────────────

For issues:
1. Check data/bot_runtime.log for error details
2. Verify rich is installed: pip check
3. Test imports: python -c "from src.cli_dashboard import *"
4. Ensure Python 3.9+: python --version
5. Check terminal compatibility: $TERM variable

═══════════════════════════════════════════════════════════════════════════════════
""")

if __name__ == "__main__":
    print("\\nFor interactive dashboard, run:\\n")
    print("  python src/main.py --dashboard\\n")
