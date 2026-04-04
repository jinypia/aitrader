#!/usr/bin/env python3
"""
Real-time CLI Dashboard for monitoring trading transactions and performance.
Displays live trading activity, positions, P&L, and event logs.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

try:
    from rich.console import Console
    from rich.table import Table
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text
    from rich.align import Align
    from rich.progress import Progress, SpinnerColumn, TextColumn
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

if TYPE_CHECKING:
    from bot_runtime import BotState


class CLIDashboard:
    """Real-time CLI dashboard for trading activity monitoring."""

    def __init__(self, state: "BotState", update_interval: float = 1.0):
        """Initialize dashboard.
        
        Args:
            state: BotState object from the running bot
            update_interval: How often to update display (seconds)
        """
        if not RICH_AVAILABLE:
            raise ImportError("This feature requires 'rich' package. Install with: pip install rich")
        
        self.state = state
        self.update_interval = update_interval
        self.console = Console()
        self.running = False
        self.thread: threading.Thread | None = None
        self.last_event_count = 0
        self.last_order_count = 0
        self._recent_events: deque[str] = deque(maxlen=15)
        self._recent_trades: list[dict] = []
        self._market_status_signature = ""
        self._market_status_last_changed_ts = time.time()
        self._market_status_history: deque[dict[str, str]] = deque(maxlen=8)
        self._loop_time_samples: deque[float] = deque(maxlen=120)
        self._perf_badge_good_ms = max(100.0, float(os.getenv("BOT_PERF_BADGE_GOOD_MS", "800")))
        self._perf_badge_warn_ms = max(
            self._perf_badge_good_ms,
            float(os.getenv("BOT_PERF_BADGE_WARN_MS", "1500")),
        )

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

    def _build_status_panel(self) -> Panel:
        """Build trading status panel."""
        status_text = Text()
        
        # Trading status
        if not self.state.running:
            status_text.append("Status: ", style="bold")
            status_text.append("STOPPED", style="red bold")
        else:
            status_text.append("Status: ", style="bold")
            status_text.append("RUNNING", style="green bold")
        
        status_text.append("\n")
        
        # Trade mode
        mode_color = "yellow" if self.state.trade_mode == "DRY" else "red bold"
        status_text.append(f"Mode: {self.state.trade_mode} | ", style="bold")
        status_text.append(f"Armed: {self.state.live_armed}", style="bold")
        status_text.append("\n")
        
        # Session info
        status_text.append(f"Session: {self.state.session_phase} | ", style="bold")
        status_text.append(f"Regime: {self.state.market_regime}\n", style="bold")
        
        # Risk status
        heat_color = "red" if self.state.portfolio_heat_pct >= 70 else "yellow" if self.state.portfolio_heat_pct >= 50 else "green"
        status_text.append(f"Portfolio Heat: ", style="bold")
        status_text.append(f"{self.state.portfolio_heat_pct:.1f}% / {self.state.max_portfolio_heat_pct:.1f}%", style=heat_color)
        
        return Panel(status_text, title="[bold cyan]Trading Status[/bold cyan]", border_style="cyan")

    def _market_status_signature_text(self) -> str:
        """Build a lightweight signature for change detection of market status fields."""
        return "|".join(
            [
                str(self.state.market_regime),
                f"{float(self.state.regime_confidence):.3f}",
                str(self.state.session_phase),
                f"{float(self.state.data_freshness_sec):.1f}",
                str(bool(self.state.stale_data_active)),
                str(bool(self.state.risk_halt_active)),
                str(self.state.daily_selection_status),
                str(self.state.market_flow_summary),
            ]
        )

    def _touch_market_status_update(self) -> None:
        """Track when market status data changed so dashboard can show real update age."""
        current_sig = self._market_status_signature_text()
        if current_sig != self._market_status_signature:
            self._market_status_signature = current_sig
            self._market_status_last_changed_ts = time.time()
            self._market_status_history.append(
                {
                    "ts": datetime.now().strftime("%H:%M:%S"),
                    "regime": str(self.state.market_regime or "UNKNOWN").upper(),
                    "conf": f"{float(self.state.regime_confidence) * 100:.1f}%",
                    "fresh": f"{float(self.state.data_freshness_sec):.1f}s",
                    "stale": "STALE" if bool(self.state.stale_data_active) else "LIVE",
                }
            )

    def _build_market_status_panel(self) -> Panel:
        """Build market status panel with freshness and risk health."""
        self._touch_market_status_update()

        panel_text = Text()
        regime = str(self.state.market_regime or "UNKNOWN").upper()
        if regime == "BULLISH":
            regime_style = "green bold"
        elif regime == "BEARISH":
            regime_style = "red bold"
        elif regime == "NEUTRAL":
            regime_style = "yellow bold"
        else:
            regime_style = "white bold"

        panel_text.append("Regime: ", style="bold")
        panel_text.append(regime, style=regime_style)
        panel_text.append(f" | Confidence: {float(self.state.regime_confidence) * 100:.1f}%\n", style="bold")

        panel_text.append("Session: ", style="bold")
        panel_text.append(f"{self.state.session_phase} ({self.state.session_profile})\n", style="cyan")

        freshness = float(self.state.data_freshness_sec)
        freshness_style = "green" if freshness <= 120 else "yellow" if freshness <= 300 else "red"
        panel_text.append("Data Freshness: ", style="bold")
        panel_text.append(f"{freshness:.1f}s", style=freshness_style)

        if self.state.stale_data_active:
            panel_text.append(" | STALE\n", style="red bold")
            if self.state.stale_data_reason:
                panel_text.append(f"Stale Reason: {self.state.stale_data_reason}\n", style="red")
        else:
            panel_text.append(" | LIVE\n", style="green")

        panel_text.append("Risk Halt: ", style="bold")
        panel_text.append("ON\n" if self.state.risk_halt_active else "OFF\n", style=("red bold" if self.state.risk_halt_active else "green"))

        panel_text.append("Selection: ", style="bold")
        panel_text.append(f"{self.state.daily_selection_status}\n", style="magenta")

        flow_summary = str(self.state.market_flow_summary or "-").strip()
        if len(flow_summary) > 88:
            flow_summary = flow_summary[:85] + "..."
        panel_text.append("Flow: ", style="bold")
        panel_text.append(flow_summary + "\n", style="white")

        changed_age_sec = max(0.0, time.time() - self._market_status_last_changed_ts)
        panel_text.append("Market Status Updated: ", style="bold")
        panel_text.append(f"{changed_age_sec:.1f}s ago\n", style="dim")

        if self._market_status_history:
            panel_text.append("Recent Updates:\n", style="bold")
            for row in list(self._market_status_history)[-4:]:
                stale_style = "red" if row["stale"] == "STALE" else "green"
                panel_text.append(f"{row['ts']} ", style="dim")
                panel_text.append(f"{row['regime']} ", style=regime_style)
                panel_text.append(f"{row['conf']} ", style="cyan")
                panel_text.append(f"{row['fresh']} ", style="yellow")
                panel_text.append(f"{row['stale']}\n", style=stale_style)

        return Panel(panel_text, title="[bold cyan]Market Status[/bold cyan]", border_style="cyan")

    def _build_position_panel(self) -> Panel:
        """Build current position panel."""
        if self.state.position_qty <= 0:
            return Panel(
                Text("No active position", style="dim"),
                title="[bold cyan]Position[/bold cyan]",
                border_style="cyan"
            )
        
        position_text = Text()
        position_text.append(f"Symbol: {self.state.position_symbol}\n", style="bold")
        position_text.append(f"Quantity: {self.state.position_qty:,}\n", style="bold")
        position_text.append(f"Average Price: ₩{self.state.avg_price:,.0f}\n", style="bold")
        position_text.append(f"Current Price: ₩{self.state.last_price:,.0f}\n", style="bold")
        
        # Calculate position P&L
        if self.state.last_price and self.state.avg_price > 0:
            pnl_point = self.state.last_price - self.state.avg_price
            pnl_pct = (pnl_point / self.state.avg_price) * 100
            pnl_color = "green" if pnl_pct >= 0 else "red"
            position_text.append(f"Position P&L: ", style="bold")
            position_text.append(f"{pnl_point:+,.0f}₩ ({pnl_pct:+.2f}%)\n", style=pnl_color)
        
        return Panel(position_text, title="[bold cyan]Position[/bold cyan]", border_style="cyan")

    def _build_pnl_panel(self) -> Panel:
        """Build P&L summary panel."""
        pnl_text = Text()
        pnl_text.append(f"Equity: ", style="bold")
        pnl_text.append(f"₩{self.state.equity:,.0f}\n", style="cyan")
        
        pnl_text.append(f"Cash: ", style="bold")
        pnl_text.append(f"₩{self.state.cash_balance:,.0f}\n", style="cyan")
        
        pnl_text.append(f"Unrealized P&L: ", style="bold")
        pnl_text.append(f"₩{self.state.unrealized_pnl:,.0f}\n", style=("green" if self.state.unrealized_pnl >= 0 else "red"))
        
        pnl_text.append(f"Realized P&L: ", style="bold")
        pnl_text.append(f"₩{self.state.realized_pnl:,.0f}\n", style=("green" if self.state.realized_pnl >= 0 else "red"))
        
        pnl_text.append(f"Total P&L: ", style="bold")
        pnl_text.append(f"₩{self.state.total_pnl:,.0f}\n", style=("green bold" if self.state.total_pnl >= 0 else "red bold"))
        
        pnl_text.append(f"Return: ", style="bold")
        pnl_text.append(f"{self.state.total_return_pct:+.2f}%", style=("green" if self.state.total_return_pct >= 0 else "red"))
        
        return Panel(pnl_text, title="[bold cyan]P&L Summary[/bold cyan]", border_style="cyan")

    def _build_account_panel(self) -> Panel:
        """Build broker account panel with number, balances, and holdings."""
        snapshot = self.state.broker_account_snapshot if isinstance(self.state.broker_account_snapshot, dict) else {}

        account_no = str(snapshot.get("account_no") or "-").strip() or "-"
        source = str(snapshot.get("source") or "-").strip() or "-"
        updated_at = str(snapshot.get("updated_at") or "-").strip() or "-"

        cash = float(snapshot.get("cash_balance", self.state.cash_balance) or 0.0)
        equity = float(snapshot.get("equity", self.state.equity) or 0.0)
        total_pnl = float(snapshot.get("total_pnl", self.state.total_pnl) or 0.0)
        total_return = float(snapshot.get("total_return_pct", self.state.total_return_pct) or 0.0)
        active_positions = int(float(snapshot.get("active_positions", self.state.active_positions) or 0))
        position_qty = int(float(snapshot.get("position_qty", self.state.position_qty) or 0))

        account_text = Text()
        account_text.append("Account: ", style="bold")
        account_text.append(f"{account_no}\n", style="cyan")
        account_text.append("Source: ", style="bold")
        account_text.append(f"{source} | Updated: {updated_at}\n", style="dim")

        account_text.append("Cash: ", style="bold")
        account_text.append(f"₩{cash:,.0f}\n", style="cyan")
        account_text.append("Equity: ", style="bold")
        account_text.append(f"₩{equity:,.0f}\n", style="cyan")

        pnl_style = "green" if total_pnl >= 0 else "red"
        ret_style = "green" if total_return >= 0 else "red"
        account_text.append("Total P&L: ", style="bold")
        account_text.append(f"₩{total_pnl:+,.0f}\n", style=pnl_style)
        account_text.append("Return: ", style="bold")
        account_text.append(f"{total_return:+.2f}%\n", style=ret_style)

        account_text.append("Holdings: ", style="bold")
        account_text.append(f"{active_positions} symbols / {position_qty:,} shares\n", style="yellow")

        positions = snapshot.get("positions") if isinstance(snapshot.get("positions"), list) else []
        if positions:
            show_rows = [row for row in positions if isinstance(row, dict)][:4]
            for row in show_rows:
                sym = str(row.get("symbol") or "-")
                qty = int(float(row.get("qty", 0) or 0))
                avg = float(row.get("avg_price", 0.0) or 0.0)
                ret = float(row.get("return_pct", 0.0) or 0.0)
                row_style = "green" if ret >= 0 else "red"
                account_text.append(f"- {sym} {qty:,} @ ₩{avg:,.0f} ", style="white")
                account_text.append(f"({ret:+.2f}%)\n", style=row_style)
            if len(positions) > len(show_rows):
                account_text.append(f"... +{len(positions) - len(show_rows)} more\n", style="dim")
        else:
            pos_summary = str(snapshot.get("positions_summary") or self.state.positions_summary or "-").strip()
            account_text.append(f"{pos_summary}\n", style="dim")

        cash_error = str(snapshot.get("cash_error") or "").strip()
        holdings_error = str(snapshot.get("holdings_error") or "").strip()
        if cash_error or holdings_error:
            account_text.append("API Notice: ", style="bold red")
            account_text.append(f"cash={cash_error or '-'} | holdings={holdings_error or '-'}", style="red")

        return Panel(account_text, title="[bold cyan]Account[/bold cyan]", border_style="cyan")

    def _build_performance_panel(self) -> Panel:
        """Build runtime performance panel from bot perf profile."""
        perf = self.state.perf_profile if isinstance(self.state.perf_profile, dict) else {}

        loop_total_sec = float(perf.get("loop_total_sec", 0.0) or 0.0)
        if loop_total_sec > 0:
            self._loop_time_samples.append(loop_total_sec)

        loop_ms = loop_total_sec * 1000.0
        samples = sorted(self._loop_time_samples)
        p50_ms = 0.0
        p95_ms = 0.0
        if samples:
            p50_ms = samples[len(samples) // 2] * 1000.0
            p95_ms = samples[min(len(samples) - 1, int(len(samples) * 0.95))] * 1000.0

        runtime_p50_ms = float(perf.get("loop_p50_ms", 0.0) or 0.0)
        runtime_p95_ms = float(perf.get("loop_p95_ms", 0.0) or 0.0)
        if runtime_p50_ms > 0 and runtime_p95_ms > 0:
            p50_ms = runtime_p50_ms
            p95_ms = runtime_p95_ms

        perf_text = Text()
        perf_text.append("Loop Time: ", style="bold")
        loop_style = "green" if loop_ms <= 400 else "yellow" if loop_ms <= 1200 else "red"
        perf_text.append(f"{loop_ms:.1f} ms\n", style=loop_style)

        perf_text.append("P50/P95: ", style="bold")
        perf_text.append(f"{p50_ms:.1f} / {p95_ms:.1f} ms\n", style="cyan")

        candidate_refresh_ms = float(perf.get("candidate_refresh_sec", 0.0) or 0.0) * 1000.0
        selection_ms = float(perf.get("selection_sec", 0.0) or 0.0) * 1000.0
        quote_fetch_ms = float(perf.get("quote_fetch_sec", 0.0) or 0.0) * 1000.0
        decision_eval_ms = float(perf.get("decision_eval_sec", 0.0) or 0.0) * 1000.0

        perf_text.append("Candidate Refresh: ", style="bold")
        perf_text.append(f"{candidate_refresh_ms:.1f} ms\n", style="white")
        perf_text.append("Selection: ", style="bold")
        perf_text.append(f"{selection_ms:.1f} ms\n", style="white")
        perf_text.append("Quote Fetch: ", style="bold")
        perf_text.append(f"{quote_fetch_ms:.1f} ms\n", style="white")
        perf_text.append("Decision Eval: ", style="bold")
        perf_text.append(f"{decision_eval_ms:.1f} ms\n", style="white")

        perf_text.append("Watch/Candidates: ", style="bold")
        perf_text.append(
            f"{int(float(perf.get('watch_symbol_count', 0.0) or 0.0))} / "
            f"{int(float(perf.get('candidate_pool_count', 0.0) or 0.0))}\n",
            style="magenta",
        )

        perf_text.append("Cache Size: ", style="bold")
        perf_text.append(
            f"{int(float(perf.get('daily_analysis_cache_size', 0.0) or 0.0))}",
            style="yellow",
        )

        return Panel(perf_text, title="[bold cyan]Runtime Performance[/bold cyan]", border_style="cyan")

    def _performance_health_badge(self) -> Text:
        """Return compact runtime performance health badge for header."""
        perf = self.state.perf_profile if isinstance(self.state.perf_profile, dict) else {}
        loop_ms = float(perf.get("loop_total_sec", 0.0) or 0.0) * 1000.0
        p95_ms = float(perf.get("loop_p95_ms", 0.0) or 0.0)

        if p95_ms <= 0:
            p95_ms = loop_ms

        good_ms = float(self._perf_badge_good_ms)
        warn_ms = float(self._perf_badge_warn_ms)

        if p95_ms <= good_ms and loop_ms <= good_ms:
            label = "GOOD"
            style = "black on green"
        elif p95_ms <= warn_ms and loop_ms <= warn_ms:
            label = "WARN"
            style = "black on yellow"
        else:
            label = "HOT"
            style = "white on red"

        return Text(f" PERF {label} ", style=style)

    def _build_orders_table(self) -> Table:
        """Build recent orders/trades table."""
        table = Table(title="Recent Orders", show_header=True, header_style="bold magenta")
        table.add_column("Time", style="dim")
        table.add_column("Type", style="cyan")
        table.add_column("Symbol", style="cyan")
        table.add_column("Qty", justify="right", style="cyan")
        table.add_column("Price", justify="right", style="cyan")
        table.add_column("Amount", justify="right", style="cyan")
        table.add_column("Status", style="cyan")
        
        # Get recent orders from order_journal
        recent_orders = self.state.order_journal[-10:] if self.state.order_journal else []
        for order in reversed(recent_orders):
            time_str = str(order.get("timestamp", ""))[-8:]
            order_type = str(order.get("type", "")).upper()
            symbol = str(order.get("symbol", ""))
            qty = str(int(order.get("qty", 0)))
            price = f"₩{float(order.get('price', 0)):,.0f}"
            amount = f"₩{float(order.get('amount', 0)):,.0f}"
            status = str(order.get("status", "")).upper()
            
            # Color code status
            status_style = "green" if status == "FILLED" else "yellow" if status == "PENDING" else "red"
            
            table.add_row(
                time_str,
                order_type,
                symbol,
                qty,
                price,
                amount,
                Text(status, style=status_style),
            )
        
        return table

    def _build_events_panel(self) -> Panel:
        """Build recent events panel."""
        # Capture new events
        current_events = list(self.state.events)
        if len(current_events) > self.last_event_count:
            new_events = current_events[self.last_event_count:]
            self._recent_events.extend(new_events)
            self.last_event_count = len(current_events)
        
        if not self._recent_events:
            return Panel(
                Text("Waiting for events...", style="dim"),
                title="[bold cyan]Recent Events[/bold cyan]",
                border_style="cyan"
            )
        
        events_text = Text()
        for event in self._recent_events:
            # Color code events
            if "ORDER RESULT" in event:
                style = "magenta"
            elif "RISK_EXIT" in event or "HALT" in event:
                style = "red"
            elif "SELL" in event or "EXIT" in event:
                style = "yellow"
            elif "BUY" in event or "ENTRY" in event:
                style = "green"
            elif "ERROR" in event or "error" in event:
                style = "bright_red"
            else:
                style = "white"
            
            events_text.append(event + "\n", style=style)
        
        return Panel(events_text, title="[bold cyan]Recent Events[/bold cyan]", border_style="cyan")

    def _build_layout(self) -> Layout:
        """Build dashboard layout."""
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="main"),
            Layout(name="footer", size=2),
        )
        
        # Header
        title_text = Text("AITRADER - Real-time Trading Dashboard", style="bold cyan", justify="center")
        title_text.append(" ")
        title_text.append_text(self._performance_health_badge())
        timestamp = Text(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), style="dim", justify="center")
        header_panel = Panel(f"{title_text}\n{timestamp}", border_style="blue")
        layout["header"].update(header_panel)
        
        # Main content - split into left and right columns
        left_panel = Layout(name="left")
        left_panel.split_column(
            Layout(name="status"),
            Layout(name="market_status"),
            Layout(name="position"),
        )
        
        right_panel = Layout(name="right")
        right_panel.split_column(
            Layout(name="pnl"),
            Layout(name="account"),
            Layout(name="performance"),
            Layout(name="events"),
        )
        
        layout["main"].split_row(left_panel, right_panel)
        
        layout["status"].update(self._build_status_panel())
        layout["market_status"].update(self._build_market_status_panel())
        layout["position"].update(self._build_position_panel())
        layout["pnl"].update(self._build_pnl_panel())
        layout["account"].update(self._build_account_panel())
        layout["performance"].update(self._build_performance_panel())
        layout["events"].update(self._build_events_panel())
        
        # Footer
        footer_text = Text("Press Ctrl+C to exit | Updating every {}s".format(self.update_interval), style="dim", justify="center")
        layout["footer"].update(footer_text)
        
        return layout

    def start(self) -> None:
        """Start the dashboard."""
        if self.running:
            return
        
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.console.print("[green]✓[/green] Dashboard started")

    def stop(self) -> None:
        """Stop the dashboard."""
        self.running = False
        if self.thread:
            self.thread.join(timeout=2)
        self.console.print("[yellow]✓[/yellow] Dashboard stopped")

    def _run(self) -> None:
        """Main dashboard loop."""
        try:
            with Live(self._build_layout(), console=self.console, refresh_per_second=1/self.update_interval, screen=True) as live:
                while self.running:
                    try:
                        live.update(self._build_layout())
                        time.sleep(self.update_interval)
                    except KeyboardInterrupt:
                        self.running = False
                        break
                    except Exception as e:
                        self.console.print(f"[red]Dashboard update error: {e}[/red]")
                        time.sleep(self.update_interval)
        except Exception as e:
            self.console.print(f"[red]Dashboard error: {e}[/red]")
            self.running = False

    def display_summary(self) -> None:
        """Display a static summary instead of live updates."""
        try:
            console = Console()
            layout = self._build_layout()
            console.print(layout)
        except Exception as e:
            self.console.print(f"[red]Error displaying summary: {e}[/red]")


def create_dashboard(state: "BotState", update_interval: float = 1.0) -> CLIDashboard:
    """Factory function to create a CLI dashboard.
    
    Args:
        state: BotState object from the running bot
        update_interval: How often to update display (seconds)
    
    Returns:
        CLIDashboard instance
    
    Raises:
        ImportError: If rich library is not installed
    """
    return CLIDashboard(state, update_interval)
