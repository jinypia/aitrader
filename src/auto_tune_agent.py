from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from backtest_compare import _prepare_market_data
from config import load_settings, save_runtime_overrides, selection_universe_symbols
from krx_iterative_tune import (
    TuneParams,
    _mutations,
    _objective,
    _run_market_ready,
)


def _build_baseline_params(settings: Any) -> TuneParams:
    return TuneParams(
        trend_daily_rsi_min=float(settings.trend_daily_rsi_min),
        trend_daily_rsi_max=float(settings.trend_daily_rsi_max),
        trend_min_turnover_ratio_5_to_20=float(settings.trend_min_turnover_ratio_5_to_20),
        trend_min_value_spike_ratio=float(settings.trend_min_value_spike_ratio),
        trend_gap_skip_up_pct=float(settings.trend_gap_skip_up_pct),
        trend_gap_skip_down_pct=float(settings.trend_gap_skip_down_pct),
        trend_max_chase_from_open_pct=float(settings.trend_max_chase_from_open_pct),
        trend_breakout_near_high_pct=float(getattr(settings, "trend_breakout_near_high_pct", 97.0)),
        trend_overheat_day_pct=float(getattr(settings, "trend_overheat_day_pct", 18.0)),
        trend_overheat_2day_pct=float(getattr(settings, "trend_overheat_2day_pct", 25.0)),
        signal_confirm_cycles=int(getattr(settings, "signal_confirm_cycles", 1)),
        volume_spike_mult=float(getattr(settings, "volume_spike_mult", 1.25)),
        golden_cross_entry_bb_max=float(getattr(settings, "golden_cross_entry_bb_max", 0.72)),
    )


def _tune_parameters(
    ready: list[tuple[str, list[dict[str, float]]]],
    baseline: TuneParams,
    rounds: int,
    history_days: int,
    cash: float,
) -> tuple[TuneParams, list[dict[str, Any]]]:
    current_best = baseline
    history: list[dict[str, Any]] = []

    for round_idx in range(1, max(2, rounds + 1)):
        if round_idx == 1:
            candidates = [current_best]
        else:
            candidates = _mutations(current_best, round_idx)

        best_row: dict[str, Any] | None = None
        for cand in candidates:
            report = _run_market_ready(ready=ready, days=history_days, cash=cash, params=cand)
            score = _objective(report)
            row = {
                "round": round_idx,
                "score": score,
                "report": report,
                "params": asdict(cand),
            }
            if best_row is None or row["score"] > best_row["score"]:
                best_row = row

        if best_row is None:
            raise RuntimeError("No tuning candidate was evaluated.")

        current_best = TuneParams(**dict(best_row["params"]))
        history.append(best_row)

    return current_best, history


def _dump_history(report: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def _save_overrides(best_params: TuneParams) -> None:
    overrides = {
        "TREND_DAILY_RSI_MIN": str(best_params.trend_daily_rsi_min),
        "TREND_DAILY_RSI_MAX": str(best_params.trend_daily_rsi_max),
        "TREND_MIN_TURNOVER_RATIO_5_TO_20": str(best_params.trend_min_turnover_ratio_5_to_20),
        "TREND_MIN_VALUE_SPIKE_RATIO": str(best_params.trend_min_value_spike_ratio),
        "TREND_GAP_SKIP_UP_PCT": str(best_params.trend_gap_skip_up_pct),
        "TREND_GAP_SKIP_DOWN_PCT": str(best_params.trend_gap_skip_down_pct),
        "TREND_MAX_CHASE_FROM_OPEN_PCT": str(best_params.trend_max_chase_from_open_pct),
        "TREND_BREAKOUT_NEAR_HIGH_PCT": str(best_params.trend_breakout_near_high_pct),
        "TREND_OVERHEAT_DAY_PCT": str(best_params.trend_overheat_day_pct),
        "TREND_OVERHEAT_2DAY_PCT": str(best_params.trend_overheat_2day_pct),
        "SIGNAL_CONFIRM_CYCLES": str(best_params.signal_confirm_cycles),
        "VOLUME_SPIKE_MULT": str(best_params.volume_spike_mult),
        "GOLDEN_CROSS_ENTRY_BB_MAX": str(best_params.golden_cross_entry_bb_max),
    }
    save_runtime_overrides(overrides)


def main() -> None:
    parser = argparse.ArgumentParser(description="Automatic tuning agent for KRX trend parameters.")
    parser.add_argument("--rounds", type=int, default=6, help="Number of tuning rounds")
    parser.add_argument("--symbols", type=int, default=20, help="Number of KRX symbols to use")
    parser.add_argument("--history-days", type=int, default=120, help="Backtest history length in days")
    parser.add_argument("--fetch-limit", type=int, default=300, help="Maximum bar history to load")
    parser.add_argument(
        "--cash",
        type=float,
        default=None,
        help="Simulation cash for the full tuning run; defaults to settings.INITIAL_CASH or 1,000,000",
    )
    parser.add_argument("--save", action="store_true", help="Save tuned parameters to data/runtime_config.json and .env")
    parser.add_argument("--output", default="data/auto_tune_agent_report.json", help="Output path for tuning history")
    args = parser.parse_args()

    settings = load_settings()
    cash = float(args.cash) if args.cash is not None else max(1_000_000.0, float(settings.initial_cash))
    symbols = [x for x in selection_universe_symbols(settings) if x.strip().isdigit()][: args.symbols]
    if not symbols:
        raise SystemExit("No KRX symbols available for tuning.")

    ready = _prepare_market_data(market="KR", symbols=symbols, fetch_limit=args.fetch_limit)
    if not ready:
        raise SystemExit("No market history available for selected symbol universe.")

    baseline = _build_baseline_params(settings)
    best_params, history = _tune_parameters(
        ready=ready,
        baseline=baseline,
        rounds=args.rounds,
        history_days=args.history_days,
        cash=cash,
    )

    report = {
        "symbols": symbols,
        "symbol_history_count": len(ready),
        "history_days": args.history_days,
        "rounds": args.rounds,
        "baseline": asdict(baseline),
        "best_params": asdict(best_params),
        "history": history,
    }
    output_path = Path(args.output)
    _dump_history(report["history"], output_path)

    print("Auto tuning complete")
    print(f"  symbols used: {len(symbols)}")
    print(f"  history days: {args.history_days}")
    print(f"  output report: {output_path}")

    baseline_dict = asdict(baseline)
    best_dict = asdict(best_params)
    changed = {
        key: {
            "baseline": baseline_dict[key],
            "best": best_dict[key],
        }
        for key in best_dict
        if baseline_dict.get(key) != best_dict.get(key)
    }

    if changed:
        print("Suggested parameter changes:")
        for key, values in changed.items():
            print(f"  {key}: {values['baseline']} -> {values['best']}")
    else:
        print("No parameter changes suggested; baseline is already optimal.")

    if args.save:
        _save_overrides(best_params)
        print("Saved tuned parameters to data/runtime_config.json and .env")


if __name__ == "__main__":
    main()
