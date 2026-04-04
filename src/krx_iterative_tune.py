from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from backtest_compare import (
    TUNED_TECH,
    _mean,
    _pct_change,
    _prepare_market_data,
    _pstdev,
    _rsi,
    _tech_flags,
    _trend_structure_ok,
)
from config import load_settings, selection_universe_symbols
from strategy import bearish_long_exception_ready, trend_runtime_signal


@dataclass
class TuneParams:
    trend_daily_rsi_min: float
    trend_daily_rsi_max: float
    trend_min_turnover_ratio_5_to_20: float
    trend_min_value_spike_ratio: float
    trend_gap_skip_up_pct: float
    trend_gap_skip_down_pct: float
    trend_max_chase_from_open_pct: float
    trend_breakout_near_high_pct: float
    trend_overheat_day_pct: float
    trend_overheat_2day_pct: float
    signal_confirm_cycles: int
    volume_spike_mult: float
    golden_cross_entry_bb_max: float


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _metrics_from_bars(
    bars: list[dict[str, float]],
    *,
    market_index_pct: float,
    params: TuneParams,
) -> dict[str, float]:
    closes = [float(x.get("close", 0.0)) for x in bars if float(x.get("close", 0.0)) > 0]
    highs = [float(x.get("high", 0.0) or x.get("close", 0.0)) for x in bars if float(x.get("close", 0.0)) > 0]
    values = [float(x.get("value", 0.0)) for x in bars if float(x.get("close", 0.0)) > 0]
    volumes = [float(x.get("volume", 0.0)) for x in bars if float(x.get("close", 0.0)) > 0]
    if len(closes) < 60:
        return {}
    last = closes[-1]
    ma5 = _mean(closes[-5:])
    ma20 = _mean(closes[-20:])
    ma60 = _mean(closes[-60:])
    ma20_prev = _mean(closes[-21:-1]) if len(closes) >= 21 else ma20
    ma60_prev = _mean(closes[-61:-1]) if len(closes) >= 61 else ma60
    ret5 = _pct_change(last, closes[-6]) if len(closes) >= 6 else 0.0
    ret20 = _pct_change(last, closes[-21]) if len(closes) >= 21 else 0.0
    relative_pct = ret20 - market_index_pct
    volatility_pct = (
        _pstdev([_pct_change(closes[i], closes[i - 1]) / 100.0 for i in range(1, len(closes))]) * 100.0
        if len(closes) >= 3
        else 0.0
    )
    turnover5 = _mean(values[-5:])
    turnover20 = _mean(values[-20:])
    volume5 = _mean(volumes[-5:])
    volume20 = _mean(volumes[-20:])
    attention_ratio = (turnover5 / turnover20) if turnover20 > 0 else 0.0
    value_spike_ratio = (values[-1] / turnover20) if turnover20 > 0 and values else 0.0
    volume_ratio = (volume5 / volume20) if volume20 > 0 else 0.0
    near_high_pct = (last / max(highs[-20:]) * 100.0) if highs and max(highs[-20:]) > 0 else 0.0
    daily_rsi = _rsi(closes, 14)
    ret1 = _pct_change(last, closes[-2]) if len(closes) >= 2 else 0.0
    ret2 = _pct_change(last, closes[-3]) if len(closes) >= 3 else ret1
    trend_ok = 1.0 if (ma5 > ma20 > ma60) else 0.0
    structure_ok = 1.0 if _trend_structure_ok(bars) else 0.0
    breakout_ok = 1.0 if near_high_pct >= params.trend_breakout_near_high_pct else 0.0
    overheat = 1.0 if (ret1 >= params.trend_overheat_day_pct or ret2 >= params.trend_overheat_2day_pct) else 0.0
    score = (
        0.30 * ret20
        + 0.20 * ret5
        + 10.0 * (attention_ratio - 1.0)
        + 5.0 * (volume_ratio - 1.0)
        + 0.10 * (near_high_pct - 95.0)
        - 0.35 * volatility_pct
        + 8.0 * trend_ok
        + 6.0 * structure_ok
        + 4.0 * breakout_ok
        - 12.0 * overheat
    )
    return {
        "score": score,
        "momentum_pct": ret20,
        "ret5_pct": ret5,
        "relative_pct": relative_pct,
        "trend_pct": _pct_change(ma20, ma20_prev) if ma20_prev > 0 else 0.0,
        "volatility_pct": volatility_pct,
        "attention_ratio": attention_ratio,
        "value_spike_ratio": value_spike_ratio,
        "daily_rsi": daily_rsi,
        "near_high_pct": near_high_pct,
        "trend_ok": trend_ok,
        "structure_ok": structure_ok,
        "breakout_ok": breakout_ok,
        "overheat": overheat,
    }


