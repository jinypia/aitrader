"""
Market Strategy Fit Module

Evaluates how well the current market conditions align with each available
trading strategy (scalping and market-sentiment / trend-following) and
produces a side-by-side comparison together with economist-style commentary
on what macro signals suggest about improving or switching strategies.

Usage
-----
>>> from market_strategy_fit import MarketCondition, compare_strategies
>>> condition = MarketCondition(
...     daily_rsi=52.0,
...     intraday_volatility_pct=0.8,
...     momentum_pct=0.4,
...     trend_pct=0.6,
...     volume_ratio=1.5,
...     market_regime="BULLISH",
... )
>>> result = compare_strategies(condition)
>>> print(result["recommendation"])
"""

from __future__ import annotations

from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Market Condition Description
# ---------------------------------------------------------------------------


@dataclass
class MarketCondition:
    """Snapshot of observable market indicators used to score strategy fit.

    Parameters
    ----------
    daily_rsi:
        Relative Strength Index on the daily timeframe (0–100).
        Values near 30 indicate oversold; values near 70 indicate overbought.
    intraday_volatility_pct:
        Average intraday price swing expressed as a percentage of price
        (e.g. 0.8 means prices swing ~0.8% intraday).  A rough proxy for
        the Average True Range (ATR) on short bars.
    momentum_pct:
        Price change over the last few sessions as a percentage (positive =
        upward pressure, negative = downward pressure).
    trend_pct:
        Longer-term trend strength measured as (MA5 - MA20) / MA20 × 100.
        Positive means the short MA is above the long MA (uptrend).
    volume_ratio:
        Latest session volume divided by the rolling average volume.
        A value of 1.0 means average participation.
    market_regime:
        Qualitative market regime label.  One of ``"BULLISH"``,
        ``"BEARISH"``, ``"NEUTRAL"``, or ``"SIDEWAYS"``.
    vix_level:
        CBOE Volatility Index (fear gauge). 0.0 means not available.
        Values below 15 indicate complacency; above 30 indicate high fear.
    yield_curve_spread_bps:
        10-year minus 2-year Treasury yield spread in basis points.
        Negative values (inversion) historically precede recessions.
        0.0 means not available.
    credit_spread_bps:
        Investment-grade corporate credit spread over Treasuries in basis
        points.  Rising spreads signal tightening credit conditions.
        0.0 means not available.
    """

    daily_rsi: float
    intraday_volatility_pct: float
    momentum_pct: float
    trend_pct: float
    volume_ratio: float
    market_regime: str = "NEUTRAL"
    vix_level: float = 0.0
    yield_curve_spread_bps: float = 0.0
    credit_spread_bps: float = 0.0


# ---------------------------------------------------------------------------
# Individual Strategy Evaluators
# ---------------------------------------------------------------------------


