#!/usr/bin/env python3
"""
Simulation Dashboard - Replay historic data with strategy and visualize results.

This dashboard replays stored market data, applies your trading strategy decisions,
and displays the simulated trading activity in real-time with metrics tracking.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

try:
    from rich.console import Console
    from rich.table import Table
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text
    from rich.progress import Progress, BarColumn, DownloadColumn, TextColumn, TimeRemainingColumn
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False


@dataclass
class SimulationTrade:
    """Represents a simulated trade."""
    timestamp: str
    symbol: str
    trade_type: str  # BUY or SELL
    price: float
    quantity: int
    reason: str


@dataclass 
class SimulationState:
    """Tracks simulation state."""
    current_bar: int = 0
    total_bars: int = 0
    symbol: str = ""
    paused: bool = False
    data_source: str = ""  # Path to data file
    data_start_date: str = ""  # First bar date
    data_end_date: str = ""  # Last bar date
    replay_speed: float = 1.0  # 1.0 = normal, 0.5 = half speed, 2.0 = double
    
    # Position tracking
    position_qty: int = 0
    entry_price: float = 0.0
    current_price: float = 0.0
    current_bar_time: str = ""
    
    # P&L tracking
    cash: float = 10000000.0  # Start with 10M
    equity: float = 10000000.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    trades: list[SimulationTrade] = field(default_factory=list)
    
    # Performance metrics
    win_count: int = 0
    loss_count: int = 0
    max_drawdown: float = 0.0
    peak_equity: float = 10000000.0


class SimulationDashboard:
    """Dashboard for replaying historical data with strategy."""
    
    def __init__(self, bars_data: list[dict], symbol: str = "005930", speed: float = 1.0, data_source: str = ""):
        """Initialize simulation dashboard.
        
        Args:
            bars_data: List of OHLCV bars from historical data
            symbol: Stock symbol being simulated
            speed: Replay speed (1.0 = normal, 0.5 = half, 2.0 = double)
            data_source: Path or description of data source
        """
        if not RICH_AVAILABLE:
            raise ImportError("Simulation dashboard requires 'rich'. Install with: pip install rich")
        
        self.console = Console()
        self.bars_data = bars_data or []
        self.symbol = symbol
        
        # Extract date range from first and last bars
        start_date = str(bars_data[0].get("timestamp", "N/A")) if bars_data else "N/A"
        end_date = str(bars_data[-1].get("timestamp", "N/A")) if bars_data else "N/A"
        
        self.state = SimulationState(
            total_bars=len(bars_data),
            symbol=symbol,
            replay_speed=speed,
            data_source=data_source,
            data_start_date=start_date,
            data_end_date=end_date
        )
        self.running = False
        self.thread: threading.Thread | None = None
        self._bar_trades: deque[SimulationTrade] = deque(maxlen=10)
        
    def _format_currency(self, value: float) -> Text:
        """Format currency with color coding."""
        if value >= 0:
            return Text(f"₩{value:,.0f}", style="green")
        else:
            return Text(f"₩{value:,.0f}", style="red")
    
    def _format_percent(self, value: float) -> Text:
        """Format percentage with color coding."""
        if value >= 0:
            return Text(f"{value:+.2f}%", style="green")
        else:
            return Text(f"{value:+.2f}%", style="red")
    
    def _apply_strategy_decision(self, bar: dict) -> tuple[str, str]:
        """Apply strategy to current bar and return (action, reason).
        
        Args:
            bar: Current OHLCV bar
            
        Returns:
            Tuple of (action, reason) where action is HOLD/BUY/SELL
        """
        close = float(bar.get("close", 0.0))
        volume = float(bar.get("volume", 0.0))
        
        # Simple strategy: Buy on dips, sell on rallies
        # (You can replace this with your actual strategy logic)
        
        if self.state.position_qty == 0:  # Look for entry
            # Entry signal: lower volumes or momentum
            avg_volume = volume  # Simplified
            if close > 0:
                return "BUY", "Entry signal - momentum detected"
        else:  # Have position, look for exit
            pnl_pct = ((close - self.state.entry_price) / self.state.entry_price) * 100
            
            # Exit: take profit at 2%, stop loss at -1%
            if pnl_pct >= 2.0:
                return "SELL", f"Take profit at +{pnl_pct:.2f}%"
            elif pnl_pct <= -1.0:
                return "SELL", f"Stop loss at {pnl_pct:.2f}%"
        
        return "HOLD", "Waiting for signal"
    
    def _process_bar(self, bar: dict) -> None:
        """Process a single bar of data."""
        close = float(bar.get("close", 0.0))
        timestamp = str(bar.get("timestamp", ""))
        
        self.state.current_price = close
        self.state.current_bar_time = timestamp
        
        # Get strategy decision
        action, reason = self._apply_strategy_decision(bar)
        
        # Execute trade if action requires it
        if action == "BUY" and self.state.position_qty == 0:
            # Open position
            self.state.position_qty = 100  # 100 shares
            self.state.entry_price = close
            self.state.cash -= close * 100
            
            trade = SimulationTrade(
                timestamp=timestamp,
                symbol=self.symbol,
                trade_type="BUY",
                price=close,
                quantity=100,
                reason=reason
            )
            self.state.trades.append(trade)
            self._bar_trades.append(trade)
            self.console.log(f"[green]BUY[/green]: {reason} @ ₩{close:,.0f}")
            
        elif action == "SELL" and self.state.position_qty > 0:
            # Close position
            sell_value = close * self.state.position_qty
            self.state.cash += sell_value
            
            pnl = (close - self.state.entry_price) * self.state.position_qty
            self.state.realized_pnl += pnl
            
            if pnl > 0:
                self.state.win_count += 1
            else:
                self.state.loss_count += 1
            
            trade = SimulationTrade(
                timestamp=timestamp,
                symbol=self.symbol,
                trade_type="SELL",
                price=close,
                quantity=self.state.position_qty,
                reason=reason
            )
            self.state.trades.append(trade)
            self._bar_trades.append(trade)
            self.console.log(f"[red]SELL[/red]: {reason} @ ₩{close:,.0f} | P&L: ₩{pnl:,.0f}")
            
            self.state.position_qty = 0
            self.state.entry_price = 0.0
        
        # Update equity
        if self.state.position_qty > 0:
            self.state.unrealized_pnl = (close - self.state.entry_price) * self.state.position_qty
        else:
            self.state.unrealized_pnl = 0.0
        
        self.state.equity = self.state.cash + (close * self.state.position_qty) + self.state.unrealized_pnl
        
        # Track max drawdown
        if self.state.equity > self.state.peak_equity:
            self.state.peak_equity = self.state.equity
        else:
            dd = 1 - (self.state.equity / self.state.peak_equity)
            if dd > self.state.max_drawdown:
                self.state.max_drawdown = dd
    
    def _build_header_panel(self) -> Panel:
        """Build simulation info panel."""
        header = Text()
        header.append(f"Simulating {self.symbol} | ", style="bold cyan")
        header.append(f"Bar {self.state.current_bar}/{self.state.total_bars} | ", style="cyan")
        
        progress_pct = (self.state.current_bar / max(1, self.state.total_bars)) * 100
        header.append(f"Progress: {progress_pct:.1f}% | ", style="cyan")
        
        if self.state.paused:
            header.append("[PAUSED]", style="yellow bold")
        else:
            header.append("[RUNNING]", style="green bold")
        
        return Panel(header, border_style="blue", padding=(1, 2))
    
    def _build_data_panel(self) -> Panel:
        """Build data source information panel."""
        text = Text()
        text.append("Data Source: ", style="bold")
        text.append(f"{self.state.data_source}\n", style="cyan")
        
        text.append("Period: ", style="bold")
        text.append(f"{self.state.data_start_date}", style="cyan")
        text.append(" → ", style="cyan")
        text.append(f"{self.state.data_end_date}\n", style="cyan")
        
        text.append("Total Bars: ", style="bold")
        text.append(f"{self.state.total_bars:,}\n", style="cyan")
        
        text.append("Replay Speed: ", style="bold")
        text.append(f"{self.state.replay_speed:.1f}x", style="cyan")
        
        return Panel(text, title="[bold cyan]Data Info[/bold cyan]", border_style="cyan")
    
    def _build_position_panel(self) -> Panel:
        """Build position status panel."""
        text = Text()
        
        if self.state.position_qty <= 0:
            text.append("No active position", style="dim")
        else:
            text.append(f"Symbol: {self.symbol}\n", style="bold")
            text.append(f"Quantity: {self.state.position_qty:,} shares\n", style="bold")
            text.append(f"Entry Price: ₩{self.state.entry_price:,.0f}\n", style="bold")
            text.append(f"Current Price: ₩{self.state.current_price:,.0f}\n", style="bold")
            
            if self.state.entry_price > 0:
                pnl_pct = ((self.state.current_price - self.state.entry_price) / self.state.entry_price) * 100
                pnl_color = "green" if pnl_pct >= 0 else "red"
                text.append(f"Unrealized P&L: ", style="bold")
                text.append(f"₩{self.state.unrealized_pnl:,.0f} ({pnl_pct:+.2f}%)\n", style=pnl_color)
        
        return Panel(text, title="[bold cyan]Position[/bold cyan]", border_style="cyan")
    
    def _build_pnl_panel(self) -> Panel:
        """Build P&L panel."""
        text = Text()
        text.append("Cash: ", style="bold")
        text.append(f"₩{self.state.cash:,.0f}\n", style="cyan")
        
        text.append("Equity: ", style="bold")
        text.append(f"₩{self.state.equity:,.0f}\n", style="cyan")
        
        text.append("Realized P&L: ", style="bold")
        text.append(f"₩{self.state.realized_pnl:,.0f}\n", style=("green" if self.state.realized_pnl >= 0 else "red"))
        
        text.append("Unrealized P&L: ", style="bold")
        text.append(f"₩{self.state.unrealized_pnl:,.0f}\n", style=("green" if self.state.unrealized_pnl >= 0 else "red"))
        
        total_pnl = self.state.realized_pnl + self.state.unrealized_pnl
        return_pct = (total_pnl / 10000000.0) * 100 if total_pnl != 0 else 0.0
        
        text.append("Total P&L: ", style="bold")
        text.append(f"₩{total_pnl:,.0f}\n", style=("green bold" if total_pnl >= 0 else "red bold"))
        
        text.append("Return: ", style="bold")
        text.append(f"{return_pct:+.2f}%", style=("green" if return_pct >= 0 else "red"))
        
        return Panel(text, title="[bold cyan]P&L Summary[/bold cyan]", border_style="cyan")
    
    def _build_metrics_panel(self) -> Panel:
        """Build performance metrics panel."""
        text = Text()
        text.append(f"Trades Executed: {len(self.state.trades)}\n", style="bold")
        text.append(f"Wins: {self.state.win_count} | ", style="bold green")
        text.append(f"Losses: {self.state.loss_count}\n", style="bold red")
        
        win_rate = (self.state.win_count / max(1, self.state.win_count + self.state.loss_count)) * 100
        text.append(f"Win Rate: {win_rate:.1f}%\n", style="bold")
        
        text.append(f"Max Drawdown: {self.state.max_drawdown*100:.2f}%\n", style="bold yellow")
        text.append(f"Time: {self.state.current_bar_time}", style="dim")
        
        return Panel(text, title="[bold cyan]Metrics[/bold cyan]", border_style="cyan")
    
    def _build_trades_table(self) -> Table:
        """Build recent trades table."""
        table = Table(title="Recent Trades", show_header=True, header_style="bold magenta")
        table.add_column("Type", style="cyan")
        table.add_column("Price", justify="right", style="cyan")
        table.add_column("Qty", justify="right", style="cyan")
        table.add_column("Reason", style="dim")
        
        for trade in list(self._bar_trades):
            trade_type = Text(trade.trade_type, style="green" if trade.trade_type == "BUY" else "red")
            table.add_row(
                trade_type,
                f"₩{trade.price:,.0f}",
                str(trade.quantity),
                trade.reason[:30]
            )
        
        return table
    
    def _build_layout(self) -> Layout:
        """Build simulation dashboard layout."""
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="data_info", size=4),
            Layout(name="main"),
            Layout(name="footer", size=2)
        )
        
        layout["header"].update(self._build_header_panel())
        layout["data_info"].update(self._build_data_panel())
        
        # Main content
        left_panel = Layout(name="left")
        left_panel.split_column(
            Layout(name="position"),
            Layout(name="pnl")
        )
        
        right_panel = Layout(name="right")
        right_panel.split_column(
            Layout(name="metrics"),
            Layout(name="trades")
        )
        
        layout["main"].split_row(left_panel, right_panel)
        
        layout["position"].update(self._build_position_panel())
        layout["pnl"].update(self._build_pnl_panel())
        layout["metrics"].update(self._build_metrics_panel())
        layout["trades"].update(self._build_trades_table())
        
        # Footer
        footer = Text("Simulation complete!" if self.state.current_bar >= self.state.total_bars else "Running...", style="dim", justify="center")
        layout["footer"].update(footer)
        
        return layout
    
    def start(self) -> None:
        """Start simulation."""
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
    
    def stop(self) -> None:
        """Stop simulation."""
        self.running = False
        if self.thread:
            self.thread.join(timeout=2)
    
    def _run(self) -> None:
        """Main simulation loop."""
        try:
            with Live(self._build_layout(), console=self.console, refresh_per_second=2, screen=True) as live:
                for idx, bar in enumerate(self.bars_data):
                    if not self.running:
                        break
                    
                    self.state.current_bar = idx + 1
                    self._process_bar(bar)
                    live.update(self._build_layout())
                    
                    # Simulate replay speed
                    time.sleep(0.5 / self.state.replay_speed)
                
                # Final display
                live.update(self._build_layout())
                self.console.print("\n[green]✓[/green] Simulation complete!")
                self._print_summary()
                
        except Exception as e:
            self.console.print(f"[red]Error during simulation: {e}[/red]")
    
    def _print_summary(self) -> None:
        """Print final simulation summary."""
        total_pnl = self.state.realized_pnl + self.state.unrealized_pnl
        return_pct = (total_pnl / 10000000.0) * 100
        
        self.console.print("\n" + "="*60)
        self.console.print("[bold cyan]SIMULATION SUMMARY[/bold cyan]")
        self.console.print("="*60)
        self.console.print(f"Symbol: {self.symbol}")
        self.console.print(f"Bars Processed: {self.state.current_bar}/{self.state.total_bars}")
        self.console.print(f"Total Trades: {len(self.state.trades)}")
        self.console.print(f"Wins/Losses: {self.state.win_count}/{self.state.loss_count}")
        
        if self.state.win_count + self.state.loss_count > 0:
            wr = (self.state.win_count / (self.state.win_count + self.state.loss_count)) * 100
            self.console.print(f"Win Rate: {wr:.1f}%")
        
        self.console.print(f"Final Equity: ₩{self.state.equity:,.0f}")
        self.console.print(f"Final P&L: ₩{total_pnl:,.0f} ({return_pct:+.2f}%)", style="green bold" if total_pnl >= 0 else "red bold")
        self.console.print(f"Max Drawdown: {self.state.max_drawdown*100:.2f}%")
        self.console.print("="*60 + "\n")


def load_historical_data(symbol: str, data_file: Path | None = None) -> tuple[list[dict], str]:
    """Load historical OHLCV data for simulation.
    
    Args:
        symbol: Stock symbol (e.g., "005930")
        data_file: Optional path to JSON data file
        
    Returns:
        Tuple of (bars list, data source path)
    """
    if data_file and data_file.exists():
        try:
            with open(data_file) as f:
                data = json.load(f)
            bars = data.get("bars", []) or data.get("rows", [])
            if isinstance(bars, list):
                return bars, str(data_file)
        except Exception as e:
            print(f"Error loading {data_file}: {e}")
    
    # Try default backtest cache locations
    cache_paths = [
        Path(f"data/backtest_cache/kr_{symbol}_daily.json"),
        Path(f"data/backtest_cache/kr_{symbol}_daily.json"),
    ]
    
    for path in cache_paths:
        if path.exists():
            try:
                with open(path) as f:
                    data = json.load(f)
                bars = data.get("bars", []) or data.get("rows", [])
                if isinstance(bars, list):
                    return bars, str(path)
            except Exception as e:
                print(f"Error loading {path}: {e}")
    
    print(f"No historical data found for {symbol}")
    return [], f"No data found for {symbol}"


def create_simulation_dashboard(symbol: str = "005930", speed: float = 1.0, data_file: Path | None = None) -> SimulationDashboard:
    """Factory function to create a simulation dashboard.
    
    Args:
        symbol: Stock symbol to simulate
        speed: Replay speed (1.0 = normal, 0.5 = half, 2.0 = double)
        data_file: Optional path to custom data file
        
    Returns:
        SimulationDashboard instance
    """
    bars_data, data_source = load_historical_data(symbol, data_file)
    if not bars_data:
        raise ValueError(f"No data available for {symbol}")
    
    return SimulationDashboard(bars_data, symbol, speed, data_source)