def _market_proxy_returns_pct(ready: list[tuple[str, list[dict[str, float]]]]) -> list[float]:
    min_len = min(len(bars) for _, bars in ready if bars)
    if min_len < 3:
        return []
    proxy: list[float] = []
    for t in range(min_len):
        proxy.append(_mean([(float(bars[-min_len + t].get("close", 0.0)) / float(bars[-min_len].get("close", 1.0))) * 100.0 for _, bars in ready]))
    out = [0.0]
    for i in range(1, len(proxy)):
        prev = proxy[i - 1]
        cur = proxy[i]
        out.append(((cur - prev) / prev) * 100.0 if prev > 0 else 0.0)
    return out


def _simulate_symbol(
    *,
    bars: list[dict[str, float]],
    days: int,
    initial_cash: float,
    params: TuneParams,
    market_change_pct_series: list[float],
) -> dict[str, float]:
    closes = [float(x.get("close", 0.0)) for x in bars if float(x.get("close", 0.0)) > 0]
    volumes = [int(float(x.get("volume", 0.0))) for x in bars if float(x.get("close", 0.0)) > 0]
    if len(closes) < 80:
        return {"final_equity": initial_cash, "trade_count": 0.0, "sell_count": 0.0, "win_rate": 0.0, "mdd": 0.0}
    cash = float(initial_cash)
    qty = 0
    avg = 0.0
    peak = cash
    mdd = 0.0
    trade_count = 0
    sell_count = 0
    win_count = 0
    peak_price = 0.0
    entry_stop_atr = 0.0
    entry_take_atr = 0.0
    entry_trailing_atr = 0.0
    entry_stop_floor_pct = 0.0
    entry_take_floor_pct = 0.0
    streak_sig = ""
    streak_cnt = 0
    realized_pnls: list[float] = []
    loss_streak = 0
    max_loss_streak = 0
    start = max(61, len(closes) - max(20, int(days)))
    tech_params = replace(
        TUNED_TECH,
        volume_spike_mult=params.volume_spike_mult,
        golden_cross_entry_bb_max=params.golden_cross_entry_bb_max,
    )
    settings = load_settings()
    bearish_trigger_pct = float(getattr(settings, "bearish_exception_trigger_pct", -0.4))
    bearish_max_drop_pct = float(getattr(settings, "bearish_exception_max_market_drop_pct", -9.0))
    market_shock_drop_pct = float(getattr(settings, "market_shock_drop_pct", -2.0))
    for i in range(start, len(closes)):
        prev_p = float(closes[i - 1])
        cur_p = float(closes[i])
        if prev_p <= 0 or cur_p <= 0:
            continue
        base_idx = max(0, i - min(20, i))
        mom_base = float(closes[base_idx]) if base_idx < len(closes) else prev_p
        momentum_pct = (((cur_p / mom_base) - 1.0) * 100.0) if mom_base > 0 else 0.0
        sma = _mean([float(x) for x in closes[max(0, i - 19): i + 1]])
        trend_pct = ((cur_p - sma) / sma * 100.0) if sma > 0 else 0.0
        mkt_i = i + (len(market_change_pct_series) - len(closes))
        market_chg_pct = float(market_change_pct_series[mkt_i]) if 0 <= mkt_i < len(market_change_pct_series) else 0.0
        metrics = _metrics_from_bars(bars[: i + 1], market_index_pct=market_chg_pct, params=params)
        tf = _tech_flags(
            hist_closes=[float(x) for x in closes[:i]],
            current_price=cur_p,
            current_volume=int(volumes[i]),
            hist_volumes=[int(x) for x in volumes[max(0, i - 80): i]],
            params=tech_params,
        )
        action, tech_priority, trend_entry_ready = trend_runtime_signal(
            qty=qty,
            trend_ok=bool(metrics.get("trend_ok", 0.0)),
            structure_ok=bool(metrics.get("structure_ok", 0.0)),
            breakout_ok=bool(metrics.get("breakout_ok", 0.0)),
            overheat_flag=bool(metrics.get("overheat", 0.0)),
            daily_rsi=float(metrics.get("daily_rsi", 50.0)),
            attention_ratio=float(metrics.get("attention_ratio", 0.0)),
            value_spike_ratio=float(metrics.get("value_spike_ratio", 0.0)),
            gap_from_prev_close_pct=_pct_change(cur_p, prev_p) if prev_p > 0 else 0.0,
            trend_daily_rsi_min=params.trend_daily_rsi_min,
            trend_daily_rsi_max=params.trend_daily_rsi_max,
            trend_min_turnover_ratio_5_to_20=params.trend_min_turnover_ratio_5_to_20,
            trend_min_value_spike_ratio=params.trend_min_value_spike_ratio,
            trend_gap_skip_up_pct=params.trend_gap_skip_up_pct,
            trend_gap_skip_down_pct=params.trend_gap_skip_down_pct,
            trend_max_chase_from_open_pct=params.trend_max_chase_from_open_pct,
            market_chg_pct=market_chg_pct,
            momentum_pct=momentum_pct,
            trend_pct=trend_pct,
            tech_flags=tf,
            golden_cross_entry_bb_max=params.golden_cross_entry_bb_max,
        )
        bearish_long_ok = False
        if bool(getattr(settings, "enable_bearish_exception", False)):
            bearish_long_ok = bearish_long_exception_ready(
                trend_ok=bool(metrics.get("trend_ok", 0.0)),
                structure_ok=bool(metrics.get("structure_ok", 0.0)),
                breakout_ok=bool(metrics.get("breakout_ok", 0.0)),
                daily_rsi=float(metrics.get("daily_rsi", 50.0)),
                attention_ratio=float(metrics.get("attention_ratio", 0.0)),
                value_spike_ratio=float(metrics.get("value_spike_ratio", 0.0)),
                momentum_pct=momentum_pct,
                trend_pct=trend_pct,
                tech_flags=tf,
            )
        if market_chg_pct <= bearish_trigger_pct and qty <= 0 and action == "HOLD" and bearish_long_ok:
            action = "BUY"
            tech_priority = True
        if market_chg_pct <= bearish_trigger_pct and qty <= 0 and action == "BUY" and not bearish_long_ok:
            action = "HOLD"
        if market_chg_pct <= market_shock_drop_pct and qty <= 0 and action == "BUY":
            if not (bearish_long_ok and market_chg_pct > bearish_max_drop_pct):
                action = "HOLD"
        local_confirm = 1 if tech_priority and action in {"BUY", "SELL"} else max(1, int(params.signal_confirm_cycles))
        if action in {"BUY", "SELL"}:
            if action == streak_sig:
                streak_cnt += 1
            else:
                streak_sig = action
                streak_cnt = 1
            if streak_cnt < local_confirm:
                action = "HOLD"
        else:
            streak_sig = ""
            streak_cnt = 0

        bearish_entry_ready = market_chg_pct <= bearish_trigger_pct and bearish_long_ok
        entry_allowed = trend_entry_ready or bearish_entry_ready
        if action == "BUY" and qty <= 0 and entry_allowed:
            if market_chg_pct >= 0.7:
                entry_stop_atr, entry_take_atr, entry_trailing_atr = 1.8, 4.0, 2.4
                entry_stop_floor_pct, entry_take_floor_pct = 3.5, 10.0
            elif market_chg_pct <= -0.7:
                entry_stop_atr, entry_take_atr, entry_trailing_atr = 1.0, 3.0, 1.6
                entry_stop_floor_pct, entry_take_floor_pct = 2.0, 6.0
            else:
                entry_stop_atr, entry_take_atr, entry_trailing_atr = 1.4, 3.0, 1.8
                entry_stop_floor_pct, entry_take_floor_pct = 2.8, 7.0
            stop_pct = max(entry_stop_floor_pct, 2.0) / 100.0
            equity_now = cash + (qty * cur_p)
            risk_budget = max(0.0, equity_now * 0.004)
            # The tuner simulates one symbol sleeve at a time, so the incoming cash
            # is already the deployable capital for that sleeve.
            capital_cap = equity_now
            qty_cash = int(min(cash, capital_cap) / cur_p)
            qty_risk = int(risk_budget / max(1e-9, cur_p * stop_pct))
            buy_qty = max(1, min(qty_cash, qty_risk)) if qty_cash > 0 and qty_risk > 0 else 0
            need = buy_qty * cur_p
            if need > 0 and cash >= need:
                cash -= need
                qty = buy_qty
                avg = cur_p
                peak_price = cur_p
                trade_count += 1
        elif qty > 0:
            peak_price = max(peak_price, cur_p)
            position_return_pct = _pct_change(cur_p, avg) if avg > 0 else 0.0
            trailing_drawdown_pct = _pct_change(cur_p, peak_price) if peak_price > 0 else 0.0
            if market_chg_pct >= 0.7:
                current_stop_atr, current_take_atr, current_trailing_atr = 1.8, 4.0, 2.4
                current_stop_floor_pct, current_take_floor_pct = 3.5, 10.0
            elif market_chg_pct <= -0.7:
                current_stop_atr, current_take_atr, current_trailing_atr = 1.0, 3.0, 1.6
                current_stop_floor_pct, current_take_floor_pct = 2.0, 6.0
            else:
                current_stop_atr, current_take_atr, current_trailing_atr = 1.4, 3.0, 1.8
                current_stop_floor_pct, current_take_floor_pct = 2.8, 7.0
            stop_loss_pct = -max(
                max(current_stop_floor_pct, entry_stop_floor_pct),
                max(current_stop_atr, entry_stop_atr) * 2.0,
            )
            take_profit_pct = max(
                max(current_take_floor_pct, entry_take_floor_pct),
                max(current_take_atr, entry_take_atr) * 2.0,
            )
            trailing_stop_pct = max(1.9, max(current_trailing_atr, entry_trailing_atr) * 1.2)
            if position_return_pct <= stop_loss_pct:
                action = "SELL"
            elif position_return_pct >= take_profit_pct:
                action = "SELL"
            elif trailing_drawdown_pct <= -trailing_stop_pct:
                action = "SELL"
            elif float(metrics.get("daily_rsi", 50.0)) < 50.0 and float(metrics.get("trend_pct", 0.0)) < 0.0:
                action = "SELL"
        if action == "SELL" and qty > 0:
            pnl = (cur_p - avg) * qty
            cash += qty * cur_p
            qty = 0
            avg = 0.0
            peak_price = 0.0
            entry_stop_atr = 0.0
            entry_take_atr = 0.0
            entry_trailing_atr = 0.0
            entry_stop_floor_pct = 0.0
            entry_take_floor_pct = 0.0
            trade_count += 1
            sell_count += 1
            realized_pnls.append(float(pnl))
            if pnl > 0:
                win_count += 1
                loss_streak = 0
            else:
                loss_streak += 1
                max_loss_streak = max(max_loss_streak, loss_streak)
        equity = cash + (qty * cur_p)
        peak = max(peak, equity)
        dd = ((equity - peak) / peak * 100.0) if peak > 0 else 0.0
        mdd = min(mdd, dd)
    last_px = float(closes[-1])
    final_equity = cash + (qty * last_px)
    win_rate = (win_count / float(sell_count) * 100.0) if sell_count > 0 else 0.0
    avg_win = _mean([x for x in realized_pnls if x > 0])
    avg_loss_abs = abs(_mean([x for x in realized_pnls if x <= 0]))
    expectancy = _mean(realized_pnls)
    gross_profit = sum(x for x in realized_pnls if x > 0)
    gross_loss_abs = abs(sum(x for x in realized_pnls if x <= 0))
    profit_factor = (gross_profit / gross_loss_abs) if gross_loss_abs > 0 else (999.0 if gross_profit > 0 else 0.0)
    return {
        "final_equity": final_equity,
        "trade_count": float(trade_count),
        "sell_count": float(sell_count),
        "win_rate": float(win_rate),
        "mdd": float(mdd),
        "avg_win": float(avg_win),
        "avg_loss_abs": float(avg_loss_abs),
        "expectancy": float(expectancy),
        "profit_factor": float(profit_factor),
        "max_loss_streak": float(max_loss_streak),
    }


