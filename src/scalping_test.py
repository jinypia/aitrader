from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from backtest_compare import generate_intraday_scalping_report
from scalping_strategy import ScalpParams, calculate_scalp_metrics, scalp_entry_signal, scalp_exit_signal


def test_scalping_strategy_metrics() -> None:
    closes = [100, 101, 102, 101.5, 102.5, 103, 102.2, 103.1, 104, 103.8, 104.2, 104.5, 104.8, 105.0, 105.5, 106.2, 105.7, 106.5, 107.0, 107.5]
    bars = [{"close": c, "volume": 1000.0 + (i * 10)} for i, c in enumerate(closes)]
    params = ScalpParams(rsi_entry_min=10.0, rsi_entry_max=90.0, min_volume_ratio=0.0, trend_strength_threshold=0.0)
    metrics = calculate_scalp_metrics(bars, params)
    assert isinstance(metrics, dict)
    assert metrics.get("rsi") > 0
    assert metrics.get("momentum") != 0
    assert scalp_entry_signal(metrics, params)
    assert scalp_exit_signal(entry_price=100.0, current_price=103.0, hold_bars=1, rsi=80.0, params=params) is not None


def test_generate_intraday_scalping_report() -> None:
    path = Path("data/selected_intraday_prices.json")
    backup = None
    if path.exists():
        backup = path.read_text()
    try:
        start_ts = datetime(2026, 4, 3, 9, 10)
        rows = []
        for i in range(24):
            rows.append(
                {
                    "bar_ts": (start_ts + timedelta(minutes=10 * i)).strftime("%Y-%m-%d %H:%M:%S"),
                    "symbol": "000660",
                    "price": 100.0 + i * 0.5,
                    "quote_volume": 1000 + i * 20,
                    "action": "BUY" if i == 0 else "HOLD",
                }
            )

        payload = {
            "updated_at": "",
            "bar_interval_minutes": 10,
            "rows": rows,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))

        report = generate_intraday_scalping_report(
            window_days=2,
            target_day='2026-04-03',
            params=ScalpParams(
                rsi_entry_min=0.0,
                rsi_entry_max=100.0,
                min_volume_ratio=0.0,
                trend_strength_threshold=0.0,
            ),
        )
        assert report.get("report_type") == "intraday_scalping"
        summary = report.get("summary", {})
        assert summary.get("day_count", 0) >= 1
        assert summary.get("trade_count", 0) >= 1
    finally:
        if backup is None:
            if path.exists():
                path.unlink()
        else:
            path.write_text(backup)


if __name__ == "__main__":
    test_scalping_strategy_metrics()
    test_generate_intraday_scalping_report()
    print("scalping_test passed")
