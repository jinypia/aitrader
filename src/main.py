from __future__ import annotations

import argparse
import logging
import sys
import threading
from pathlib import Path

from bot_runtime import BotState, run_bot
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
    
    args = parser.parse_args()
    try:
        run(
            enable_dashboard=args.dashboard,
            dashboard_interval=args.update_interval,
            ai_company_mode=args.ai_company,
            manager_report_minutes=args.manager_report_minutes,
            manager_cycle_seconds=args.manager_cycle_seconds,
            manager_report_path=args.manager_report_path,
        )
    except RuntimeError as exc:
        logging.error("Run failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