def _run_market_ready(
    *,
    ready: list[tuple[str, list[dict[str, float]]]],
    days: int,
    cash: float,
    params: TuneParams,
) -> dict[str, float]:
    if not ready:
        return {"ret": 0.0, "trades": 0.0, "win_rate": 0.0, "mdd": 0.0, "count": 0.0}
    settings = load_settings()
    active_slots = max(
        1,
        min(
            len(ready),
            int(getattr(settings, "trend_select_count", 5)),
            int(getattr(settings, "max_active_positions", 5)),
        ),
    )
    per_cash = float(cash) / float(active_slots)
    market_proxy_rets = _market_proxy_returns_pct(ready)
    symbol_returns: list[float] = []
    trades: list[float] = []
    sells: list[float] = []
    mdds: list[float] = []
    w_rates: list[float] = []
    avg_wins: list[float] = []
    avg_losses: list[float] = []
    expectancies: list[float] = []
    pfs: list[float] = []
    max_loss_streaks: list[float] = []
    for _, bars in ready:
        r = _simulate_symbol(
            bars=bars,
            days=days,
            initial_cash=per_cash,
            params=params,
            market_change_pct_series=market_proxy_rets,
        )
        final_equity = float(r["final_equity"])
        symbol_returns.append(((final_equity - per_cash) / per_cash * 100.0) if per_cash > 0 else 0.0)
        trades.append(float(r["trade_count"]))
        sells.append(float(r["sell_count"]))
        w_rates.append(float(r["win_rate"]))
        mdds.append(float(r["mdd"]))
        avg_wins.append(float(r.get("avg_win", 0.0)))
        avg_losses.append(float(r.get("avg_loss_abs", 0.0)))
        expectancies.append(float(r.get("expectancy", 0.0)))
        pfs.append(float(r.get("profit_factor", 0.0)))
        max_loss_streaks.append(float(r.get("max_loss_streak", 0.0)))
    return {
        "ret": _mean(symbol_returns) if symbol_returns else 0.0,
        "trades": sum(trades),
        "win_rate": _mean([w for w, s in zip(w_rates, sells) if s > 0]) if any(s > 0 for s in sells) else 0.0,
        "mdd": _mean(mdds),
        "count": float(len(ready)),
        "avg_win": _mean(avg_wins),
        "avg_loss_abs": _mean(avg_losses),
        "expectancy": _mean(expectancies),
        "profit_factor": _mean([x for x in pfs if x < 900.0]) if any(x < 900.0 for x in pfs) else _mean(pfs),
        "max_loss_streak": max(max_loss_streaks) if max_loss_streaks else 0.0,
    }


