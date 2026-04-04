#!/usr/bin/env python3
"""
Professional Scalping Scheduler - Weekday Stock Selection & Trading Automation

Workflow:
- 08:30 AM: Pre-market validation (simulate yesterday on top 5 stocks)
- 09:00 AM: Market open (select top 5 from current intraday data)
- Every 2 hours: Refresh top 5 stocks (capture changing liquidity)
- 15:30 PM: Market close (summarize daily results)

Timezone: KST (UTC+9) - Korea Stock Exchange
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional

import pytz
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

sys.path.insert(0, str(Path(__file__).parent / "src"))

from scalping_data_loader import get_day_price_data, get_today_scalping_data
from scalping_strategy import ScalpParams, calculate_scalp_metrics, scalp_entry_signal, scalp_exit_signal
from intraday_stock_selector import get_best_scalping_stocks, display_scalping_stocks

# Try to import APScheduler for scheduling
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    HAS_SCHEDULER = True
except ImportError:
    HAS_SCHEDULER = False
    print("⚠️  APScheduler not installed. Run: pip install apscheduler")


class ScalpingScheduler:
    """Automated weekday stock selection and scalping execution."""
    
    def __init__(self, timezone_str: str = "Asia/Seoul"):
        """Initialize scheduler with KST timezone."""
        self.tz = pytz.timezone(timezone_str)
        self.scheduler = BackgroundScheduler(timezone=timezone_str) if HAS_SCHEDULER else None
        
        # State tracking
        self.selected_stocks: List[str] = []
        self.daily_results: Dict = {
            "date": self._get_today_str(),
            "sessions": []
        }
        self.current_session = 0
        self.last_selection_time: Optional[datetime] = None
        
        # Configuration
        bar_interval = max(1, int(os.getenv("BAR_INTERVAL_MINUTES", "2")))
        reselect_minutes = max(1, int(os.getenv("INTRADAY_RESELECT_MINUTES", "120")))
        self.config = {
            "min_win_rate": 55.0,        # Min win rate from validation to trade
            "max_daily_loss": -50000,    # Stop loss for day (-₩50k)
            "session_interval": max(1, reselect_minutes // 60),  # Re-select interval in hours
            "top_n_stocks": 5,          # Trade top 5 stocks
            "bar_interval": bar_interval,  # Match bot_runtime bar interval default
        }
        
        # Slack configuration
        self.slack_config = {
            "webhook_url": os.getenv("SLACK_WEBHOOK_URL", ""),
            "channel": os.getenv("SLACK_CHANNEL", "#trading-reports"),
            "enabled": bool(os.getenv("SLACK_WEBHOOK_URL", "")),
            "send_hourly_reports": True,
            "send_daily_summary": True,
            "send_validation_reports": True,
        }

        # Market analysis agent hook configuration
        self.analysis_config = {
            "enabled": str(os.getenv("MARKET_ANALYSIS_ENABLED", "true")).strip().lower() in {"1", "true", "yes", "on"},
            "command": str(os.getenv("MARKET_ANALYSIS_CMD", "")).strip(),
            "signal_path": str(os.getenv("MARKET_ANALYSIS_SIGNAL_PATH", "data/market_analysis_signal.json")).strip(),
            "max_age_minutes": max(5, int(os.getenv("MARKET_ANALYSIS_MAX_AGE_MINUTES", "240"))),
        }
        
        # Initialize Slack client if webhook is configured
        self.slack_client = None
        if self.slack_config["enabled"]:
            try:
                from slack_sdk import WebhookClient
                self.slack_client = WebhookClient(self.slack_config["webhook_url"])
                print("✅ Slack integration enabled")
                print(f"   Channel: {self.slack_config['channel']}")
            except ImportError:
                print("⚠️  Slack SDK not available. Run: pip install slack-sdk")
                self.slack_config["enabled"] = False
        else:
            print("ℹ️  Slack integration disabled (no SLACK_WEBHOOK_URL set)")
        
        print("✅ Scalping Scheduler initialized")
        print(f"   Timezone: {timezone_str}")
        print(f"   Min Win Rate: {self.config['min_win_rate']}%")
        print(f"   Top N Stocks: {self.config['top_n_stocks']}")
        print(f"   Slack: {'Enabled' if self.slack_config['enabled'] else 'Disabled'}")
        print(f"   Market Analysis: {'Enabled' if self.analysis_config['enabled'] else 'Disabled'}")
    
    def _get_now(self) -> datetime:
        """Get current time in scheduler timezone."""
        return datetime.now(self.tz)
    
    def _get_today_str(self) -> str:
        """Get today's date as YYYY-MM-DD."""
        return self._get_now().strftime("%Y-%m-%d")
    
    def _get_next_session_time(self) -> str:
        """Get the next scheduled session time as HH:MM KST."""
        now = self._get_now()
        hour = now.hour
        
        # Session times: 11:00, 13:00, 15:00
        if hour < 11:
            return "11:00 KST"
        elif hour < 13:
            return "13:00 KST"
        elif hour < 15:
            return "15:00 KST"
        else:
            return "Tomorrow 09:00 KST"

    def run_market_analysis(self, trigger: str = "stock_selection") -> Dict:
        """Run external market analysis agent and load its latest signal output."""
        result = {
            "preferred_symbols": [],
            "summary": "",
            "source": "disabled",
            "fresh": False,
        }

        if not self.analysis_config["enabled"]:
            return result

        cmd = self.analysis_config["command"]
        signal_path = Path(self.analysis_config["signal_path"])
        max_age = timedelta(minutes=int(self.analysis_config["max_age_minutes"]))

        if cmd:
            try:
                proc = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                if proc.returncode == 0:
                    print(f"✅ Market analysis completed ({trigger})")
                else:
                    print(f"⚠️  Market analysis command failed ({trigger}): rc={proc.returncode}")
            except Exception as e:
                print(f"⚠️  Market analysis command error ({trigger}): {e}")

        if not signal_path.exists():
            return result

        try:
            raw = json.loads(signal_path.read_text())
            preferred = (
                raw.get("preferred_symbols")
                or raw.get("popular_symbols")
                or raw.get("interesting_symbols")
                or raw.get("selected_symbols")
                or []
            )
            preferred_symbols = [str(s).strip() for s in list(preferred) if str(s).strip()]

            summary = str(raw.get("summary") or raw.get("market_summary") or raw.get("analysis") or "").strip()

            generated_at_raw = str(raw.get("generated_at") or raw.get("updated_at") or "").strip()
            fresh = False
            if generated_at_raw:
                try:
                    generated_at = datetime.fromisoformat(generated_at_raw.replace("Z", "+00:00"))
                    now = datetime.now(generated_at.tzinfo) if generated_at.tzinfo else datetime.now()
                    fresh = (now - generated_at) <= max_age
                except Exception:
                    fresh = False

            if preferred_symbols:
                print(f"🔎 Market analysis preferred symbols: {', '.join(preferred_symbols[:10])}")
            if summary:
                print(f"📰 Market analysis summary: {summary[:140]}")
            if generated_at_raw and not fresh:
                print(f"⚠️  Market analysis signal is stale: {generated_at_raw}")

            result.update(
                {
                    "preferred_symbols": preferred_symbols,
                    "summary": summary,
                    "source": str(signal_path),
                    "fresh": fresh,
                }
            )
            return result
        except Exception as e:
            print(f"⚠️  Failed to read market analysis signal: {e}")
            return result
    
    def is_market_hours(self) -> bool:
        """Check if current time is during KRX market hours (9:00 AM - 3:30 PM)."""
        now = self._get_now()
        hour = now.hour
        minute = now.minute
        weekday = now.weekday()  # 0=Mon, 4=Fri, 5=Sat, 6=Sun
        
        # Only weekdays
        if weekday >= 5:
            return False
        
        # 09:00 to 15:30
        if hour == 9 and minute >= 0:
            return True
        if 10 <= hour < 15:
            return True
        if hour == 15 and minute <= 30:
            return True
        
        return False
    
    def is_before_market_open(self) -> bool:
        """Check if before market open (good for pre-market validation)."""
        now = self._get_now()
        hour = now.hour
        weekday = now.weekday()
        
        if weekday >= 5:
            return False
        
        return 8 <= hour < 9
    
    def send_slack_message(self, message: str, title: str = "", color: str = "good"):
        """
        Send a message to Slack if configured.
        
        Args:
            message: The message text
            title: Optional title for the message
            color: Color for Slack attachment (good, warning, danger)
        """
        if not self.slack_config["enabled"] or not self.slack_client:
            return
        
        # Create attachment for better formatting
        attachment = {
            "color": color,
            "text": message,
            "footer": "Scalping Scheduler",
            "ts": datetime.now().timestamp()
        }
        
        if title:
            attachment["title"] = title

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                response = self.slack_client.send(
                    text=f"📊 {title}" if title else "📊 Scalping Report",
                    attachments=[attachment]
                )

                status_code = int(getattr(response, "status_code", 0) or 0)
                body = str(getattr(response, "body", "")).strip()

                if status_code == 200:
                    print(f"✅ Slack message sent: {title or 'Scalping Report'}")
                    return

                # Retry transient failures (rate-limit or server-side errors)
                if status_code == 429 or 500 <= status_code < 600:
                    if attempt < max_retries:
                        wait_sec = 1.0 * attempt
                        print(
                            f"⚠️  Slack temporary error ({status_code}) attempt {attempt}/{max_retries}, retrying in {wait_sec:.1f}s"
                        )
                        time.sleep(wait_sec)
                        continue

                print(
                    f"❌ Slack message failed: status={status_code}, body={body or '-'}"
                )
                return

            except Exception as e:
                if attempt < max_retries:
                    wait_sec = 1.0 * attempt
                    print(
                        f"⚠️  Slack error on attempt {attempt}/{max_retries}: {e}; retrying in {wait_sec:.1f}s"
                    )
                    time.sleep(wait_sec)
                    continue
                print(f"❌ Slack error: {e}")
                return
    
    def format_hourly_report(self, session_results: Dict, session_num: int) -> str:
        """
        Format session results for Slack hourly report.
        
        Args:
            session_results: Session results dictionary
            session_num: Session number
            
        Returns:
            Formatted Slack message
        """
        timestamp = session_results.get("timestamp", "")
        total_pnl = session_results.get("total_pnl", 0)
        stocks = session_results.get("stocks", [])
        
        # Format timestamp
        if timestamp:
            try:
                dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                time_str = dt.strftime("%H:%M KST")
            except:
                time_str = timestamp[:16]
        else:
            time_str = self._get_now().strftime("%H:%M")
        
        # Build message
        message = f"*Session #{session_num} Report* ({time_str})\n\n"
        
        # Summary
        successful_stocks = len([s for s in stocks if s.get("pnl", 0) > 0])
        message += f"📊 *Summary:*\n"
        message += f"• Stocks traded: {len(stocks)}\n"
        message += f"• Successful: {successful_stocks}\n"
        message += f"• Session P&L: ₩{total_pnl:+,.0f}\n\n"
        
        # Individual stock results
        if stocks:
            message += f"📈 *Stock Performance:*\n"
            for stock in stocks:
                symbol = stock.get("symbol", "???")
                trades = stock.get("trades", 0)
                win_rate = stock.get("win_rate", 0)
                pnl = stock.get("pnl", 0)
                
                status = "✅" if pnl > 0 else "⚠️" if trades > 0 else "🔍"
                message += f"• {status} {symbol}: {trades} trades, {win_rate:.1f}% WR, ₩{pnl:+,.0f}\n"
        
        return message
    
    def send_stock_selection_notification(self, selected_stocks: List[Dict], analysis_summary: str = ""):
        """
        Send Slack notification when new stocks are selected.
        
        Args:
            selected_stocks: List of selected stock dictionaries
        """
        if not self.slack_config["enabled"]:
            return
            
        try:
            # Format the message
            timestamp = self._get_now().strftime("%H:%M KST")
            message = f"*🔄 Stock Selection Update* ({timestamp})\n\n"
            message += f"📊 *Top {len(selected_stocks)} Stocks Selected:*\n"
            
            for i, stock in enumerate(selected_stocks, 1):
                symbol = stock.get("symbol", "???")
                score = stock.get("score", 0)
                price_range = stock.get("price_range_pct", 0)
                volume_spike = stock.get("volume_spike", 0)
                rsi = stock.get("rsi", 0)
                
                message += f"• #{i} {symbol}: Score {score:.1f}, Range {price_range:.1f}%, Vol {volume_spike:.1f}x, RSI {rsi:.0f}\n"
            
            if analysis_summary:
                message += f"\n🧠 *Market Analysis:* {analysis_summary[:180]}"

            message += f"\n💡 *Next session:* {self._get_next_session_time()}"
            
            # Send to Slack
            self.send_slack_message(
                message=message,
                title="Stock Selection Update",
                color="good"
            )
            
        except Exception as e:
            print(f"⚠️  Failed to send stock selection notification: {e}")
    
    def run_pre_market_validation(self):
        """
        Run simulations on yesterday's top 5 stocks to validate strategy.
        Executed at 08:30 AM before market open.
        """
        print("\n" + "=" * 100)
        print("📋 PRE-MARKET VALIDATION (08:30 AM)")
        print("=" * 100)
        
        yesterday = (self._get_now() - timedelta(days=1)).strftime("%Y-%m-%d")
        candidate_dates = [
            (self._get_now() - timedelta(days=offset)).strftime("%Y-%m-%d")
            for offset in range(1, 8)
        ]
        
        # Get yesterday's top stocks (or use hardcoded if not available)
        try:
            best_stocks = get_best_scalping_stocks(limit=self.config["top_n_stocks"])
            symbols = [s["symbol"] for s in best_stocks]
            print(f"\n📊 Testing yesterday ({yesterday}) with today's top {len(symbols)} stocks:")
            for i, s in enumerate(best_stocks, 1):
                print(f"   {i}. {s['symbol']} - Score: {s['score']:.1f}")
        except Exception as e:
            print(f"⚠️  Could not load stock selection: {e}")
            symbols = ["005930", "000660", "035720", "051910", "247540"]  # Default
        
        print()
        
        # Run simulations
        all_results = []
        for symbol in symbols:
            try:
                results = None
                used_date = None

                for target_date in candidate_dates:
                    trial = self._run_single_simulation(symbol, target_date)
                    if trial.get("success"):
                        results = trial
                        used_date = target_date
                        break

                if results and results.get("success"):
                    all_results.append(results)
                    status = "✅" if results["win_rate"] >= self.config["min_win_rate"] else "❌"
                    date_note = f" [{used_date}]" if used_date and used_date != yesterday else ""
                    print(f"{status} {symbol}{date_note}: {results['trade_count']} trades, "
                          f"Win Rate: {results['win_rate']:.1f}%, "
                          f"P&L: ₩{results['pnl']:+,.0f}")
            except Exception as e:
                print(f"❌ {symbol}: {e}")
        
        # Validation summary
        print("\n" + "─" * 100)
        if all_results:
            avg_win_rate = sum(r["win_rate"] for r in all_results) / len(all_results)
            total_pnl = sum(r["pnl"] for r in all_results)
            
            print(f"📊 VALIDATION SUMMARY:")
            print(f"   Simulations: {len(all_results)}/{len(symbols)}")
            print(f"   Avg Win Rate: {avg_win_rate:.1f}% {'✅' if avg_win_rate >= self.config['min_win_rate'] else '⚠️'}")
            print(f"   Total P&L: ₩{total_pnl:+,.0f}")
            
            if avg_win_rate >= self.config["min_win_rate"]:
                print(f"\n✅ Strategy VALIDATED! Ready to trade today.")
                
                # Send Slack validation success
                if self.slack_config["send_validation_reports"]:
                    validation_msg = f"✅ *Strategy Validated*\n\nPre-market validation passed!\n• Win Rate: {avg_win_rate:.1f}%\n• Threshold: {self.config['min_win_rate']}%\n• Total P&L: ₩{total_pnl:+,.0f}\n\n🚀 Ready to trade today!"
                    self.send_slack_message(validation_msg, "Strategy Validated", "good")
                
                return True
            else:
                print(f"\n⚠️  Win rate {avg_win_rate:.1f}% below threshold {self.config['min_win_rate']}%")
                
                # Send Slack validation failure
                if self.slack_config["send_validation_reports"]:
                    validation_msg = f"⚠️ *Strategy Validation Failed*\n\nWin rate below threshold\n• Actual: {avg_win_rate:.1f}%\n• Required: {self.config['min_win_rate']}%\n• Total P&L: ₩{total_pnl:+,.0f}\n\n❌ Trading cancelled for today"
                    self.send_slack_message(validation_msg, "Strategy Validation Failed", "warning")
                
                return False
        else:
            print("❌ No successful simulations. Check data availability.")
            
            # Send Slack error notification
            if self.slack_config["send_validation_reports"]:
                error_msg = f"❌ *Validation Error*\n\nNo successful simulations\n• Check data availability\n• Verify stock selection\n\n❌ Trading cancelled for today"
                self.send_slack_message(error_msg, "Validation Error", "danger")
            
            return False
    
    def select_best_stocks(self) -> List[Dict]:
        """
        Select best stocks for scalping based on current intraday data.
        Called every 2 hours during market hours.
        """
        print("\n" + "─" * 100)
        print(f"🔄 STOCK SELECTION UPDATE ({self._get_now().strftime('%H:%M:%S')})")
        print("─" * 100)
        
        try:
            analysis = self.run_market_analysis(trigger="stock_selection")
            best_stocks = get_best_scalping_stocks(limit=self.config["top_n_stocks"])

            preferred = [s for s in analysis.get("preferred_symbols", []) if s]
            if preferred and best_stocks:
                preferred_rank = {sym: idx for idx, sym in enumerate(preferred)}
                best_stocks.sort(
                    key=lambda row: (
                        preferred_rank.get(str(row.get("symbol", "")).strip(), 10_000),
                        -float(row.get("score", 0.0)),
                    )
                )
                for idx, row in enumerate(best_stocks, 1):
                    row["rank"] = idx
            
            print(f"\n📊 Top {len(best_stocks)} Stocks for Scalping:")
            print(f"{'Rank':<6} {'Symbol':<10} {'Score':<10} {'Range':<12} {'Volume':<10} {'RSI':<8}")
            print("─" * 60)
            
            for stock in best_stocks:
                print(f"{stock.get('rank', '?'):<6} "
                      f"{stock['symbol']:<10} "
                      f"{stock['score']:.1f}/<100  "
                      f"{stock.get('price_range_pct', 0):.2f}%  "
                      f"{stock.get('volume_spike', 0):.1f}x  "
                      f"{stock.get('rsi', 0):.0f}")
            
            self.selected_stocks = [s["symbol"] for s in best_stocks]
            self.last_selection_time = self._get_now()
            
            # Send Slack notification for stock selection
            if self.slack_config["enabled"] and best_stocks:
                self.send_stock_selection_notification(best_stocks, analysis_summary=analysis.get("summary", ""))
            
            return best_stocks
            
        except Exception as e:
            print(f"❌ Error selecting stocks: {e}")
            return []
    
    def run_intraday_scalping_session(self, session_num: int = 1):
        """
        Run scalping simulations on currently selected stocks.
        Called multiple times during the day.
        
        In live mode, this would execute real trades.
        In simulation mode, this validates the strategy.
        """
        print("\n" + "=" * 100)
        print(f"🚀 SCALPING SESSION #{session_num} ({self._get_now().strftime('%H:%M:%S')})")
        print("=" * 100)
        
        today = self._get_today_str()

        # Before each session, refresh selection (with market analysis) unless just selected.
        now = self._get_now()
        if (
            self.last_selection_time is None
            or (now - self.last_selection_time).total_seconds() > 60
        ):
            self.select_best_stocks()
        
        if not self.selected_stocks:
            print("⚠️  No stocks selected. Running selection...")
            self.select_best_stocks()
        
        if not self.selected_stocks:
            print("❌ Unable to select stocks. Skipping session.")
            return
        
        # Run simulations on selected stocks
        session_results = {
            "session_num": session_num,
            "timestamp": self._get_now().isoformat(),
            "stocks": []
        }
        
        total_session_pnl = 0
        successful_trades = 0
        
        for symbol in self.selected_stocks:
            try:
                results = self._run_single_simulation(symbol, today, show_details=False)
                
                if results.get("success"):
                    session_results["stocks"].append({
                        "symbol": symbol,
                        "trades": results["trade_count"],
                        "win_rate": results["win_rate"],
                        "pnl": results["pnl"],
                        "hold_minutes": results.get("hold_minutes", 0)
                    })
                    
                    total_session_pnl += results["pnl"]
                    successful_trades += 1
                    
                    status = "✅" if results["pnl"] > 0 else "⚠️" if results["trade_count"] > 0 else "🔍"
                    print(f"{status} {symbol}: {results['trade_count']} trades, "
                          f"WR: {results['win_rate']:.1f}%, "
                          f"PnL: ₩{results['pnl']:+,.0f}")
            except Exception as e:
                print(f"❌ {symbol}: {e}")
        
        # Check daily loss limit
        session_results["total_pnl"] = total_session_pnl
        self.daily_results["sessions"].append(session_results)
        
        total_daily_pnl = sum(s.get("total_pnl", 0) for s in self.daily_results["sessions"])
        
        print("\n" + "─" * 100)
        print(f"📊 SESSION #{session_num} SUMMARY:")
        print(f"   Successful Stocks: {successful_trades}/{len(self.selected_stocks)}")
        print(f"   Session P&L: ₩{total_session_pnl:+,.0f}")
        print(f"   Daily P&L: ₩{total_daily_pnl:+,.0f}")
        
        if total_daily_pnl <= self.config["max_daily_loss"]:
            print(f"\n🛑 STOP LOSS HIT! Daily loss ₩{total_daily_pnl} exceeds "
                  f"limit ₩{self.config['max_daily_loss']}. Halting trading.")
            
            # Send emergency Slack alert
            if self.slack_config["send_hourly_reports"]:
                emergency_msg = f"🚨 *EMERGENCY STOP*\n\nDaily loss limit hit!\n• Loss: ₩{total_daily_pnl:,}\n• Limit: ₩{self.config['max_daily_loss']:,}\n• Trading halted for today"
                self.send_slack_message(emergency_msg, "🚨 Trading Stopped - Loss Limit", "danger")
            
            return "STOP"
        
        # Send hourly Slack report
        if self.slack_config["send_hourly_reports"]:
            hourly_report = self.format_hourly_report(session_results, session_num)
            color = "good" if total_session_pnl > 0 else "warning" if total_session_pnl == 0 else "danger"
            self.send_slack_message(hourly_report, f"Session #{session_num} Complete", color)
        
        return "CONTINUE"
    
    def run_market_close_summary(self):
        """
        Generate end-of-day summary.
        Executed at 15:30 PM after market close.
        """
        print("\n" + "=" * 100)
        print("📊 MARKET CLOSE SUMMARY (15:30 PM)")
        print("=" * 100)
        
        if not self.daily_results["sessions"]:
            print("⚠️  No sessions executed today.")
            return
        
        # Calculate daily stats
        total_pnl = sum(s.get("total_pnl", 0) for s in self.daily_results["sessions"])
        total_trades = sum(len(s.get("stocks", [])) * 5 for s in self.daily_results["sessions"])  # Rough estimate
        
        print(f"\n📅 Date: {self.daily_results['date']}")
        print(f"📊 Sessions: {len(self.daily_results['sessions'])}")
        print(f"💰 Daily P&L: ₩{total_pnl:+,.0f}")
        print(f"📈 Total Trades: ~{total_trades}")
        
        # Best/worst stocks
        print(f"\n🏆 Best Performers:")
        stocks_by_pnl = []
        for session in self.daily_results["sessions"]:
            for stock in session.get("stocks", []):
                pnl = stock.get("pnl", 0)
                if pnl > 0:
                    stocks_by_pnl.append(stock)
        
        stocks_by_pnl.sort(key=lambda x: x.get("pnl", 0), reverse=True)
        for i, stock in enumerate(stocks_by_pnl[:3], 1):
            print(f"   {i}. {stock['symbol']}: ₩{stock['pnl']:+,.0f} ({stock['win_rate']:.0f}% WR)")
        
        # Save daily report
        report_file = Path(f"data/daily_scalp_report_{self.daily_results['date']}.json")
        with open(report_file, "w") as f:
            json.dump(self.daily_results, f, indent=2)
        
        print(f"\n✅ Daily report saved: {report_file}")
        
        # Send daily summary to Slack
        if self.slack_config["send_daily_summary"]:
            daily_msg = f"*Daily Trading Summary* ({self.daily_results['date']})\n\n"
            daily_msg += f"📊 *Overview:*\n"
            daily_msg += f"• Sessions: {len(self.daily_results['sessions'])}\n"
            daily_msg += f"• Total P&L: ₩{total_pnl:+,.0f}\n"
            daily_msg += f"• Estimated Trades: ~{total_trades}\n\n"
            
            if stocks_by_pnl:
                daily_msg += f"🏆 *Top Performers:*\n"
                for i, stock in enumerate(stocks_by_pnl[:3], 1):
                    daily_msg += f"• {stock['symbol']}: ₩{stock['pnl']:+,.0f} ({stock['win_rate']:.0f}% WR)\n"
            
            color = "good" if total_pnl > 0 else "warning" if total_pnl == 0 else "danger"
            self.send_slack_message(daily_msg, f"Daily Summary: ₩{total_pnl:+,.0f}", color)
    
    def _run_single_simulation(
        self,
        symbol: str,
        date_str: str,
        show_details: bool = True
    ) -> Dict:
        """Run a single stock scalping simulation."""
        from scalp_sim import run_scalping_simulation
        
        return run_scalping_simulation(
            symbol=symbol,
            date_str=date_str,
            bar_interval=self.config["bar_interval"],
            show_details=show_details
        )
    
    def schedule_daily_jobs(self):
        """Setup APScheduler jobs for automated execution."""
        if not HAS_SCHEDULER:
            print("❌ APScheduler required for scheduling. Install: pip install apscheduler")
            return False
        
        if not self.scheduler:
            return False
        
        # Pre-market validation at 08:30 AM
        self.scheduler.add_job(
            self.run_pre_market_validation,
            CronTrigger(
                hour=8,
                minute=30,
                day_of_week="0-4",  # Mon-Fri
                timezone=str(self.tz)
            ),
            id="pre_market_validation",
            name="Pre-market Validation"
        )

        # Early morning market analysis refresh (before stock selection)
        self.scheduler.add_job(
            lambda: self.run_market_analysis(trigger="early_morning"),
            CronTrigger(
                hour=8,
                minute=20,
                day_of_week="0-4",  # Mon-Fri
                timezone=str(self.tz)
            ),
            id="early_morning_market_analysis",
            name="Early Morning Market Analysis"
        )
        
        # Select stocks at market open (09:00 AM)
        self.scheduler.add_job(
            self.select_best_stocks,
            CronTrigger(
                hour=9,
                minute=0,
                day_of_week="0-4",
                timezone=str(self.tz)
            ),
            id="market_open_selection",
            name="Market Open Stock Selection"
        )
        
        # Refresh stocks by configured session interval during market hours.
        interval_hours = max(1, int(self.config.get("session_interval", 2)))
        session_hours = list(range(9, 16, interval_hours))
        if 15 not in session_hours:
            session_hours.append(15)
        session_hours = sorted(set(h for h in session_hours if 9 <= h <= 15))

        for idx, hour in enumerate(session_hours, start=1):
            self.scheduler.add_job(
                lambda session_num=idx: self.run_intraday_scalping_session(session_num=session_num),
                CronTrigger(
                    hour=hour,
                    minute=0,
                    day_of_week="0-4",
                    timezone=str(self.tz)
                ),
                id=f"scalping_session_{hour}",
                name=f"Scalping Session {hour}:00"
            )
        
        # Market close summary at 15:30 PM
        self.scheduler.add_job(
            self.run_market_close_summary,
            CronTrigger(
                hour=15,
                minute=30,
                day_of_week="0-4",
                timezone=str(self.tz)
            ),
            id="market_close_summary",
            name="Market Close Summary"
        )
        
        print("\n✅ Scheduled jobs:")
        for job in self.scheduler.get_jobs():
            print(f"   • {job.name} (trigger: {job.trigger})")
        
        return True
    
    def start_scheduler(self):
        """Start the APScheduler background scheduler."""
        if not self.scheduler:
            print("❌ Scheduler not available. Install: pip install apscheduler")
            return False
        
        try:
            self.scheduler.start()
            print("\n🚀 Scheduler started! Running in background...")
            print("   Press Ctrl+C to stop")
            
            return True
        except Exception as e:
            print(f"❌ Failed to start scheduler: {e}")
            return False
    
    def run_manual_session(self, session_type: str = "full"):
        """
        Run a manual session without APScheduler.
        Useful for testing or manual execution.
        
        session_type: "pre_market", "select", "scalp", or "full"
        """
        print(f"\n🔄 Running manual session: {session_type}")
        
        if session_type in ["pre_market", "full"]:
            self.run_pre_market_validation()
        
        if session_type in ["select", "full"]:
            self.select_best_stocks()
        
        if session_type in ["scalp", "full"]:
            self.run_intraday_scalping_session()
        
        if session_type == "full":
            self.run_market_close_summary()


