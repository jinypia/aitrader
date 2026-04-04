from __future__ import annotations


def decide_action_optimized(
    *,
    prev_price: float,
    current_price: float,
    buy_drop_pct: float = -2.00,
    sell_rise_pct: float = 2.00,
    momentum_pct: float = 0.0,
    trend_pct: float = 0.0,
    enable_trend_entry: bool = True,
    breakout_buy_pct: float = 0.70,
    pullback_sell_pct: float = -1.23,
    atr_pct: float | None = None,
    volume_ratio: float | None = None,
    min_volume_ratio: float = 1.20,
) -> str:
    if prev_price <= 0 or current_price <= 0:
        return "HOLD"

    pct = ((current_price - prev_price) / prev_price) * 100.0

    # Ignore weak moves that happen without enough participation.
    if volume_ratio is not None and volume_ratio > 0 and volume_ratio < min_volume_ratio:
        return "HOLD"

    buy_th = float(buy_drop_pct)
    sell_th = float(sell_rise_pct)
    pullback_th = float(pullback_sell_pct)

    # ATR-based dynamic thresholds. Cap the ATR effect so a volatile day does
    # not make the trigger unrealistically wide and suppress all trades.
    if atr_pct is not None and atr_pct > 0:
        atr_ref = min(float(atr_pct), 2.0)
        buy_th = -(1.5 * atr_ref)
        sell_th = 2.0 * atr_ref
        pullback_th = -(1.0 * atr_ref)

    if momentum_pct >= 1.0 and trend_pct >= 0.3:
        buy_th *= 0.70
        sell_th *= 1.35
    elif momentum_pct <= -0.8 or trend_pct <= -0.3:
        buy_th *= 1.35
        sell_th *= 0.75

    if enable_trend_entry:
        if pct >= breakout_buy_pct and momentum_pct >= 0.8 and trend_pct >= 0.2:
            return "BUY"
        if pct <= pullback_th and (momentum_pct <= 0.0 or trend_pct <= 0.0):
            return "SELL"

    if pct <= buy_th and momentum_pct <= -0.6 and trend_pct <= -0.2:
        return "HOLD"

    if pct <= max(pullback_th, -0.7) and (momentum_pct <= -0.6 or trend_pct <= -0.2):
        return "SELL"

    if pct <= buy_th:
        return "BUY"
    if pct >= sell_th:
        return "SELL"
    return "HOLD"


def decide_action(
    *,
    prev_price: float,
    current_price: float,
    buy_drop_pct: float,
    sell_rise_pct: float,
    momentum_pct: float = 0.0,
    trend_pct: float = 0.0,
    enable_trend_entry: bool = False,
    breakout_buy_pct: float = 0.70,
    pullback_sell_pct: float = -1.23,
) -> str:
    # Keep the legacy entrypoint for callers that still import decide_action.
    return decide_action_optimized(
        prev_price=prev_price,
        current_price=current_price,
        buy_drop_pct=buy_drop_pct,
        sell_rise_pct=sell_rise_pct,
        momentum_pct=momentum_pct,
        trend_pct=trend_pct,
        enable_trend_entry=enable_trend_entry,
        breakout_buy_pct=breakout_buy_pct,
        pullback_sell_pct=pullback_sell_pct,
    )