def _objective(rep: dict[str, float]) -> float:
    return (
        rep["ret"]
        - (0.55 * abs(rep["mdd"]))
        + (0.06 * rep["win_rate"])
        + (0.90 * min(5.0, rep["profit_factor"]))
        + (0.003 * rep["expectancy"])
        + (0.05 * rep["trades"])
        - (0.45 * rep["max_loss_streak"])
    )


def _mutations(best: TuneParams, round_idx: int) -> list[TuneParams]:
    scale = max(0.35, 1.0 - (round_idx * 0.07))
    step_rsi = 2.0 * scale
    step_ratio = 0.08 * scale
    step_gap = 0.6 * scale
    step_near_high = 0.5 * scale
    step_overheat = 1.0 * scale
    step_bb = 0.04 * scale
    step_vol = 0.08 * scale
    out = [best]
    out.extend(
        [
            replace(best, trend_daily_rsi_min=_clamp(best.trend_daily_rsi_min - step_rsi, 45.0, 65.0)),
            replace(best, trend_daily_rsi_min=_clamp(best.trend_daily_rsi_min + step_rsi, 45.0, 65.0)),
            replace(best, trend_daily_rsi_max=_clamp(best.trend_daily_rsi_max - step_rsi, 68.0, 85.0)),
            replace(best, trend_daily_rsi_max=_clamp(best.trend_daily_rsi_max + step_rsi, 68.0, 85.0)),
            replace(best, trend_min_turnover_ratio_5_to_20=_clamp(best.trend_min_turnover_ratio_5_to_20 - step_ratio, 0.90, 1.70)),
            replace(best, trend_min_turnover_ratio_5_to_20=_clamp(best.trend_min_turnover_ratio_5_to_20 + step_ratio, 0.90, 1.70)),
            replace(best, trend_min_value_spike_ratio=_clamp(best.trend_min_value_spike_ratio - step_ratio, 0.90, 1.90)),
            replace(best, trend_min_value_spike_ratio=_clamp(best.trend_min_value_spike_ratio + step_ratio, 0.90, 1.90)),
            replace(best, trend_gap_skip_up_pct=_clamp(best.trend_gap_skip_up_pct + step_gap, 4.0, 10.0)),
            replace(best, trend_gap_skip_up_pct=_clamp(best.trend_gap_skip_up_pct - step_gap, 4.0, 10.0)),
            replace(best, trend_gap_skip_down_pct=_clamp(best.trend_gap_skip_down_pct - step_gap, -6.0, -1.0)),
            replace(best, trend_gap_skip_down_pct=_clamp(best.trend_gap_skip_down_pct + step_gap, -6.0, -1.0)),
            replace(best, trend_max_chase_from_open_pct=_clamp(best.trend_max_chase_from_open_pct + step_gap, 5.0, 12.0)),
            replace(best, trend_max_chase_from_open_pct=_clamp(best.trend_max_chase_from_open_pct - step_gap, 5.0, 12.0)),
            replace(best, trend_breakout_near_high_pct=_clamp(best.trend_breakout_near_high_pct - step_near_high, 95.0, 99.0)),
            replace(best, trend_breakout_near_high_pct=_clamp(best.trend_breakout_near_high_pct + step_near_high, 95.0, 99.0)),
            replace(best, trend_overheat_day_pct=_clamp(best.trend_overheat_day_pct + step_overheat, 13.0, 24.0)),
            replace(best, trend_overheat_day_pct=_clamp(best.trend_overheat_day_pct - step_overheat, 13.0, 24.0)),
            replace(best, trend_overheat_2day_pct=_clamp(best.trend_overheat_2day_pct + step_overheat, 18.0, 32.0)),
            replace(best, trend_overheat_2day_pct=_clamp(best.trend_overheat_2day_pct - step_overheat, 18.0, 32.0)),
            replace(best, signal_confirm_cycles=max(1, min(4, best.signal_confirm_cycles - 1))),
            replace(best, signal_confirm_cycles=max(1, min(4, best.signal_confirm_cycles + 1))),
            replace(best, volume_spike_mult=_clamp(best.volume_spike_mult - step_vol, 0.90, 1.80)),
            replace(best, volume_spike_mult=_clamp(best.volume_spike_mult + step_vol, 0.90, 1.80)),
            replace(best, golden_cross_entry_bb_max=_clamp(best.golden_cross_entry_bb_max + step_bb, 0.55, 0.95)),
            replace(best, golden_cross_entry_bb_max=_clamp(best.golden_cross_entry_bb_max - step_bb, 0.55, 0.95)),
            replace(
                best,
                trend_daily_rsi_min=_clamp(best.trend_daily_rsi_min - step_rsi, 45.0, 65.0),
                trend_gap_skip_up_pct=_clamp(best.trend_gap_skip_up_pct + step_gap, 4.0, 10.0),
            ),
            replace(
                best,
                trend_daily_rsi_max=_clamp(best.trend_daily_rsi_max + step_rsi, 68.0, 85.0),
                trend_gap_skip_down_pct=_clamp(best.trend_gap_skip_down_pct - step_gap, -6.0, -1.0),
            ),
            replace(
                best,
                trend_min_turnover_ratio_5_to_20=_clamp(best.trend_min_turnover_ratio_5_to_20 + step_ratio, 0.90, 1.70),
                trend_min_value_spike_ratio=_clamp(best.trend_min_value_spike_ratio + step_ratio, 0.90, 1.90),
            ),
            replace(
                best,
                trend_breakout_near_high_pct=_clamp(best.trend_breakout_near_high_pct + step_near_high, 95.0, 99.0),
                golden_cross_entry_bb_max=_clamp(best.golden_cross_entry_bb_max - step_bb, 0.55, 0.95),
            ),
        ]
    )
    dedup: dict[tuple[object, ...], TuneParams] = {}
    for p in out:
        key = tuple(asdict(p).values())
        dedup[key] = p
    return list(dedup.values())