def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Professional scalping scheduler with stock selection automation"
    )
    parser.add_argument(
        "--start",
        action="store_true",
        help="Start scheduler (requires APScheduler)"
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Run pre-market validation test"
    )
    parser.add_argument(
        "--select",
        action="store_true",
        help="Select and display best stocks"
    )
    parser.add_argument(
        "--session",
        action="store_true",
        help="Run one intraday scalping session"
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Run full daily cycle (validate → select → session → summary)"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check market hours and scheduler status"
    )
    
    args = parser.parse_args()
    
    # Create scheduler
    scheduler = ScalpingScheduler()
    
    # Handle commands
    if args.start:
        scheduler.schedule_daily_jobs()
        scheduler.start_scheduler()
        # Keep running
        try:
            asyncio.run(asyncio.sleep(float('inf')))
        except KeyboardInterrupt:
            print("\n\n👋 Scheduler stopped")
    
    elif args.validate:
        scheduler.run_pre_market_validation()
    
    elif args.select:
        scheduler.select_best_stocks()
    
    elif args.session:
        scheduler.select_best_stocks()
        scheduler.run_intraday_scalping_session()
    
    elif args.full:
        scheduler.run_pre_market_validation()
        scheduler.select_best_stocks()
        scheduler.run_intraday_scalping_session()
        scheduler.run_market_close_summary()
    
    elif args.check:
        now = scheduler._get_now()
        print(f"\n📊 SCHEDULER STATUS CHECK")
        print(f"   Current time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        print(f"   Market hours: {scheduler.is_market_hours()}")
        print(f"   Pre-market: {scheduler.is_before_market_open()}")
        print(f"   APScheduler available: {HAS_SCHEDULER}")
        print(f"   Config: {scheduler.config}")
    
    else:
        print("Usage:")
        print("  --start    : Start 24/7 scheduler")
        print("  --validate : Run pre-market validation test")
        print("  --select   : Select best stocks")
        print("  --session  : Run one scalping session")
        print("  --full     : Run complete daily cycle")
        print("  --check    : Check market status")


if __name__ == "__main__":
    main()