def trend_runtime_diagnostics(
    *,
    qty: int,
    trend_ok: bool,
    structure_ok: bool,
    breakout_ok: bool,
    overheat_flag: bool,
    daily_rsi: float,
    attention_ratio: float,
    value_spike_ratio: float,
    gap_from_prev_close_pct: float,
    trend_daily_rsi_min: float,
    trend_daily_rsi_max: float,
    trend_min_turnover_ratio_5_to_20: float,
    trend_min_value_spike_ratio: float,
    trend_gap_skip_up_pct: float,
    trend_gap_skip_down_pct: float,
    trend_max_chase_from_open_pct: float,
    market_chg_pct: float,
    momentum_pct: float,
    trend_pct: float,
    tech_flags: dict[str, object],
    prev_price: float = 0.0,
    current_price: float = 0.0,
    atr_pct: float | None = None,
    volume_ratio: float | None = None,
) -> dict[str, object]:
    trend_up = bool(tech_flags.get("trend_up", False))
    trend_down = bool(tech_flags.get("trend_down", False))
    bb_pos = float(tech_flags.get("bb_pos", 0.5))
    risk_unit = max(1.0, float(atr_pct or 0.0))
    risk_adjusted_momentum = momentum_pct / risk_unit
    risk_adjusted_trend = trend_pct / risk_unit
    weak_high_band_filter = (
        trend_up
        and (not trend_down)
        and (
            (trend_pct >= 6.0 and bb_pos >= 0.80 and attention_ratio < 1.12)
            or (trend_pct >= 6.0 and bb_pos >= 0.90 and value_spike_ratio < 1.08)
        )
    )
    late_chase_filter = (
        (bb_pos > 1.00)
        or (bb_pos >= 0.92 and attention_ratio < 1.12)
        or ((momentum_pct >= 6.0 or trend_pct >= 6.0) and attention_ratio < 1.12)
        or ((momentum_pct >= 6.0 or trend_pct >= 6.0) and value_spike_ratio < 1.10)
    )
    overextended_continuation_filter = (
        trend_up
        and (not trend_down)
        and (
            (bb_pos >= 0.90 and momentum_pct >= 10.0 and trend_pct >= 5.0)
            or (bb_pos >= 0.88 and daily_rsi >= 72.0 and attention_ratio < 1.28)
            or (bb_pos >= 0.78 and momentum_pct >= 6.0 and trend_pct >= 5.5 and value_spike_ratio < 1.18)
        )
    )
    strong_overextension_filter = (
        trend_up
        and (not trend_down)
        and momentum_pct >= 11.0
        and trend_pct >= 6.0
        and daily_rsi >= 75.0
    )
    mid_band_late_chase_filter = (
        trend_up
        and (not trend_down)
        and breakout_ok
        and momentum_pct >= 9.0
        and bb_pos >= 0.68
        and trend_pct <= 4.2
        and (attention_ratio < 1.22 or value_spike_ratio < 1.18)
    )
    mid_band_continuation_filter = (
        trend_up
        and (not trend_down)
        and breakout_ok
        and momentum_pct >= 8.0
        and trend_pct <= 4.0
        and bb_pos >= 0.68
        and daily_rsi <= 64.0
        and (attention_ratio < 1.55 or value_spike_ratio < 1.40)
    )
    weak_breakout_filter = (
        trend_up
        and (not trend_down)
        and breakout_ok
        and bb_pos >= 0.82
        and trend_pct >= 3.5
        and momentum_pct <= 2.5
        and daily_rsi < 61.0
        and value_spike_ratio < 1.35
    )
    weak_torque_filter = (
        trend_up
        and (not trend_down)
        and momentum_pct >= 8.0
        and bb_pos >= 0.62
        and value_spike_ratio < 1.08
        and attention_ratio < 1.30
    )
    residual_mid_band_continuation_filter = (
        trend_up
        and (not trend_down)
        and momentum_pct >= 9.0
        and trend_pct <= 3.8
        and bb_pos >= 0.70
        and daily_rsi <= 64.0
        and attention_ratio < 1.55
        and value_spike_ratio < 1.45
    )
    residual_weak_torque_filter = (
        trend_up
        and (not trend_down)
        and momentum_pct >= 8.5
        and trend_pct <= 2.5
        and bb_pos >= 0.62
        and value_spike_ratio < 1.08
        and attention_ratio < 1.30
    )
    high_rsi_upper_band_filter = (
        trend_up
        and (not trend_down)
        and breakout_ok
        and daily_rsi >= 72.0
        and bb_pos >= 0.84
    )
    low_attention_continuation_filter = (
        trend_up
        and (not trend_down)
        and momentum_pct >= 9.0
        and attention_ratio < 1.20
    )
    market_surge_chase_filter = (
        market_chg_pct >= 1.0
        and momentum_pct >= 6.0
        and trend_pct <= 3.5
        and value_spike_ratio < 1.20
    )
    noisy_momentum_filter = (
        trend_up
        and (not trend_down)
        and momentum_pct >= 6.0
        and risk_adjusted_momentum < 1.80
        and (attention_ratio < 1.30 or value_spike_ratio < 1.25)
    )
    inefficient_trend_filter = (
        trend_up
        and (not trend_down)
        and breakout_ok
        and momentum_pct >= 5.0
        and risk_adjusted_trend < 0.48
        and attention_ratio < 1.28
    )
    shock_reversal_filter = (
        trend_up
        and (not trend_down)
        and momentum_pct >= 6.0
        and risk_adjusted_momentum >= 3.0
        and risk_adjusted_trend < 0.36
        and value_spike_ratio >= 1.10
    )
    event_spike_exhaustion_filter = (
        trend_up
        and (not trend_down)
        and risk_adjusted_momentum >= 4.5
        and risk_adjusted_trend < 0.28
        and (
            attention_ratio >= 1.50
            or value_spike_ratio >= 2.20
        )
    )
    shock_reversal_filter = (
        trend_up
        and (not trend_down)
        and momentum_pct >= 6.0
        and risk_adjusted_momentum >= 3.0
        and risk_adjusted_trend < 0.36
        and value_spike_ratio >= 1.10
    )
    event_spike_exhaustion_filter = (
        trend_up
        and (not trend_down)
        and risk_adjusted_momentum >= 4.5
        and risk_adjusted_trend < 0.28
        and (
            attention_ratio >= 1.50
            or value_spike_ratio >= 2.20
        )
    )
    optimized_signal = decide_action_optimized(
        prev_price=prev_price,
        current_price=current_price,
        momentum_pct=momentum_pct,
        trend_pct=trend_pct,
        enable_trend_entry=True,
        breakout_buy_pct=0.70,
        pullback_sell_pct=-1.23,
        atr_pct=atr_pct,
        volume_ratio=volume_ratio,
    )
    blockers: list[str] = []
    if not trend_ok:
        blockers.append("trend")
    if not (structure_ok or breakout_ok):
        blockers.append("structure_or_breakout")
    if overheat_flag:
        blockers.append("overheat")
    if daily_rsi < trend_daily_rsi_min:
        blockers.append("daily_rsi_low")
    if daily_rsi > trend_daily_rsi_max:
        blockers.append("daily_rsi_high")
    if attention_ratio < max(0.95, trend_min_turnover_ratio_5_to_20 - 0.15):
        blockers.append("attention_low")
    if value_spike_ratio < max(0.95, trend_min_value_spike_ratio - 0.20):
        blockers.append("value_spike_low")
    if gap_from_prev_close_pct < trend_gap_skip_down_pct:
        blockers.append("gap_down_skip")
    if gap_from_prev_close_pct >= (trend_gap_skip_up_pct + 1.5):
        blockers.append("gap_up_skip")
    if gap_from_prev_close_pct > (trend_max_chase_from_open_pct + 2.0):
        blockers.append("chase_from_open")
    if trend_pct < -0.5:
        blockers.append("trend_pct_low")
    filter_flags = {
        "weak_high_band": weak_high_band_filter,
        "late_chase": late_chase_filter,
        "overextended_continuation": overextended_continuation_filter,
        "strong_overextension": strong_overextension_filter,
        "mid_band_late_chase": mid_band_late_chase_filter,
        "mid_band_continuation": mid_band_continuation_filter,
        "weak_breakout": weak_breakout_filter,
        "weak_torque": weak_torque_filter,
        "residual_mid_band_continuation": residual_mid_band_continuation_filter,
        "residual_weak_torque": residual_weak_torque_filter,
        "high_rsi_upper_band": high_rsi_upper_band_filter,
        "low_attention_continuation": low_attention_continuation_filter,
        "market_surge_chase": market_surge_chase_filter,
        "noisy_momentum": noisy_momentum_filter,
        "inefficient_trend": inefficient_trend_filter,
        "shock_reversal_risk": shock_reversal_filter,
        "event_spike_exhaustion": event_spike_exhaustion_filter,
    }
    blockers.extend(name for name, active in filter_flags.items() if active)
    watch_reason = ""
    if qty <= 0 and trend_ok and (structure_ok or breakout_ok) and not overheat_flag and trend_up and not trend_down:
        for name in [
            "high_rsi_upper_band",
            "strong_overextension",
            "overextended_continuation",
            "mid_band_late_chase",
            "mid_band_continuation",
            "weak_breakout",
            "weak_torque",
            "low_attention_continuation",
            "market_surge_chase",
            "noisy_momentum",
            "inefficient_trend",
            "shock_reversal_risk",
            "event_spike_exhaustion",
        ]:
            if filter_flags.get(name, False):
                watch_reason = name
                break
        if not watch_reason and daily_rsi >= max(68.0, trend_daily_rsi_max - 2.0):
            watch_reason = "extended_but_strong"
    watchlist = bool(watch_reason)
    market_type = "NEUTRAL"
    if strong_overextension_filter or high_rsi_upper_band_filter or overextended_continuation_filter:
        market_type = "OVEREXTENDED_MOMENTUM"
    elif market_surge_chase_filter:
        market_type = "SURGE_CHASE"
    elif trend_up and (structure_ok or breakout_ok) and attention_ratio >= 1.2 and value_spike_ratio >= 1.2:
        market_type = "HEALTHY_TREND"
    elif trend_up and not trend_down:
        market_type = "MIXED_TREND"
    elif trend_down:
        market_type = "WEAK_TAPE"
    return {
        "optimized_signal": optimized_signal,
        "trend_up": trend_up,
        "trend_down": trend_down,
        "bb_pos": bb_pos,
        "blockers": blockers,
        "watchlist": watchlist,
        "watch_reason": watch_reason,
        "market_type": market_type,
        "risk_adjusted_momentum": round(float(risk_adjusted_momentum), 3),
        "risk_adjusted_trend": round(float(risk_adjusted_trend), 3),
        "filter_flags": filter_flags,
    }