def main() -> None:
    s = load_settings()
    kr_symbols = [x for x in selection_universe_symbols(s) if x.strip().isdigit()][:20]
    ready = _prepare_market_data(market="KR", symbols=kr_symbols, fetch_limit=300)
    if not ready:
        raise SystemExit("No KRX symbols with enough history for tuning.")

    baseline = TuneParams(
        trend_daily_rsi_min=float(s.trend_daily_rsi_min),
        trend_daily_rsi_max=float(s.trend_daily_rsi_max),
        trend_min_turnover_ratio_5_to_20=float(s.trend_min_turnover_ratio_5_to_20),
        trend_min_value_spike_ratio=float(s.trend_min_value_spike_ratio),
        trend_gap_skip_up_pct=float(s.trend_gap_skip_up_pct),
        trend_gap_skip_down_pct=float(s.trend_gap_skip_down_pct),
        trend_max_chase_from_open_pct=float(s.trend_max_chase_from_open_pct),
        trend_breakout_near_high_pct=97.0,
        trend_overheat_day_pct=float(s.trend_overheat_day_pct),
        trend_overheat_2day_pct=float(s.trend_overheat_2day_pct),
        signal_confirm_cycles=int(s.signal_confirm_cycles),
        volume_spike_mult=float(TUNED_TECH.volume_spike_mult),
        golden_cross_entry_bb_max=float(TUNED_TECH.golden_cross_entry_bb_max),
    )
    current_best = baseline
    history: list[dict[str, object]] = []
    cash = float(max(1_000_000.0, float(s.initial_cash)))
    for round_idx in range(1, 11):
        candidates = [current_best] if round_idx == 1 else _mutations(current_best, round_idx)
        best_row: dict[str, object] | None = None
        for cand in candidates:
            rep = _run_market_ready(ready=ready, days=120, cash=cash, params=cand)
            score = _objective(rep)
            row = {
                "round": round_idx,
                "score": score,
                "report": rep,
                "params": asdict(cand),
            }
            if best_row is None or float(row["score"]) > float(best_row["score"]):
                best_row = row
        assert best_row is not None
        current_best = TuneParams(**dict(best_row["params"]))
        history.append(best_row)

    baseline_rep = history[0]["report"]
    final_rep = history[-1]["report"]
    print(f"KRX iterative tuning (120d, {len(ready)} symbols)")
    print("-" * 168)
    print("round  score    ret%    mdd%   win%   pf    expectancy   trades  maxL  rsi[min,max]  attn  spike  gapUp  confirm  volSpike  gcBB")
    print("-" * 168)
    for row in history:
        rep = row["report"]
        p = row["params"]
        print(
            f"{int(row['round']):<6}{float(row['score']):>7.2f}  {float(rep['ret']):>+6.2f}  {float(rep['mdd']):>+6.2f}  "
            f"{float(rep['win_rate']):>5.1f}  {float(rep['profit_factor']):>4.2f}  {float(rep['expectancy']):>+10.0f}  "
            f"{int(float(rep['trades'])):>6}  {int(float(rep['max_loss_streak'])):>4}  "
            f"{float(p['trend_daily_rsi_min']):>4.1f},{float(p['trend_daily_rsi_max']):>4.1f}  "
            f"{float(p['trend_min_turnover_ratio_5_to_20']):>4.2f}  {float(p['trend_min_value_spike_ratio']):>4.2f}  "
            f"{float(p['trend_gap_skip_up_pct']):>5.1f}  {int(p['signal_confirm_cycles']):>7}  "
            f"{float(p['volume_spike_mult']):>8.2f}  {float(p['golden_cross_entry_bb_max']):>4.2f}"
        )
    print("-" * 168)
    print(
        "baseline -> final: "
        + f"ret {float(final_rep['ret']) - float(baseline_rep['ret']):+.2f}pp, "
        + f"mdd {float(final_rep['mdd']) - float(baseline_rep['mdd']):+.2f}pp, "
        + f"win_rate {float(final_rep['win_rate']) - float(baseline_rep['win_rate']):+.2f}pp, "
        + f"pf {float(final_rep['profit_factor']) - float(baseline_rep['profit_factor']):+.2f}, "
        + f"expectancy {float(final_rep['expectancy']) - float(baseline_rep['expectancy']):+.0f}, "
        + f"trades {int(float(final_rep['trades']) - float(baseline_rep['trades'])):+d}"
    )
    out_path = Path("data/krx_tuning_report.json")
    out_path.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n")
    print(f"saved report -> {out_path}")


if __name__ == "__main__":
    main()
