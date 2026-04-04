#!/usr/bin/env python3
"""
Interactive: Simulation Dashboard

Replay historical data with your trading strategy.
Supports switching between different stocks.
"""

import argparse
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from simulation_dashboard import create_simulation_dashboard, load_historical_data


def get_available_symbols() -> list[str]:
    """Get list of available symbols in backtest cache."""
    cache_dir = Path("data/backtest_cache")
    if not cache_dir.exists():
        return []
    
    symbols = []
    for file in sorted(cache_dir.glob("kr_*_daily.json")):
        # Extract symbol from filename kr_XXXXXX_daily.json
        parts = file.stem.split("_")
        if len(parts) >= 2 and parts[0] == "kr":
            symbol = parts[1].upper()
            symbols.append(symbol)
    
    return sorted(list(set(symbols)))  # Remove duplicates, sort


def display_available_symbols():
    """Show available symbols to user."""
    symbols = get_available_symbols()
    if not symbols:
        print("❌ No cached data found. Run: python src/fill_krx_cache.py")
        return
    
    print(f"\n📊 Available Symbols ({len(symbols)} total):")
    print("─" * 70)
    for i, symbol in enumerate(symbols[:20], 1):  # Show first 20
        print(f"  {i:2d}. {symbol}")
    
    if len(symbols) > 20:
        print(f"  ... and {len(symbols) - 20} more")
    print("─" * 70)


def run_simulation_loop():
    """Interactive simulation loop allowing stock switching."""
    symbol = "005930"  # Default
    speed = 1.0
    custom_data = None
    
    while True:
        print("\n" + "=" * 70)
        print("AITRADER - Strategy Simulation Dashboard")
        print("=" * 70)
        
        print(f"\n📊 Current Settings:")
        print(f"   Symbol: {symbol}")
        print(f"   Speed: {speed}x")
        print(f"   Custom Data: {'Yes' if custom_data else 'No'}\n")
        
        print("Options:")
        print("  1. Run simulation with current symbol")
        print("  2. Change symbol")
        print("  3. Change speed")
        print("  4. Load custom data file")
        print("  5. View available symbols")
        print("  6. Exit\n")
        
        choice = input("Select option (1-6): ").strip()
        
        if choice == "1":
            print(f"\n🚀 Starting simulation for {symbol} at {speed}x speed...\n")
            try:
                dashboard = create_simulation_dashboard(
                    symbol=symbol,
                    speed=speed,
                    data_file=custom_data
                )
                dashboard.start()
                
                # Wait for simulation to complete
                while dashboard.running:
                    try:
                        import time
                        time.sleep(1)
                    except KeyboardInterrupt:
                        print("\n\n⏹️  Stopping simulation...")
                        dashboard.stop()
                        break
                
            except Exception as e:
                print(f"\n❌ Error: {e}")
        
        elif choice == "2":
            print("\n" + "─" * 70)
            display_available_symbols()
            new_symbol = input("\nEnter symbol (e.g., 005930) or press Enter to cancel: ").strip().upper()
            if new_symbol:
                # Validate symbol exists
                if new_symbol in get_available_symbols():
                    symbol = new_symbol
                    print(f"✓ Symbol changed to {symbol}")
                else:
                    print(f"❌ Symbol {new_symbol} not found in cache")
        
        elif choice == "3":
            try:
                new_speed = float(input("\nEnter replay speed (0.25-4.0): ").strip())
                if 0.25 <= new_speed <= 4.0:
                    speed = new_speed
                    print(f"✓ Speed changed to {speed}x")
                else:
                    print("❌ Speed must be between 0.25 and 4.0")
            except ValueError:
                print("❌ Invalid speed value")
        
        elif choice == "4":
            data_path = input("\nEnter path to data file: ").strip()
            if data_path:
                path = Path(data_path)
                if path.exists():
                    custom_data = path
                    print(f"✓ Custom data set to {path}")
                else:
                    print(f"❌ File not found: {path}")
        
        elif choice == "5":
            print()
            display_available_symbols()
        
        elif choice == "6":
            print("\n👋 Exiting...")
            break
        
        else:
            print("❌ Invalid option. Please select 1-6.")


def main():
    parser = argparse.ArgumentParser(
        description="Run trading strategy simulation on historical data"
    )
    parser.add_argument(
        "--symbol",
        help="Stock symbol to simulate (skip interactive mode)"
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="Replay speed: 0.5=half, 1.0=normal, 2.0=double (default: 1.0)"
    )
    parser.add_argument(
        "--data",
        type=Path,
        help="Optional path to custom historical data JSON file"
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        default=True,
        help="Run in interactive mode with stock selection (default)"
    )
    
    args = parser.parse_args()
    
    # If symbol is provided, run single simulation
    if args.symbol:
        print("=" * 70)
        print("AITRADER - Strategy Simulation Dashboard")
        print("=" * 70)
        print(f"\n📊 Simulating: {args.symbol}")
        print(f"⚡ Speed: {args.speed}x")
        
        if args.data:
            print(f"📁 Data: {args.data}")
        else:
            print(f"📁 Data: Auto-loading from backtest cache...")
            cache_path = Path(f"data/backtest_cache/kr_{args.symbol}_daily.json")
            if cache_path.exists():
                print(f"   → Found: {cache_path}")
        
        print("\n🚀 Starting simulation...\n")
        
        try:
            dashboard = create_simulation_dashboard(
                symbol=args.symbol,
                speed=args.speed,
                data_file=args.data
            )
            dashboard.start()
            
            # Keep the process alive
            while dashboard.running:
                try:
                    import time
                    time.sleep(1)
                except KeyboardInterrupt:
                    print("\n\n⏹️  Stopping simulation...")
                    dashboard.stop()
                    break
                    
        except Exception as e:
            print(f"\n❌ Error: {e}")
            sys.exit(1)
    else:
        # Interactive mode
        run_simulation_loop()


if __name__ == "__main__":
    main()