def evaluate_scalping_fit(condition: MarketCondition) -> dict:
    """Score how well *condition* suits the intraday scalping strategy.

    The scalping strategy (``scalping_strategy.py``) operates on 2-minute
    bars and requires:

    * RSI in a tradeable mid-range (not near extremes where moves stall).
    * Intraday volatility sufficient to cross profit-target / stop thresholds
      without being so wide that stops are triggered on noise.
    * Volume at least 1.2× the baseline for adequate liquidity.
    * No dominant macro directional trend that would invalidate quick
      mean-reverting micro-moves.

    Returns
    -------
    dict with keys:
        ``score``    – integer 0–100
        ``grade``    – letter grade A / B / C / D / F
        ``factors``  – list of (label, points_earned, max_points, note) tuples
        ``summary``  – human-readable text
    """
    factors: list[tuple[str, float, float, str]] = []

    # ── RSI in mid-range (max 30 pts) ──────────────────────────────────────
    rsi = condition.daily_rsi
    if 35.0 <= rsi <= 65.0:
        rsi_pts = 30.0
        rsi_note = f"RSI {rsi:.1f} is in the ideal mid-range for scalping"
    elif (25.0 <= rsi < 35.0) or (65.0 < rsi <= 75.0):
        rsi_pts = 18.0
        rsi_note = f"RSI {rsi:.1f} is approaching extremes — entries trickier"
    else:
        rsi_pts = 5.0
        rsi_note = f"RSI {rsi:.1f} is at an extreme — mean-reversion risk is high"
    factors.append(("RSI range", rsi_pts, 30.0, rsi_note))

    # ── Intraday volatility (max 25 pts) ───────────────────────────────────
    vol = condition.intraday_volatility_pct
    if 0.3 <= vol <= 1.5:
        vol_pts = 25.0
        vol_note = f"Intraday range {vol:.2f}% matches scalp profit/stop bands"
    elif (0.15 <= vol < 0.3) or (1.5 < vol <= 2.5):
        vol_pts = 15.0
        vol_note = f"Intraday range {vol:.2f}% is marginal for scalping"
    elif vol > 2.5:
        vol_pts = 8.0
        vol_note = f"Intraday range {vol:.2f}% is too wide — stops get hit by noise"
    else:
        vol_pts = 5.0
        vol_note = f"Intraday range {vol:.2f}% is too low — moves won't cover spread"
    factors.append(("Intraday volatility", vol_pts, 25.0, vol_note))

    # ── Volume (max 20 pts) ────────────────────────────────────────────────
    vr = condition.volume_ratio
    if vr >= 1.5:
        vol_r_pts = 20.0
        vol_r_note = f"Volume ratio {vr:.2f}× — excellent liquidity for quick fills"
    elif vr >= 1.2:
        vol_r_pts = 15.0
        vol_r_note = f"Volume ratio {vr:.2f}× — adequate participation"
    elif vr >= 0.9:
        vol_r_pts = 8.0
        vol_r_note = f"Volume ratio {vr:.2f}× — thin liquidity may cause slippage"
    else:
        vol_r_pts = 3.0
        vol_r_note = f"Volume ratio {vr:.2f}× — dangerously illiquid for scalping"
    factors.append(("Volume ratio", vol_r_pts, 20.0, vol_r_note))

    # ── Market regime (max 15 pts) ─────────────────────────────────────────
    regime = condition.market_regime.upper()
    if regime in ("SIDEWAYS", "NEUTRAL"):
        reg_pts = 15.0
        reg_note = f"Regime '{regime}' — choppy price action suits mean-reversion scalping"
    elif regime == "BULLISH":
        reg_pts = 8.0
        reg_note = "Bullish regime — strong trend may override micro-reversal signals"
    elif regime == "BEARISH":
        reg_pts = 6.0
        reg_note = "Bearish regime — downward momentum raises stop-hit rate"
    else:
        reg_pts = 10.0
        reg_note = f"Regime '{regime}' — unknown label, assuming partial suitability"
    factors.append(("Market regime", reg_pts, 15.0, reg_note))

    # ── Momentum (max 10 pts) ──────────────────────────────────────────────
    mom = abs(condition.momentum_pct)
    if mom <= 0.5:
        mom_pts = 10.0
        mom_note = f"Momentum {condition.momentum_pct:+.2f}% — minimal bias, ideal for scalping both sides"
    elif mom <= 1.5:
        mom_pts = 7.0
        mom_note = f"Momentum {condition.momentum_pct:+.2f}% — directional lean, favour momentum-aligned scalps"
    else:
        mom_pts = 3.0
        mom_note = f"Momentum {condition.momentum_pct:+.2f}% — strong directional bias; counter-trend scalps are risky"
    factors.append(("Momentum bias", mom_pts, 10.0, mom_note))

    score = int(round(sum(f[1] for f in factors)))
    grade = _score_to_grade(score)
    summary = (
        f"Scalping strategy fit: {grade} ({score}/100). "
        + _grade_to_adjective(grade)
        + " market for intraday scalping."
    )
    return {"score": score, "grade": grade, "factors": factors, "summary": summary}