def trend_runtime_signal(
    *,
    qty: int,
    trend_ok: bool,
    structure_ok: bool,
    breakout_ok: bool,
    overheat_flag: bool,
    daily_rsi: float,
    attention_ratio: float,
    value_spike_ratio: float,
    gap_from_prev_close_pct: float,
    trend_daily_rsi_min: float,
    trend_daily_rsi_max: float,
    trend_min_turnover_ratio_5_to_20: float,
    trend_min_value_spike_ratio: float,
    trend_gap_skip_up_pct: float,
    trend_gap_skip_down_pct: float,
    trend_max_chase_from_open_pct: float,
    market_chg_pct: float,
    momentum_pct: float,
    trend_pct: float,
    tech_flags: dict[str, object],
    golden_cross_entry_bb_max: float,
    prev_price: float = 0.0,
    current_price: float = 0.0,
    atr_pct: float | None = None,
    volume_ratio: float | None = None,
) -> tuple[str, bool, bool]:
    trend_up = bool(tech_flags.get("trend_up", False))
    trend_down = bool(tech_flags.get("trend_down", False))
    bb_pos = float(tech_flags.get("bb_pos", 0.5))
    risk_unit = max(1.0, float(atr_pct or 0.0))
    risk_adjusted_momentum = momentum_pct / risk_unit
    risk_adjusted_trend = trend_pct / risk_unit
    weak_high_band_filter = (
        trend_up
        and (not trend_down)
        and (
            (trend_pct >= 6.0 and bb_pos >= 0.80 and attention_ratio < 1.12)
            or (trend_pct >= 6.0 and bb_pos >= 0.90 and value_spike_ratio < 1.08)
        )
    )
    late_chase_filter = (
        (bb_pos > 1.00)
        or (bb_pos >= 0.92 and attention_ratio < 1.12)
        or ((momentum_pct >= 6.0 or trend_pct >= 6.0) and attention_ratio < 1.12)
        or ((momentum_pct >= 6.0 or trend_pct >= 6.0) and value_spike_ratio < 1.10)
    )
    overextended_continuation_filter = (
        trend_up
        and (not trend_down)
        and (
            (
                bb_pos >= 0.90
                and momentum_pct >= 10.0
                and trend_pct >= 5.0
            )
            or (
                bb_pos >= 0.88
                and daily_rsi >= 72.0
                and attention_ratio < 1.28
            )
            or (
                bb_pos >= 0.78
                and momentum_pct >= 6.0
                and trend_pct >= 5.5
                and value_spike_ratio < 1.18
            )
        )
    )
    strong_overextension_filter = (
        trend_up
        and (not trend_down)
        and momentum_pct >= 11.0
        and trend_pct >= 6.0
        and daily_rsi >= 75.0
    )
    mid_band_late_chase_filter = (
        trend_up
        and (not trend_down)
        and breakout_ok
        and momentum_pct >= 9.0
        and bb_pos >= 0.68
        and trend_pct <= 4.2
        and (
            attention_ratio < 1.22
            or value_spike_ratio < 1.18
        )
    )
    mid_band_continuation_filter = (
        trend_up
        and (not trend_down)
        and breakout_ok
        and momentum_pct >= 8.0
        and trend_pct <= 4.0
        and bb_pos >= 0.68
        and daily_rsi <= 64.0
        and (
            attention_ratio < 1.55
            or value_spike_ratio < 1.40
        )
    )
    weak_breakout_filter = (
        trend_up
        and (not trend_down)
        and breakout_ok
        and bb_pos >= 0.82
        and trend_pct >= 3.5
        and momentum_pct <= 2.5
        and daily_rsi < 61.0
        and value_spike_ratio < 1.35
    )
    weak_torque_filter = (
        trend_up
        and (not trend_down)
        and momentum_pct >= 8.0
        and bb_pos >= 0.62
        and value_spike_ratio < 1.08
        and attention_ratio < 1.30
    )
    residual_mid_band_continuation_filter = (
        trend_up
        and (not trend_down)
        and momentum_pct >= 9.0
        and trend_pct <= 3.8
        and bb_pos >= 0.70
        and daily_rsi <= 64.0
        and attention_ratio < 1.55
        and value_spike_ratio < 1.45
    )
    residual_weak_torque_filter = (
        trend_up
        and (not trend_down)
        and momentum_pct >= 8.5
        and trend_pct <= 2.5
        and bb_pos >= 0.62
        and value_spike_ratio < 1.08
        and attention_ratio < 1.30
    )
    high_rsi_upper_band_filter = (
        trend_up
        and (not trend_down)
        and breakout_ok
        and daily_rsi >= 72.0
        and bb_pos >= 0.84
    )
    low_attention_continuation_filter = (
        trend_up
        and (not trend_down)
        and momentum_pct >= 9.0
        and attention_ratio < 1.20
    )
    market_surge_chase_filter = (
        market_chg_pct >= 1.0
        and momentum_pct >= 6.0
        and trend_pct <= 3.5
        and value_spike_ratio < 1.20
    )
    noisy_momentum_filter = (
        trend_up
        and (not trend_down)
        and momentum_pct >= 6.0
        and risk_adjusted_momentum < 1.80
        and (
            attention_ratio < 1.30
            or value_spike_ratio < 1.25
        )
    )
    inefficient_trend_filter = (
        trend_up
        and (not trend_down)
        and breakout_ok
        and momentum_pct >= 5.0
        and risk_adjusted_trend < 0.48
        and attention_ratio < 1.28
    )
    shock_reversal_filter = (
        trend_up
        and (not trend_down)
        and momentum_pct >= 6.0
        and risk_adjusted_momentum >= 3.0
        and risk_adjusted_trend < 0.36
        and value_spike_ratio >= 1.10
    )
    event_spike_exhaustion_filter = (
        trend_up
        and (not trend_down)
        and risk_adjusted_momentum >= 4.5
        and risk_adjusted_trend < 0.28
        and (
            attention_ratio >= 1.50
            or value_spike_ratio >= 2.20
        )
    )
    optimized_signal = decide_action_optimized(
        prev_price=prev_price,
        current_price=current_price,
        momentum_pct=momentum_pct,
        trend_pct=trend_pct,
        enable_trend_entry=True,
        breakout_buy_pct=0.70,
        pullback_sell_pct=-1.23,
        atr_pct=atr_pct,
        volume_ratio=volume_ratio,
    )
    trend_entry_ready = (
        trend_ok
        and (structure_ok or breakout_ok)
        and (not overheat_flag)
        and (trend_daily_rsi_min <= daily_rsi <= trend_daily_rsi_max)
        and (attention_ratio >= max(0.95, trend_min_turnover_ratio_5_to_20 - 0.15))
        and (value_spike_ratio >= max(0.95, trend_min_value_spike_ratio - 0.20))
        and (gap_from_prev_close_pct < (trend_gap_skip_up_pct + 1.5))
        and (gap_from_prev_close_pct >= trend_gap_skip_down_pct)
        and (gap_from_prev_close_pct <= (trend_max_chase_from_open_pct + 2.0))
        and (trend_pct >= -0.5)
        and (not weak_high_band_filter)
        and (not overextended_continuation_filter)
        and (not strong_overextension_filter)
        and (not mid_band_late_chase_filter)
        and (not mid_band_continuation_filter)
        and (not weak_breakout_filter)
        and (not weak_torque_filter)
        and (not residual_mid_band_continuation_filter)
        and (not residual_weak_torque_filter)
        and (not high_rsi_upper_band_filter)
        and (not low_attention_continuation_filter)
        and (not market_surge_chase_filter)
        and (not noisy_momentum_filter)
        and (not inefficient_trend_filter)
        and (not shock_reversal_filter)
        and (not event_spike_exhaustion_filter)
    )

    if qty <= 0:
        pullback_quality_ready = (
            trend_ok
            and (structure_ok or breakout_ok)
            and (not overheat_flag)
            and (trend_daily_rsi_min - 7.0 <= daily_rsi <= min(trend_daily_rsi_max, 72.0))
            and (attention_ratio >= max(0.92, trend_min_turnover_ratio_5_to_20 - 0.20))
            and (value_spike_ratio >= max(0.90, trend_min_value_spike_ratio - 0.25))
            and (gap_from_prev_close_pct < (trend_gap_skip_up_pct + 1.5))
            and (gap_from_prev_close_pct >= trend_gap_skip_down_pct)
            and (gap_from_prev_close_pct <= (trend_max_chase_from_open_pct + 2.0))
        )
        controlled_chase_buy_ready = (
            trend_ok
            and (structure_ok or breakout_ok)
            and breakout_ok
            and (not overheat_flag)
            and (not late_chase_filter)
            and (not strong_overextension_filter)
            and (not mid_band_late_chase_filter)
            and (not mid_band_continuation_filter)
            and (not weak_breakout_filter)
            and (not weak_torque_filter)
            and (not residual_mid_band_continuation_filter)
            and (not residual_weak_torque_filter)
            and (not high_rsi_upper_band_filter)
            and (not low_attention_continuation_filter)
            and (not shock_reversal_filter)
            and (not event_spike_exhaustion_filter)
            and trend_up
            and (not trend_down)
            and (0.78 <= bb_pos <= 0.98)
            and (60.0 <= daily_rsi <= min(trend_daily_rsi_max + 2.0, 79.5))
            and (2.5 <= momentum_pct <= 20.0)
            and (1.5 <= trend_pct <= 9.0)
            and not (bb_pos >= 0.95 and momentum_pct < 4.5)
            and not (bb_pos >= 0.95 and daily_rsi < 65.0)
            and (attention_ratio >= max(0.98, trend_min_turnover_ratio_5_to_20 - 0.10))
            and (value_spike_ratio >= max(1.00, trend_min_value_spike_ratio - 0.10))
            and (
                bool(tech_flags.get("volume_spike"))
                or attention_ratio >= max(1.20, trend_min_turnover_ratio_5_to_20 + 0.05)
            )
            and optimized_signal != "SELL"
            and (not shock_reversal_filter)
            and (not event_spike_exhaustion_filter)
        )
        pullback_recovery_buy_ready = (
            trend_up
            and (not trend_down)
            and pullback_quality_ready
            and (0.10 <= bb_pos <= 0.82)
            and (46.0 <= daily_rsi <= min(trend_daily_rsi_max, 72.0))
            and (-2.50 <= momentum_pct <= 7.50)
            and (-1.80 <= trend_pct <= 6.50)
            and (
                bool(tech_flags.get("short_bottom"))
                or bool(tech_flags.get("golden_cross"))
                or bool(tech_flags.get("near_lower"))
                or bb_pos <= 0.55
            )
            and (
                bool(tech_flags.get("volume_spike"))
                or bool(tech_flags.get("near_lower"))
                or attention_ratio >= max(1.00, trend_min_turnover_ratio_5_to_20 - 0.05)
            )
            and optimized_signal != "SELL"
        )
        weak_follow_through = (
            momentum_pct < 4.0
            and daily_rsi < 62.0
            and bb_pos < 0.72
            and trend_pct < 3.5
        )
        tech_buy_ready = (
            (
                trend_up
                and (not trend_down)
                and bool(tech_flags.get("short_bottom"))
                and bool(tech_flags.get("volume_spike"))
                and (bool(tech_flags.get("golden_cross")) or bb_pos <= 0.22)
            )
            or (
                (not trend_down)
                and bool(tech_flags.get("short_bottom"))
                and bool(tech_flags.get("volume_spike"))
                and bb_pos <= 0.16
            )
            or (
                trend_up
                and (not trend_down)
                and bool(tech_flags.get("golden_cross"))
                and bool(tech_flags.get("volume_spike"))
                and (0.30 <= bb_pos <= golden_cross_entry_bb_max)
            )
            or (
                (not trend_down)
                and bb_pos <= 0.45
                and momentum_pct > -1.5
                and bool(tech_flags.get("short_bottom"))
            )
            or (
                trend_up
                and (not trend_down)
                and bb_pos <= 0.58
                and momentum_pct >= 0.80
                and trend_pct >= 0.20
                and (
                    bool(tech_flags.get("volume_spike"))
                    or bool(tech_flags.get("golden_cross"))
                )
            )
        )
        continuation_buy_ready = (
            trend_up
            and (not trend_down)
            and breakout_ok
            and (0.38 <= bb_pos <= 0.78)
            and (0.30 <= momentum_pct <= 5.50)
            and (0.00 <= trend_pct <= 5.00)
            and daily_rsi <= min(trend_daily_rsi_max + 1.5, 68.0)
            and bool(tech_flags.get("volume_spike"))
            and optimized_signal != "SELL"
        )
        strong_trend_continuation_ready = (
            trend_up
            and (not trend_down)
            and trend_entry_ready
            and (structure_ok or breakout_ok)
            and (not overheat_flag)
            and (not strong_overextension_filter)
            and (not mid_band_late_chase_filter)
            and (not mid_band_continuation_filter)
            and (not weak_breakout_filter)
            and (not weak_torque_filter)
            and (not residual_mid_band_continuation_filter)
            and (not residual_weak_torque_filter)
            and (not high_rsi_upper_band_filter)
            and (not low_attention_continuation_filter)
            and (0.48 <= bb_pos <= 0.95)
            and (56.0 <= daily_rsi <= min(trend_daily_rsi_max + 1.5, 74.5))
            and (0.20 <= momentum_pct <= 10.50)
            and (0.10 <= trend_pct <= 7.50)
            and (attention_ratio >= max(1.00, trend_min_turnover_ratio_5_to_20 - 0.10))
            and (value_spike_ratio >= max(0.98, trend_min_value_spike_ratio - 0.10))
            and (not shock_reversal_filter)
            and (not event_spike_exhaustion_filter)
            and optimized_signal != "SELL"
        )
        continuation_fast_confirm = (
            trend_up
            and (not trend_down)
            and breakout_ok
            and (not strong_overextension_filter)
            and (not mid_band_late_chase_filter)
            and (not mid_band_continuation_filter)
            and (not weak_breakout_filter)
            and (not weak_torque_filter)
            and (not residual_mid_band_continuation_filter)
            and (not residual_weak_torque_filter)
            and (not high_rsi_upper_band_filter)
            and (not low_attention_continuation_filter)
            and (0.42 <= bb_pos <= 0.82)
            and (0.70 <= momentum_pct <= 6.50)
            and (0.10 <= trend_pct <= 5.00)
            and daily_rsi <= min(trend_daily_rsi_max + 1.5, 69.0)
            and (
                bool(tech_flags.get("volume_spike"))
                or bool(tech_flags.get("golden_cross"))
            )
            and optimized_signal != "SELL"
        )
        strict_tech_buy_ready = (
            tech_buy_ready
            and bb_pos <= 0.62
            and daily_rsi <= min(trend_daily_rsi_max + 2.0, 70.0)
            and (-1.5 <= momentum_pct <= 4.5)
        )
        fast_confirm_buy_ready = (
            strict_tech_buy_ready
            or pullback_recovery_buy_ready
            or continuation_fast_confirm
            or strong_trend_continuation_ready
            or controlled_chase_buy_ready
        )
        if trend_entry_ready and (
            pullback_recovery_buy_ready
            or strict_tech_buy_ready
            or continuation_buy_ready
            or strong_trend_continuation_ready
            or controlled_chase_buy_ready
        ):
            if weak_follow_through and not strict_tech_buy_ready:
                return "HOLD", False, trend_entry_ready
            return "BUY", fast_confirm_buy_ready, trend_entry_ready
        return "HOLD", False, trend_entry_ready

    tech_sell_ready = (
        (
            bool(tech_flags.get("short_top"))
            and bool(tech_flags.get("volume_spike"))
            and bool(tech_flags.get("death_cross"))
        )
        or (bool(tech_flags.get("death_cross")) and bool(tech_flags.get("near_upper")))
        or (trend_down and bb_pos >= 0.60)
        or optimized_signal == "SELL"
    )
    if tech_sell_ready:
        return "SELL", True, trend_entry_ready
    return "HOLD", False, trend_entry_ready


def bearish_long_exception_ready(
    *,
    trend_ok: bool,
    structure_ok: bool,
    breakout_ok: bool,
    daily_rsi: float,
    attention_ratio: float,
    value_spike_ratio: float,
    momentum_pct: float,
    trend_pct: float,
    tech_flags: dict[str, object],
) -> bool:
    bb_pos = float(tech_flags.get("bb_pos", 0.5))
    return (
        trend_ok
        and (structure_ok or breakout_ok)
        and bool(tech_flags.get("trend_up", False))
        and (not bool(tech_flags.get("trend_down", False)))
        and bool(tech_flags.get("volume_spike", False))
        and (bool(tech_flags.get("golden_cross", False)) or bb_pos <= 0.82)
        and (0.45 <= bb_pos <= 0.80)
        and (62.0 <= daily_rsi <= 68.0)
        and (attention_ratio >= 1.25)
        and (value_spike_ratio >= 1.40)
        and (8.0 <= momentum_pct <= 12.5)
        and (2.0 <= trend_pct <= 4.5)
    )
