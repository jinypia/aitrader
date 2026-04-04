#!/usr/bin/env python3
"""
Professional Scalping Simulator - 2-Minute Bars + Intraday Stock Selection

Enhanced with:
- 2-minute bar support (195 bars/day for better trading opportunities)
- Intraday stock selection by current liquidity/volatility
- Professional parameters optimized for scalping
- Real-time metrics tracking
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from scalping_data_loader import get_day_price_data, get_today_scalping_data, get_day_data_preview
from scalping_strategy import ScalpParams, calculate_scalp_metrics, scalp_entry_signal, scalp_exit_signal
from intraday_stock_selector import (
    get_best_scalping_stocks,
    display_scalping_stocks,
    rank_stocks_by_liquidity
)


def run_scalping_simulation(
    symbol: str,
    date_str: str,
    bar_interval: int = 2,
    show_details: bool = False
) -> dict:
    """Run scalping simulation on a specific day with 2-minute bars.
    
    Args:
        symbol: Stock symbol
        date_str: Date (YYYY-MM-DD)
        bar_interval: Bar size in minutes (2, 5, or 10). Default 2 for scalping.
        show_details: Show detailed trade logs
        
    Returns:
        Simulation results dictionary
    """
    # Load data - now generates 2-minute bars by default
    bars, source = get_day_price_data(symbol, date_str, bar_interval=bar_interval)
    if not bars:
        return {
            "success": False,
            "error": f"No data found for {symbol} on {date_str}"
        }
    
    if show_details:
        bar_type = f"{bar_interval}-min" if bar_interval else "10-min"
        print(f"📊 Loaded {len(bars)} {bar_type} bars from {source}\n")
    
    # Initialize strategy with professional parameters
    params = ScalpParams()
    
    # Simulation state
    position = 0
    entry_price = 0.0
    entry_bar = 0
    cash = 10000000.0  # 10M starting capital
    equity = 10000000.0
    trades = []
    
    bars_processed = 0
    current_pnl = 0.0
    max_equity = equity
    max_drawdown = 0.0
    
    if show_details:
        print("=" * 100)
        print(f"🚀 PROFESSIONAL SCALPING SIMULATION: {symbol} on {date_str}")
        print(f"   Strategy Parameters: {bar_interval}-min bars, RSI({params.rsi_period}), Targets/Stops: {params.profit_target_pct}%/{params.stop_loss_pct}%")
        print("=" * 100)
        print()
    
    # Process each bar
    for i, bar in enumerate(bars):
        bars_processed += 1
        current_price = bar.get("close", 0)
        timestamp = bar.get("timestamp", "")
        
        if position == 0:
            # No position - check for entry signal
            recent_bars = bars[max(0, i-15):i+1]
            metrics = calculate_scalp_metrics(recent_bars, params)
            
            if scalp_entry_signal(metrics, params):
                position = 1  # Go long
                entry_price = current_price
                entry_bar = i
                
                trade_entry = {
                    "timestamp": timestamp,
                    "type": "BUY",
                    "price": entry_price,
                    "volume": int(cash / entry_price) if entry_price > 0 else 0,
                    "reason": "Scalp entry signal"
                }
                trades.append(trade_entry)
                
                if show_details:
                    rsi_val = metrics.get("rsi", 0)
                    print(f"[{timestamp}] 📈 BUY  @ ₩{entry_price:,.0f} | RSI({params.rsi_period})={rsi_val:.1f} Vol={metrics.get('volume_ratio', 0):.1f}x Momentum={metrics.get('momentum', 0):.2f}%")
        
        else:
            # In position - check for exit signal
            hold_bars = i - entry_bar
            pnl_pct = (current_price - entry_price) / entry_price * 100
            
            # Get current metrics
            recent_bars = bars[max(0, i-15):i+1]
            metrics = calculate_scalp_metrics(recent_bars, params)
            rsi = metrics.get("rsi", 50)
            atr = metrics.get("atr", 0)
            
            # Check exit conditions
            exit_reason = scalp_exit_signal(
                entry_price,
                current_price,
                hold_bars,
                rsi,
                params,
                atr
            )
            
            if exit_reason:
                position = 0
                pnl_won = (current_price - entry_price) * (cash // entry_price) if entry_price > 0 else 0
                cash += pnl_won
                equity = cash
                current_pnl += pnl_won
                
                trade_exit = {
                    "timestamp": timestamp,
                    "type": "SELL",
                    "price": current_price,
                    "pnl": pnl_won,
                    "pnl_pct": pnl_pct,
                    "hold_minutes": hold_bars * bar_interval,
                    "reason": exit_reason
                }
                trades.append(trade_exit)
                
                if show_details:
                    status = "✅" if pnl_pct >= 0 else "❌"
                    hold_time = f"{hold_bars * bar_interval} min"
                    print(f"[{timestamp}] {status} SELL @ ₩{current_price:,.0f} | PnL: ₩{pnl_won:,.0f} ({pnl_pct:+.3f}%) | Hold: {hold_time} | Exit: {exit_reason}")
            
            # Track equity
            unrealized = (current_price - entry_price) * (cash // entry_price) if entry_price > 0 and position > 0 else 0
            equity = cash + unrealized
            
            max_equity = max(max_equity, equity)
            drawdown = (max_equity - equity) / max_equity * 100 if max_equity > 0 else 0
            max_drawdown = max(max_drawdown, drawdown)
    
    # Close any open position
    if position > 0 and bars:
        final_price = bars[-1].get("close", 0)
        pnl_won = (final_price - entry_price) * (cash // entry_price) if entry_price > 0 else 0
        cash += pnl_won
        equity = cash
        current_pnl += pnl_won
    
    # Calculate results
    sell_trades = [t for t in trades if t.get("type") == "SELL"]
    win_count = len([t for t in sell_trades if t.get("pnl_pct", 0) >= 0])
    loss_count = len([t for t in sell_trades if t.get("pnl_pct", 0) < 0])
    
    trade_count = max(1, (len(trades) - 1) // 2)
    win_rate = (win_count / max(1, loss_count + win_count)) * 100
    total_return_pct = ((equity - 10000000.0) / 10000000.0) * 100
    
    return {
        "success": True,
        "symbol": symbol,
        "date": date_str,
        "bar_interval": bar_interval,
        "bars_processed": bars_processed,
        "bars_source": source,
        "equity": equity,
        "cash": cash,
        "pnl": current_pnl,
        "pnl_pct": total_return_pct,
        "trade_count": trade_count,
        "win_count": win_count,
        "loss_count": loss_count,
        "win_rate": win_rate,
        "max_drawdown": max_drawdown,
        "trades": trades
    }


def display_results(results: dict):
    """Display simulation results in formatted output."""
    if not results.get("success"):
        print(f"❌ Error: {results.get('error')}")
        return
    
    print("\n" + "=" * 80)
    print("📊 PROFESSIONAL SCALPING RESULTS (2-Minute Bars)")
    print("=" * 80)
    
    print(f"\n📈 Symbol:        {results['symbol']}")
    print(f"📅 Date:          {results['date']}")
    print(f"⏱️  Bar Interval:   {results['bar_interval']}-minute")
    print(f"📊 Bars:          {results['bars_processed']}")
    print(f"📁 Source:        {results['bars_source']}\n")
    
    print("PERFORMANCE METRICS")
    print("─" * 80)
    
    print(f"💰 Equity:       ₩{results['equity']:,.0f}")
    print(f"📊 P&L:          ₩{results['pnl']:+,.0f} ({results['pnl_pct']:+.2f}%)")
    print(f"⬇️  Max Drawdown:  {results['max_drawdown']:.2f}%")
    
    print("\nTRADE STATISTICS")
    print("─" * 80)
    
    print(f"📍 Total Trades:  {results['trade_count']}")
    print(f"✅ Wins:          {results['win_count']}")
    print(f"❌ Losses:        {results['loss_count']}")
    print(f"🎯 Win Rate:      {results['win_rate']:.1f}%")
    
    print("\n" + "=" * 80)


def show_best_stocks():
    """Display best stocks for scalping."""
    print("\n" + "=" * 100)
    print("🔍 INTRADAY STOCK SELECTION - BEST CANDIDATES FOR SCALPING")
    print("=" * 100)
    display_scalping_stocks()


def main():
    parser = argparse.ArgumentParser(
        description="Professional scalping strategy simulator with 2-minute bars and stock selection"
    )
    parser.add_argument(
        "--symbol",
        help="Stock symbol (auto-select if omitted)"
    )
    parser.add_argument(
        "--date",
        help="Simulation date (YYYY-MM-DD). If not provided, uses latest available"
    )
    parser.add_argument(
        "--bars",
        type=int,
        default=2,
        choices=[2, 5, 10],
        help="Bar interval in minutes (default: 2 for scalping)"
    )
    parser.add_argument(
        "--today",
        action="store_true",
        help="Simulate on today or latest available data"
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show detailed trade logs"
    )
    parser.add_argument(
        "--stocks",
        action="store_true",
        help="Show best stocks for scalping"
    )
    parser.add_argument(
        "--batch",
        type=int,
        help="Run simulations on top N stocks from today"
    )
    
    args = parser.parse_args()
    
    if args.stocks:
        show_best_stocks()
        return
    
    # Determine simulation date
    if args.date:
        date_str = args.date
    elif args.today:
        date_str = datetime.now().strftime("%Y-%m-%d")
    else:
        date_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    
    # Determine symbol(s)
    if args.batch:
        symbols = [s["symbol"] for s in get_best_scalping_stocks(limit=args.batch)]
    elif args.symbol:
        symbols = [args.symbol]
    else:
        # Auto-select best stock
        best_stocks = get_best_scalping_stocks(limit=1)
        if best_stocks:
            symbols = [best_stocks[0]["symbol"]]
            print(f"📊 Auto-selected: {best_stocks[0]['symbol']} (Score: {best_stocks[0]['score']:.1f})\n")
        else:
            symbols = ["005930"]  # Default fallback
    
    # Run simulations
    all_results = []
    for symbol in symbols:
        print(f"\n🔄 Running scalping simulation...")
        print(f"   Symbol:   {symbol}")
        print(f"   Date:     {date_str}")
        print(f"   Bars:     {args.bars}-minute")
        print()
        
        # Run simulation
        results = run_scalping_simulation(
            symbol=symbol,
            date_str=date_str,
            bar_interval=args.bars,
            show_details=args.verbose
        )
        
        # Display results
        display_results(results)
        
        # Save results
        if results.get("success"):
            output_file = Path(f"data/scalp_sim_{symbol}_{date_str}_{args.bars}min.json")
            import json
            
            # Convert trades for JSON serialization
            json_results = dict(results)
            json_results["trades"] = [
                {k: v for k, v in t.items() if not isinstance(v, (dict, list))}
                for t in results.get("trades", [])
            ]
            
            with open(output_file, "w") as f:
                json.dump(json_results, f, indent=2)
            
            print(f"✅ Results saved to: {output_file}")
            all_results.append(results)
    
    # Summary for batch
    if len(all_results) > 1:
        print("\n" + "=" * 80)
        print(f"📋 BATCH SUMMARY ({len(all_results)} simulations)")
        print("=" * 80)
        avg_win_rate = sum(r["win_rate"] for r in all_results) / len(all_results)
        total_pnl = sum(r["pnl"] for r in all_results)
        print(f"Average Win Rate: {avg_win_rate:.1f}%")
        print(f"Total P&L: ₩{total_pnl:,.0f}")
        print("=" * 80)


if __name__ == "__main__":
    main()