def evaluate_sentiment_fit(condition: MarketCondition) -> dict:
    """Score how well *condition* suits the market-sentiment / trend strategy.

    The sentiment strategy (``strategy.py`` — ``decide_action_optimized``)
    works on daily/hourly bars and requires:

    * A clear, confirmed market regime (BULLISH or BEARISH).
    * Sustained momentum to carry positions through intraday swings.
    * A positive trend confirmation (MA crossover, Bollinger position).
    * Sufficient volume to confirm institutional participation.
    * RSI that corroborates the directional bias.

    Returns
    -------
    dict with same keys as :func:`evaluate_scalping_fit`.
    """
    factors: list[tuple[str, float, float, str]] = []

    # ── Market regime (max 30 pts) ─────────────────────────────────────────
    regime = condition.market_regime.upper()
    if regime in ("BULLISH", "BEARISH"):
        reg_pts = 30.0
        reg_note = f"Regime '{regime}' — clear directional trend ideal for sentiment strategy"
    elif regime == "NEUTRAL":
        reg_pts = 12.0
        reg_note = "Neutral regime — weak trend conviction reduces signal quality"
    elif regime == "SIDEWAYS":
        reg_pts = 5.0
        reg_note = "Sideways regime — no persistent direction; sentiment signals whipsaw"
    else:
        reg_pts = 15.0
        reg_note = f"Regime '{regime}' — unknown label, assuming moderate suitability"
    factors.append(("Market regime", reg_pts, 30.0, reg_note))

    # ── Momentum (max 25 pts) ──────────────────────────────────────────────
    mom = condition.momentum_pct
    abs_mom = abs(mom)
    if abs_mom >= 2.0:
        mom_pts = 25.0
        mom_note = f"Momentum {mom:+.2f}% — strong sustained move supports trend entries"
    elif abs_mom >= 0.8:
        mom_pts = 18.0
        mom_note = f"Momentum {mom:+.2f}% — moderate drift, follow-through likely"
    elif abs_mom >= 0.3:
        mom_pts = 10.0
        mom_note = f"Momentum {mom:+.2f}% — weak drift; confirm with volume before entry"
    else:
        mom_pts = 3.0
        mom_note = f"Momentum {mom:+.2f}% — stall detected; avoid new trend positions"
    factors.append(("Sustained momentum", mom_pts, 25.0, mom_note))

    # ── Trend strength (max 25 pts) ────────────────────────────────────────
    trend = condition.trend_pct
    abs_trend = abs(trend)
    if abs_trend >= 1.0:
        trend_pts = 25.0
        trend_note = f"Trend {trend:+.2f}% — MA confirmation strong; breakout setups viable"
    elif abs_trend >= 0.3:
        trend_pts = 18.0
        trend_note = f"Trend {trend:+.2f}% — moderate MA divergence"
    elif abs_trend >= 0.1:
        trend_pts = 10.0
        trend_note = f"Trend {trend:+.2f}% — minimal MA separation; trend not yet established"
    else:
        trend_pts = 4.0
        trend_note = f"Trend {trend:+.2f}% — flat MAs; no structural edge"
    factors.append(("Trend strength (MA)", trend_pts, 25.0, trend_note))

    # ── RSI alignment with regime (max 10 pts) ────────────────────────────
    rsi = condition.daily_rsi
    if regime == "BULLISH" and 50.0 <= rsi <= 72.0:
        rsi_pts = 10.0
        rsi_note = f"RSI {rsi:.1f} in bullish continuation zone"
    elif regime == "BEARISH" and 28.0 <= rsi <= 50.0:
        rsi_pts = 10.0
        rsi_note = f"RSI {rsi:.1f} in bearish continuation zone"
    elif 40.0 <= rsi <= 60.0:
        rsi_pts = 6.0
        rsi_note = f"RSI {rsi:.1f} neutral — no strong confirmation"
    else:
        rsi_pts = 3.0
        rsi_note = f"RSI {rsi:.1f} misaligned with current regime"
    factors.append(("RSI / regime alignment", rsi_pts, 10.0, rsi_note))

    # ── Volume (max 10 pts) ────────────────────────────────────────────────
    vr = condition.volume_ratio
    if vr >= 1.3:
        vol_pts = 10.0
        vol_note = f"Volume ratio {vr:.2f}× — institutional participation confirmed"
    elif vr >= 1.0:
        vol_pts = 7.0
        vol_note = f"Volume ratio {vr:.2f}× — adequate volume"
    else:
        vol_pts = 3.0
        vol_note = f"Volume ratio {vr:.2f}× — below-average volume weakens signal"
    factors.append(("Volume confirmation", vol_pts, 10.0, vol_note))

    score = int(round(sum(f[1] for f in factors)))
    grade = _score_to_grade(score)
    summary = (
        f"Sentiment/trend strategy fit: {grade} ({score}/100). "
        + _grade_to_adjective(grade)
        + " market for trend-following."
    )
    return {"score": score, "grade": grade, "factors": factors, "summary": summary}


