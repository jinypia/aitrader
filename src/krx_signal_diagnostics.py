from __future__ import annotations

import json
from pathlib import Path

from backtest_compare import TUNED_TECH, _mean, _pct_change, _prepare_market_data, _tech_flags
from config import load_settings, selection_universe_symbols
from krx_iterative_tune import TuneParams, _metrics_from_bars
from strategy import bearish_long_exception_ready, trend_runtime_signal


def _pct(part: int, whole: int) -> float:
    return (float(part) / float(whole) * 100.0) if whole > 0 else 0.0


def main() -> None:
    s = load_settings()
    symbols = [x for x in selection_universe_symbols(s) if x.strip().isdigit()][:12]
    ready = _prepare_market_data(market="KR", symbols=symbols, fetch_limit=300)
    params = TuneParams(
        trend_daily_rsi_min=float(s.trend_daily_rsi_min),
        trend_daily_rsi_max=float(s.trend_daily_rsi_max),
        trend_min_turnover_ratio_5_to_20=float(s.trend_min_turnover_ratio_5_to_20),
        trend_min_value_spike_ratio=float(s.trend_min_value_spike_ratio),
        trend_gap_skip_up_pct=float(s.trend_gap_skip_up_pct),
        trend_gap_skip_down_pct=float(s.trend_gap_skip_down_pct),
        trend_max_chase_from_open_pct=float(s.trend_max_chase_from_open_pct),
        trend_breakout_near_high_pct=float(getattr(s, "trend_breakout_near_high_pct", 97.0)),
        trend_overheat_day_pct=float(s.trend_overheat_day_pct),
        trend_overheat_2day_pct=float(s.trend_overheat_2day_pct),
        signal_confirm_cycles=int(s.signal_confirm_cycles),
        volume_spike_mult=float(TUNED_TECH.volume_spike_mult),
        golden_cross_entry_bb_max=float(TUNED_TECH.golden_cross_entry_bb_max),
    )

    total_bars = 0
    filter_pass = {
        "trend_ok": 0,
        "structure_ok": 0,
        "breakout_ok": 0,
        "not_overheat": 0,
        "not_overextended": 0,
        "rsi_ok": 0,
        "attention_ok": 0,
        "value_spike_ok": 0,
        "gap_up_ok": 0,
        "gap_down_ok": 0,
        "chase_ok": 0,
        "trend_pct_ok": 0,
    }
    pipeline = {
        "core_setup_ready": 0,
        "trend_entry_ready": 0,
        "tech_buy_like": 0,
        "bearish_exception_ready": 0,
        "buy_signal": 0,
        "confirm_blocked_buy": 0,
        "sizing_ready_buy": 0,
    }
    blocker_counts = {
        "trend_ok": 0,
        "structure_ok": 0,
        "breakout_ok": 0,
        "overheat": 0,
        "overextended": 0,
        "rsi": 0,
        "attention": 0,
        "value_spike": 0,
        "gap_up": 0,
        "gap_down": 0,
        "chase": 0,
        "trend_pct": 0,
        "tech_buy": 0,
    }
    symbol_summary: list[dict[str, object]] = []

    for sym, bars in ready:
        sym_bars = 0
        sym_entry_ready = 0
        sym_buy = 0
        sym_confirm_blocked = 0
        sym_sizing_ready = 0
        sym_bearish_exception_ready = 0
        sym_blockers = {k: 0 for k in blocker_counts}
        streak_sig = ""
        streak_cnt = 0
        closes = [float(x.get("close", 0.0)) for x in bars if float(x.get("close", 0.0)) > 0]
        volumes = [int(float(x.get("volume", 0.0))) for x in bars if float(x.get("close", 0.0)) > 0]
        start = max(61, len(closes) - 120)
        for i in range(start, len(closes)):
            prev_p = float(closes[i - 1])
            cur_p = float(closes[i])
            if prev_p <= 0 or cur_p <= 0:
                continue
            sym_bars += 1
            total_bars += 1
            base_idx = max(0, i - min(20, i))
            mom_base = float(closes[base_idx]) if base_idx < len(closes) else prev_p
            momentum_pct = (((cur_p / mom_base) - 1.0) * 100.0) if mom_base > 0 else 0.0
            sma = _mean([float(x) for x in closes[max(0, i - 19): i + 1]])
            trend_pct = ((cur_p - sma) / sma * 100.0) if sma > 0 else 0.0
            metrics = _metrics_from_bars(bars[: i + 1], market_index_pct=0.0, params=params)
            tf = _tech_flags(
                hist_closes=[float(x) for x in closes[:i]],
                current_price=cur_p,
                current_volume=int(volumes[i]),
                hist_volumes=[int(x) for x in volumes[max(0, i - 80): i]],
                params=TUNED_TECH,
            )
            overextension_penalty = (
                max(0.0, float(metrics.get("momentum_pct", 0.0)) - 12.0) * 0.35
                + max(0.0, trend_pct - 6.0) * 1.20
                + max(0.0, float(metrics.get("daily_rsi", 50.0)) - 72.0) * 0.15
            )

            checks = {
                "trend_ok": bool(metrics.get("trend_ok", 0.0)),
                "structure_ok": bool(metrics.get("structure_ok", 0.0)),
                "breakout_ok": bool(metrics.get("breakout_ok", 0.0)),
                "not_overheat": not bool(metrics.get("overheat", 0.0)),
                "not_overextended": overextension_penalty <= 0.0,
                "rsi_ok": params.trend_daily_rsi_min <= float(metrics.get("daily_rsi", 50.0)) <= params.trend_daily_rsi_max,
                "attention_ok": float(metrics.get("attention_ratio", 0.0)) >= params.trend_min_turnover_ratio_5_to_20,
                "value_spike_ok": float(metrics.get("value_spike_ratio", 0.0)) >= params.trend_min_value_spike_ratio,
                "gap_up_ok": _pct_change(cur_p, prev_p) < params.trend_gap_skip_up_pct,
                "gap_down_ok": _pct_change(cur_p, prev_p) >= params.trend_gap_skip_down_pct,
                "chase_ok": _pct_change(cur_p, prev_p) <= params.trend_max_chase_from_open_pct,
                "trend_pct_ok": trend_pct >= -0.5,
            }
            for key, ok in checks.items():
                filter_pass[key] += int(ok)
                if not ok:
                    base_key = "overheat" if key == "not_overheat" else key.replace("_ok", "")
                    if key == "not_overextended":
                        base_key = "overextended"
                    if base_key == "breakout":
                        base_key = "breakout_ok"
                    elif base_key == "trend":
                        base_key = "trend_ok"
                    elif base_key == "structure":
                        base_key = "structure_ok"
                    blocker_counts[base_key] += 1
                    sym_blockers[base_key] += 1

            core_setup_ready = all(checks.values())
            pipeline["core_setup_ready"] += int(core_setup_ready)

            action, _tech_priority, trend_entry_ready = trend_runtime_signal(
                qty=0,
                trend_ok=checks["trend_ok"],
                structure_ok=checks["structure_ok"],
                breakout_ok=checks["breakout_ok"],
                overheat_flag=not checks["not_overheat"],
                daily_rsi=float(metrics.get("daily_rsi", 50.0)),
                attention_ratio=float(metrics.get("attention_ratio", 0.0)),
                value_spike_ratio=float(metrics.get("value_spike_ratio", 0.0)),
                gap_from_prev_close_pct=_pct_change(cur_p, prev_p),
                trend_daily_rsi_min=params.trend_daily_rsi_min,
                trend_daily_rsi_max=params.trend_daily_rsi_max,
                trend_min_turnover_ratio_5_to_20=params.trend_min_turnover_ratio_5_to_20,
                trend_min_value_spike_ratio=params.trend_min_value_spike_ratio,
                trend_gap_skip_up_pct=params.trend_gap_skip_up_pct,
                trend_gap_skip_down_pct=params.trend_gap_skip_down_pct,
                trend_max_chase_from_open_pct=params.trend_max_chase_from_open_pct,
                market_chg_pct=float(metrics.get("market_index_pct", 0.0)),
                momentum_pct=momentum_pct,
                trend_pct=trend_pct,
                tech_flags=tf,
                golden_cross_entry_bb_max=params.golden_cross_entry_bb_max,
            )
            pipeline["trend_entry_ready"] += int(trend_entry_ready)
            sym_entry_ready += int(trend_entry_ready)
            bearish_exception_ready = bearish_long_exception_ready(
                trend_ok=checks["trend_ok"],
                structure_ok=checks["structure_ok"],
                breakout_ok=checks["breakout_ok"],
                daily_rsi=float(metrics.get("daily_rsi", 50.0)),
                attention_ratio=float(metrics.get("attention_ratio", 0.0)),
                value_spike_ratio=float(metrics.get("value_spike_ratio", 0.0)),
                momentum_pct=momentum_pct,
                trend_pct=trend_pct,
                tech_flags=tf,
            )
            pipeline["bearish_exception_ready"] += int(bearish_exception_ready)
            sym_bearish_exception_ready += int(bearish_exception_ready)

            tech_buy_like = (
                (bool(tf.get("golden_cross")) and float(tf.get("bb_pos", 0.5)) <= params.golden_cross_entry_bb_max)
                or (bool(tf.get("short_bottom")) and (bool(tf.get("volume_spike")) or bool(tf.get("trend_up"))))
                or (
                    bool(tf.get("trend_up"))
                    and not bool(tf.get("trend_down"))
                    and checks["breakout_ok"]
                    and (0.45 <= float(tf.get("bb_pos", 0.5)) <= 1.20)
                    and momentum_pct >= 0.35
                    and trend_pct >= 0.05
                    and (
                        bool(tf.get("volume_spike"))
                        or bool(tf.get("golden_cross"))
                        or float(tf.get("bb_pos", 0.5)) <= 0.70
                    )
                )
            )
            pipeline["tech_buy_like"] += int(tech_buy_like)
            if trend_entry_ready and not tech_buy_like:
                blocker_counts["tech_buy"] += 1
                sym_blockers["tech_buy"] += 1

            executable_action = action
            local_confirm = 1 if _tech_priority and action in {"BUY", "SELL"} else max(1, int(params.signal_confirm_cycles))
            if executable_action in {"BUY", "SELL"}:
                if executable_action == streak_sig:
                    streak_cnt += 1
                else:
                    streak_sig = executable_action
                    streak_cnt = 1
                if streak_cnt < local_confirm:
                    if executable_action == "BUY":
                        pipeline["confirm_blocked_buy"] += 1
                        sym_confirm_blocked += 1
                    executable_action = "HOLD"
            else:
                streak_sig = ""
                streak_cnt = 0

            if executable_action == "BUY" and trend_entry_ready:
                atr14_pct = float(metrics.get("atr14_pct", 2.0))
                stop_pct = max(2.8, atr14_pct * 1.4) / 100.0
                equity_now = 1_000_000.0
                risk_budget = max(0.0, equity_now * 0.004)
                capital_cap = equity_now * 0.12
                qty_cash = int(min(equity_now, capital_cap) / cur_p)
                qty_risk = int(risk_budget / max(1e-9, cur_p * stop_pct))
                if qty_cash > 0 and qty_risk > 0:
                    pipeline["sizing_ready_buy"] += 1
                    sym_sizing_ready += 1

            pipeline["buy_signal"] += int(executable_action == "BUY")
            sym_buy += int(executable_action == "BUY")

        top_blockers = sorted(sym_blockers.items(), key=lambda kv: kv[1], reverse=True)[:4]
        symbol_summary.append(
            {
                "symbol": sym,
                "bars": sym_bars,
                "trend_entry_ready": sym_entry_ready,
                "bearish_exception_ready": sym_bearish_exception_ready,
                "confirm_blocked_buy": sym_confirm_blocked,
                "sizing_ready_buy": sym_sizing_ready,
                "buy_signal": sym_buy,
                "top_blockers": [{"name": k, "count": v} for k, v in top_blockers if v > 0],
            }
        )

    report = {
        "universe": symbols,
        "symbol_count": len(ready),
        "bars_analyzed": total_bars,
        "params": {
            "trend_daily_rsi_min": params.trend_daily_rsi_min,
            "trend_daily_rsi_max": params.trend_daily_rsi_max,
            "trend_min_turnover_ratio_5_to_20": params.trend_min_turnover_ratio_5_to_20,
            "trend_min_value_spike_ratio": params.trend_min_value_spike_ratio,
            "trend_gap_skip_up_pct": params.trend_gap_skip_up_pct,
            "trend_gap_skip_down_pct": params.trend_gap_skip_down_pct,
            "trend_max_chase_from_open_pct": params.trend_max_chase_from_open_pct,
            "signal_confirm_cycles": params.signal_confirm_cycles,
            "volume_spike_mult": params.volume_spike_mult,
            "golden_cross_entry_bb_max": params.golden_cross_entry_bb_max,
            "overextension_limits": {
                "ret20_pct_max": 12.0,
                "trend_pct_max": 6.0,
                "daily_rsi_max": 72.0,
            },
        },
        "filter_pass": {
            key: {"count": val, "pct": round(_pct(val, total_bars), 2)}
            for key, val in filter_pass.items()
        },
        "pipeline": {
            key: {"count": val, "pct": round(_pct(val, total_bars), 2)}
            for key, val in pipeline.items()
        },
        "blockers": {
            key: {"count": val, "pct": round(_pct(val, total_bars), 2)}
            for key, val in sorted(blocker_counts.items(), key=lambda kv: kv[1], reverse=True)
        },
        "symbols": symbol_summary,
    }

    out_path = Path("data/krx_filter_diagnostics_core12.json")
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    print("KRX signal diagnostics (core 12, 120d)")
    print("-" * 96)
    print(f"symbols={len(ready)} bars={total_bars}")
    print("filter pass:")
    for key, row in report["filter_pass"].items():
        print(f"  {key:<16} {row['count']:>5}  {row['pct']:>6.2f}%")
    print("pipeline:")
    for key, row in report["pipeline"].items():
        print(f"  {key:<16} {row['count']:>5}  {row['pct']:>6.2f}%")
    print("top blockers:")
    for key, row in list(report["blockers"].items())[:6]:
        print(f"  {key:<16} {row['count']:>5}  {row['pct']:>6.2f}%")
    print(f"saved -> {out_path}")


if __name__ == "__main__":
    main()
