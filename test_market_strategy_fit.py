"""
Tests for the market_strategy_fit module.

Validates all scoring paths, edge cases, and the composite comparison for
both the scalping and sentiment/trend-following strategy evaluators.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from market_strategy_fit import (
    MarketCondition,
    compare_strategies,
    economist_commentary,
    evaluate_scalping_fit,
    evaluate_sentiment_fit,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_condition(**kwargs) -> MarketCondition:
    defaults = dict(
        daily_rsi=50.0,
        intraday_volatility_pct=0.8,
        momentum_pct=0.3,
        trend_pct=0.5,
        volume_ratio=1.3,
        market_regime="NEUTRAL",
    )
    defaults.update(kwargs)
    return MarketCondition(**defaults)


# ---------------------------------------------------------------------------
# MarketCondition
# ---------------------------------------------------------------------------


def test_market_condition_defaults() -> None:
    c = MarketCondition(
        daily_rsi=55.0,
        intraday_volatility_pct=1.0,
        momentum_pct=0.5,
        trend_pct=0.3,
        volume_ratio=1.2,
    )
    assert c.market_regime == "NEUTRAL"
    assert c.vix_level == 0.0
    assert c.yield_curve_spread_bps == 0.0
    assert c.credit_spread_bps == 0.0


# ---------------------------------------------------------------------------
# evaluate_scalping_fit
# ---------------------------------------------------------------------------


def test_scalping_ideal_sideways_market() -> None:
    """Sideways + moderate volatility + good volume = high scalping score."""
    condition = _make_condition(
        daily_rsi=50.0,
        intraday_volatility_pct=0.8,
        momentum_pct=0.2,
        volume_ratio=1.6,
        market_regime="SIDEWAYS",
    )
    result = evaluate_scalping_fit(condition)
    assert result["score"] >= 75, f"Expected high score, got {result['score']}"
    assert result["grade"] in ("A", "B")
    assert isinstance(result["factors"], list)
    assert len(result["factors"]) == 5
    assert isinstance(result["summary"], str)


def test_scalping_poor_strong_trend() -> None:
    """Strong BULLISH trend + low volatility = poor scalping fit."""
    condition = _make_condition(
        daily_rsi=72.0,
        intraday_volatility_pct=0.1,
        momentum_pct=3.5,
        volume_ratio=0.7,
        market_regime="BULLISH",
    )
    result = evaluate_scalping_fit(condition)
    assert result["score"] <= 45, f"Expected low score, got {result['score']}"
    assert result["grade"] in ("D", "F")


def test_scalping_extreme_rsi_penalised() -> None:
    """RSI at extreme (>75) receives fewer points than mid-range."""
    mid = evaluate_scalping_fit(_make_condition(daily_rsi=50.0))
    high = evaluate_scalping_fit(_make_condition(daily_rsi=82.0))
    assert mid["score"] > high["score"]


def test_scalping_high_volatility_penalised() -> None:
    """Intraday volatility above 2.5% lowers the scalping score."""
    normal = evaluate_scalping_fit(_make_condition(intraday_volatility_pct=0.8))
    extreme = evaluate_scalping_fit(_make_condition(intraday_volatility_pct=3.5))
    assert normal["score"] > extreme["score"]


def test_scalping_low_volume_penalised() -> None:
    """Volume ratio below 0.9 should significantly reduce the score."""
    good_vol = evaluate_scalping_fit(_make_condition(volume_ratio=1.5))
    bad_vol = evaluate_scalping_fit(_make_condition(volume_ratio=0.5))
    assert good_vol["score"] > bad_vol["score"]


def test_scalping_score_range() -> None:
    """Score must be between 0 and 100 for any combination of inputs."""
    for rsi in (10, 50, 90):
        for vol in (0.05, 1.0, 5.0):
            for vr in (0.3, 1.0, 2.0):
                for regime in ("BULLISH", "BEARISH", "NEUTRAL", "SIDEWAYS"):
                    c = _make_condition(
                        daily_rsi=rsi,
                        intraday_volatility_pct=vol,
                        volume_ratio=vr,
                        market_regime=regime,
                    )
                    r = evaluate_scalping_fit(c)
                    assert 0 <= r["score"] <= 100, f"Score out of range: {r['score']}"


# ---------------------------------------------------------------------------
# evaluate_sentiment_fit
# ---------------------------------------------------------------------------


def test_sentiment_ideal_bullish() -> None:
    """Strong BULLISH regime + good momentum + trend = high sentiment score."""
    condition = _make_condition(
        daily_rsi=62.0,
        intraday_volatility_pct=1.0,
        momentum_pct=2.5,
        trend_pct=1.5,
        volume_ratio=1.5,
        market_regime="BULLISH",
    )
    result = evaluate_sentiment_fit(condition)
    assert result["score"] >= 80, f"Expected high score, got {result['score']}"
    assert result["grade"] in ("A", "B")


def test_sentiment_ideal_bearish() -> None:
    """Strong BEARISH regime with aligned RSI should score well."""
    condition = _make_condition(
        daily_rsi=38.0,
        intraday_volatility_pct=1.0,
        momentum_pct=-2.0,
        trend_pct=-1.2,
        volume_ratio=1.4,
        market_regime="BEARISH",
    )
    result = evaluate_sentiment_fit(condition)
    assert result["score"] >= 70, f"Expected good score, got {result['score']}"


def test_sentiment_poor_sideways_market() -> None:
    """SIDEWAYS market + flat momentum = poor sentiment/trend fit."""
    condition = _make_condition(
        daily_rsi=50.0,
        intraday_volatility_pct=0.5,
        momentum_pct=0.1,
        trend_pct=0.05,
        volume_ratio=0.9,
        market_regime="SIDEWAYS",
    )
    result = evaluate_sentiment_fit(condition)
    assert result["score"] <= 40, f"Expected low score, got {result['score']}"
    assert result["grade"] in ("D", "F")


def test_sentiment_score_range() -> None:
    """Score must be between 0 and 100 for any combination of inputs."""
    for rsi in (20, 55, 85):
        for mom in (-3.0, 0.0, 3.0):
            for trend in (-2.0, 0.0, 2.0):
                for regime in ("BULLISH", "BEARISH", "NEUTRAL", "SIDEWAYS"):
                    c = _make_condition(
                        daily_rsi=rsi,
                        momentum_pct=mom,
                        trend_pct=trend,
                        market_regime=regime,
                    )
                    r = evaluate_sentiment_fit(c)
                    assert 0 <= r["score"] <= 100, f"Score out of range: {r['score']}"


def test_sentiment_factors_count() -> None:
    """There must be exactly 5 scored factors."""
    result = evaluate_sentiment_fit(_make_condition())
    assert len(result["factors"]) == 5


# ---------------------------------------------------------------------------
# economist_commentary
# ---------------------------------------------------------------------------


def test_economist_no_macro_data() -> None:
    """When no macro inputs are given, returns a useful fallback note."""
    condition = _make_condition()
    notes = economist_commentary(condition)
    assert len(notes) >= 1
    assert any("macro" in n.lower() or "vix" in n.lower() for n in notes)


def test_economist_high_vix() -> None:
    condition = _make_condition(vix_level=38.0)
    notes = economist_commentary(condition)
    assert any("vix" in n.lower() or "fear" in n.lower() or "panic" in n.lower() for n in notes)


def test_economist_low_vix() -> None:
    condition = _make_condition(vix_level=11.0)
    notes = economist_commentary(condition)
    assert any("complacency" in n.lower() or "compress" in n.lower() for n in notes)


def test_economist_inverted_yield_curve() -> None:
    condition = _make_condition(yield_curve_spread_bps=-80.0)
    notes = economist_commentary(condition)
    assert any("invert" in n.lower() or "recession" in n.lower() for n in notes)


def test_economist_positive_yield_curve() -> None:
    condition = _make_condition(yield_curve_spread_bps=120.0)
    notes = economist_commentary(condition)
    assert any("positive" in n.lower() or "constructive" in n.lower() for n in notes)


def test_economist_wide_credit_spreads() -> None:
    condition = _make_condition(credit_spread_bps=350.0)
    notes = economist_commentary(condition)
    assert any("wide" in n.lower() or "distress" in n.lower() or "systemic" in n.lower() for n in notes)


def test_economist_multiple_macro_signals() -> None:
    """Multiple bearish signals trigger a compound warning."""
    condition = _make_condition(
        market_regime="BEARISH",
        vix_level=32.0,
        yield_curve_spread_bps=-60.0,
        credit_spread_bps=200.0,
    )
    notes = economist_commentary(condition)
    assert len(notes) >= 3


# ---------------------------------------------------------------------------
# compare_strategies
# ---------------------------------------------------------------------------


def test_compare_prefers_scalping_in_sideways() -> None:
    """Sideways, low momentum → scalping should win."""
    condition = _make_condition(
        daily_rsi=50.0,
        intraday_volatility_pct=0.9,
        momentum_pct=0.1,
        trend_pct=0.0,
        volume_ratio=1.4,
        market_regime="SIDEWAYS",
    )
    result = compare_strategies(condition)
    assert "SCALPING" in result["recommendation"] or "BOTH" in result["recommendation"]


def test_compare_prefers_sentiment_in_bullish_trend() -> None:
    """Strong bull trend → sentiment/trend strategy should win."""
    condition = _make_condition(
        daily_rsi=65.0,
        intraday_volatility_pct=1.0,
        momentum_pct=2.5,
        trend_pct=1.8,
        volume_ratio=1.5,
        market_regime="BULLISH",
    )
    result = compare_strategies(condition)
    assert "SENTIMENT" in result["recommendation"] or "BOTH" in result["recommendation"]


def test_compare_result_structure() -> None:
    """compare_strategies must return all expected keys."""
    result = compare_strategies(_make_condition())
    for key in ("scalping", "sentiment", "recommendation", "reasoning", "economist_notes", "score_delta"):
        assert key in result, f"Missing key: {key}"


def test_compare_score_delta_consistent() -> None:
    """score_delta must equal sentiment_score - scalping_score."""
    condition = _make_condition(
        market_regime="BULLISH",
        momentum_pct=2.0,
        trend_pct=1.5,
        daily_rsi=60.0,
    )
    result = compare_strategies(condition)
    expected_delta = result["sentiment"]["score"] - result["scalping"]["score"]
    assert result["score_delta"] == expected_delta


def test_compare_close_scores_recommend_both() -> None:
    """If the two strategy scores are within 5 points, recommend BOTH."""
    # Build a condition where both strategies have similar scores
    # NEUTRAL regime, mid RSI, moderate volatility, moderate momentum
    condition = _make_condition(
        daily_rsi=52.0,
        intraday_volatility_pct=0.8,
        momentum_pct=1.0,
        trend_pct=0.5,
        volume_ratio=1.3,
        market_regime="NEUTRAL",
    )
    result = compare_strategies(condition)
    delta = abs(result["score_delta"])
    if delta <= 5:
        assert "BOTH" in result["recommendation"]


def test_compare_economist_notes_included() -> None:
    """compare_strategies must include economist notes."""
    result = compare_strategies(_make_condition(vix_level=25.0))
    assert isinstance(result["economist_notes"], list)
    assert len(result["economist_notes"]) >= 1


if __name__ == "__main__":
    test_market_condition_defaults()
    test_scalping_ideal_sideways_market()
    test_scalping_poor_strong_trend()
    test_scalping_extreme_rsi_penalised()
    test_scalping_high_volatility_penalised()
    test_scalping_low_volume_penalised()
    test_scalping_score_range()
    test_sentiment_ideal_bullish()
    test_sentiment_ideal_bearish()
    test_sentiment_poor_sideways_market()
    test_sentiment_score_range()
    test_sentiment_factors_count()
    test_economist_no_macro_data()
    test_economist_high_vix()
    test_economist_low_vix()
    test_economist_inverted_yield_curve()
    test_economist_positive_yield_curve()
    test_economist_wide_credit_spreads()
    test_economist_multiple_macro_signals()
    test_compare_prefers_scalping_in_sideways()
    test_compare_prefers_sentiment_in_bullish_trend()
    test_compare_result_structure()
    test_compare_score_delta_consistent()
    test_compare_close_scores_recommend_both()
    test_compare_economist_notes_included()
    print("All market_strategy_fit tests passed.")