# ---------------------------------------------------------------------------
# Economist Commentary
# ---------------------------------------------------------------------------


def economist_commentary(condition: MarketCondition) -> list[str]:
    """Generate plain-language macro commentary based on available indicators.

    Incorporates optional macro signals (VIX, yield curve, credit spreads)
    that reflect how economists and macro analysts assess equity risk.

    Returns
    -------
    list[str]
        Each string is a standalone observation or recommendation.
    """
    notes: list[str] = []

    # ── VIX / fear gauge ──────────────────────────────────────────────────
    vix = condition.vix_level
    if vix > 0:
        if vix >= 40:
            notes.append(
                f"VIX {vix:.1f}: Extreme fear — markets are in panic mode. "
                "Scalping spreads widen sharply; consider pausing new positions "
                "until the VIX subsides below 30."
            )
        elif vix >= 30:
            notes.append(
                f"VIX {vix:.1f}: Elevated volatility — institutional selling is active. "
                "Scalping can still capture intraday spikes, but tighten stop-loss "
                "bands. Trend strategy faces higher whipsaw risk."
            )
        elif vix >= 20:
            notes.append(
                f"VIX {vix:.1f}: Moderately elevated — some nervousness in markets. "
                "Both strategies remain viable; monitor overnight gap risk."
            )
        elif vix >= 15:
            notes.append(
                f"VIX {vix:.1f}: Normal range — market is in a measured risk environment. "
                "Trend-following conditions are generally constructive."
            )
        else:
            notes.append(
                f"VIX {vix:.1f}: Low volatility / complacency regime. "
                "Scalping profits shrink as intraday ranges compress. "
                "Economists often flag low-VIX periods as precursors to sudden "
                "volatility spikes — keep position sizes conservative."
            )

    # ── Yield curve ────────────────────────────────────────────────────────
    ycs = condition.yield_curve_spread_bps
    if ycs != 0.0:
        if ycs < -50:
            notes.append(
                f"Yield curve deeply inverted ({ycs:+.0f} bps): "
                "Historically the most reliable recession indicator. "
                "Economists recommend reducing equity exposure and rotating to "
                "defensive sectors. Trend strategy performance typically "
                "deteriorates 6–18 months after deep inversion."
            )
        elif ycs < 0:
            notes.append(
                f"Yield curve inverted ({ycs:+.0f} bps): "
                "Inversion signals that markets expect the central bank to cut "
                "rates in response to a slowing economy. "
                "Bearish for the broader trend; consider tightening profit "
                "targets and reducing sizing."
            )
        elif ycs < 50:
            notes.append(
                f"Yield curve near flat ({ycs:+.0f} bps): "
                "Flat yield curve suggests economic uncertainty. "
                "Both strategies should operate with reduced leverage."
            )
        else:
            notes.append(
                f"Yield curve positive ({ycs:+.0f} bps): "
                "Healthy slope indicates expansionary credit conditions — "
                "constructive for sustained trend-following strategies."
            )

    # ── Credit spreads ─────────────────────────────────────────────────────
    cs = condition.credit_spread_bps
    if cs > 0:
        if cs >= 300:
            notes.append(
                f"Credit spreads very wide ({cs:.0f} bps): "
                "Distressed-credit territory — liquidity risk is elevated. "
                "Scalping may benefit from volatility, but execution quality "
                "degrades significantly. Economists flag this as a systemic risk signal."
            )
        elif cs >= 150:
            notes.append(
                f"Credit spreads elevated ({cs:.0f} bps): "
                "Risk-off environment; corporate borrowing costs are rising. "
                "Tighten stops across both strategies and reduce position sizes."
            )
        else:
            notes.append(
                f"Credit spreads normal ({cs:.0f} bps): "
                "Credit conditions are benign — supportive backdrop for equity strategies."
            )

    # ── Structural advice based on regime + macro ─────────────────────────
    regime = condition.market_regime.upper()
    if regime == "BEARISH" and vix > 25 and (ycs < 0 or cs >= 150):
        notes.append(
            "Multiple macro warning signals are aligned (bearish regime + high VIX "
            "+ stressed fixed income). Economists' consensus would be to reduce "
            "gross exposure and focus on capital preservation over alpha generation."
        )

    if not notes:
        notes.append(
            "No macro override indicators provided (VIX, yield curve, credit spreads). "
            "Strategy selection is based purely on price/volume signals. "
            "Incorporating macro data would allow the model to weight long-term "
            "economist views alongside technical signals."
        )

    return notes


