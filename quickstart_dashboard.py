#!/usr/bin/env python3
"""
Quick Start Example: Running AITRADER with Real-time CLI Dashboard

This example shows how to use the CLI dashboard for monitoring live trading.
"""

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from main import run

if __name__ == "__main__":
    """
    Run the trading bot with the CLI dashboard enabled.
    
    Usage:
        python quickstart_dashboard.py
    
    The dashboard will show:
    - Trading status and mode
    - Current positions
    - P&L summary
    - Recent transactions
    - Event log
    
    Press Ctrl+C to exit the bot and dashboard.
    """
    print("=" * 60)
    print("AITRADER - Real-time Trading Dashboard")
    print("=" * 60)
    print("\n📊 Starting bot with CLI dashboard...")
    print("💡 TIP: Dashboard updates every 1 second")
    print("💡 TIP: Press Ctrl+C to stop the bot\n")
    
    try:
        # Run with dashboard enabled, 1 second update interval
        run(enable_dashboard=True, dashboard_interval=1.0)
    except KeyboardInterrupt:
        print("\n\n✓ Stopped by user")
    except Exception as e:
        print(f"\n\n✗ Error: {e}")
        sys.exit(1)
