from __future__ import annotations

import argparse
import logging
import sys
import threading
from pathlib import Path

from bot_runtime import BotState, run_bot
from config import load_settings
from cli_dashboard import create_dashboard, RICH_AVAILABLE
from ai_company import run_ai_company


_log_path = Path("data/bot_runtime.log")
_log_path.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_log_path, encoding="utf-8"),
    ],
)


def run(
    enable_dashboard: bool = False,
    dashboard_interval: float = 1.0,
    *,
    ai_company_mode: bool = False,
    manager_report_minutes: int = 60,
    manager_cycle_seconds: int = 20,
    manager_report_path: str = "data/hourly_manager_reports.json",
    manager_slack_enabled: bool = False,
    manager_slack_webhook_url: str = "",
    manager_event_cooldown_seconds: int = 120,
) -> None:
    """Run the trading bot with optional CLI dashboard.
    
    Args:
        enable_dashboard: Enable real-time CLI dashboard
        dashboard_interval: Dashboard update interval in seconds
    """
    stop_event = threading.Event()
    state = BotState()
    
    # Setup dashboard if enabled
    dashboard = None
    if enable_dashboard:
        if not RICH_AVAILABLE:
            logging.warning("CLI dashboard disabled: 'rich' package not installed")
            logging.info("Install with: pip install rich")
        else:
            try:
                dashboard = create_dashboard(state, update_interval=dashboard_interval)
                dashboard.start()
                logging.info("✓ CLI dashboard started")
            except Exception as e:
                logging.error("Failed to start dashboard: %s", e)
    
    try:
        if ai_company_mode:
            run_ai_company(
                stop_event,
                state,
                report_interval_seconds=max(60, int(manager_report_minutes) * 60),
                cycle_seconds=max(5, int(manager_cycle_seconds)),
                report_path=manager_report_path,
                manager_slack_enabled=manager_slack_enabled,
                manager_slack_webhook_url=manager_slack_webhook_url,
                event_report_cooldown_seconds=max(10, int(manager_event_cooldown_seconds)),
            )
        else:
            run_bot(stop_event, state)
    except RuntimeError as exc:
        stop_event.set()
        logging.error("Bot start failed: %s", exc)
    except KeyboardInterrupt:
        stop_event.set()
        logging.info("Stopped by user.")
    except Exception as exc:
        stop_event.set()
        logging.error("Unexpected bot error: %s", exc)
    finally:
        if dashboard:
            dashboard.stop()

    # Surface silent startup failures (e.g., runtime lock contention) clearly.
    if (not state.running) and state.started_at is None:
        msg = state.last_error or "Bot exited before startup completed."
        raise RuntimeError(msg)


def main() -> None:
    """Main entry point with CLI argument parsing."""
    parser = argparse.ArgumentParser(
        description="AITRADER - Automated Trading Bot with Real-time Dashboard"
    )
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Enable real-time CLI dashboard for monitoring transactions"
    )
    parser.add_argument(
        "--update-interval",
        type=float,
        default=1.0,
        help="Dashboard update interval in seconds (default: 1.0)"
    )
    parser.add_argument(
        "--ai-company",
        action="store_true",
        help="Enable manager-led multi-agent orchestration with hourly reports"
    )
    parser.add_argument(
        "--manager-report-minutes",
        type=int,
        default=60,
        help="Manager report interval in minutes (default: 60)"
    )
    parser.add_argument(
        "--manager-cycle-seconds",
        type=int,
        default=20,
        help="Manager coordination cycle in seconds (default: 20)"
    )
    parser.add_argument(
        "--manager-report-path",
        default="data/hourly_manager_reports.json",
        help="Path to store manager reports JSON"
    )
    parser.add_argument(
        "--manager-event-cooldown-seconds",
        type=int,
        default=120,
        help="Minimum seconds between event-driven manager reports (default: 120)"
    )
    parser.add_argument(
        "--manager-slack",
        action="store_true",
        help="Send manager hourly reports to Slack webhook"
    )
    parser.add_argument(
        "--manager-slack-webhook",
        default="",
        help="Override Slack webhook URL for manager reports"
    )
    
    args = parser.parse_args()
    settings = load_settings()
    manager_slack_webhook = str(args.manager_slack_webhook or "").strip() or str(getattr(settings, "slack_webhook_url", "") or "").strip()
    manager_slack_enabled = bool(args.manager_slack) or (bool(getattr(settings, "slack_enabled", False)) and bool(manager_slack_webhook))
    try:
        run(
            enable_dashboard=args.dashboard,
            dashboard_interval=args.update_interval,
            ai_company_mode=args.ai_company,
            manager_report_minutes=args.manager_report_minutes,
            manager_cycle_seconds=args.manager_cycle_seconds,
            manager_report_path=args.manager_report_path,
            manager_slack_enabled=manager_slack_enabled,
            manager_slack_webhook_url=manager_slack_webhook,
            manager_event_cooldown_seconds=args.manager_event_cooldown_seconds,
        )
    except RuntimeError as exc:
        logging.error("Run failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