# ---------------------------------------------------------------------------
# Composite Comparison
# ---------------------------------------------------------------------------


def compare_strategies(condition: MarketCondition) -> dict:
    """Compare scalping vs sentiment strategy fit and recommend the better one.

    Parameters
    ----------
    condition:
        Current market conditions.

    Returns
    -------
    dict with keys:
        ``scalping``       – result from :func:`evaluate_scalping_fit`
        ``sentiment``      – result from :func:`evaluate_sentiment_fit`
        ``recommendation`` – which strategy is preferred and why
        ``economist_notes``– list of macro-informed observations
        ``score_delta``    – sentiment_score − scalping_score (positive =
                             sentiment preferred, negative = scalping preferred)
    """
    scalping = evaluate_scalping_fit(condition)
    sentiment = evaluate_sentiment_fit(condition)
    eco_notes = economist_commentary(condition)

    scalp_score = scalping["score"]
    sent_score = sentiment["score"]
    delta = sent_score - scalp_score

    if abs(delta) <= 5:
        preferred = "BOTH (scores within 5 points)"
        reasoning = (
            f"Both strategies are similarly suited to current conditions "
            f"(scalping {scalp_score}, sentiment {sent_score}). "
            "Consider running both with smaller individual position sizes."
        )
    elif delta > 0:
        preferred = "SENTIMENT / TREND"
        reasoning = (
            f"The sentiment/trend strategy scores {sent_score} vs scalping's "
            f"{scalp_score} (+{delta} points). A confirmed {condition.market_regime} "
            "regime with measurable momentum provides a structural edge for "
            "longer-hold, trend-following entries over ultra-short scalps."
        )
    else:
        preferred = "SCALPING"
        reasoning = (
            f"The scalping strategy scores {scalp_score} vs sentiment's "
            f"{sent_score} ({delta} points). Market conditions lack sufficient "
            "directional conviction for trend-following; intraday mean-reversion "
            "scalping offers a more reliable edge in the current environment."
        )

    return {
        "scalping": scalping,
        "sentiment": sentiment,
        "recommendation": preferred,
        "reasoning": reasoning,
        "economist_notes": eco_notes,
        "score_delta": delta,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _score_to_grade(score: int) -> str:
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 55:
        return "C"
    if score >= 40:
        return "D"
    return "F"


def _grade_to_adjective(grade: str) -> str:
    return {
        "A": "Excellent",
        "B": "Good",
        "C": "Fair",
        "D": "Poor",
        "F": "Unfavourable",
    }.get(grade, "Unknown")
