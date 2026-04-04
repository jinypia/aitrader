from __future__ import annotations

import argparse
import csv
import io
import math
import json
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import requests

from config import load_settings, selection_universe_symbols
from strategy import bearish_long_exception_ready, trend_runtime_diagnostics, trend_runtime_signal


@dataclass
class TechParams:
    volume_spike_mult: float
    short_bottom_bb_max: float
    short_top_bb_min: float
    golden_cross_entry_bb_max: float
    short_bottom_sma_ratio: float
    short_top_sma_ratio: float


@dataclass
class SessionGuardParams:
    market_shock_drop_pct: float
    vkospi_spike_proxy_pct: float
    bearish_exception_trigger_pct: float
    bearish_exception_max_market_drop_pct: float
    bearish_exception_max_vol_pct: float


BASELINE_TECH = TechParams(
    volume_spike_mult=1.50,
    short_bottom_bb_max=0.25,
    short_top_bb_min=0.75,
    golden_cross_entry_bb_max=0.65,
    short_bottom_sma_ratio=0.995,
    short_top_sma_ratio=1.005,
)

TUNED_TECH = TechParams(
    volume_spike_mult=1.25,
    short_bottom_bb_max=0.35,
    short_top_bb_min=0.68,
    golden_cross_entry_bb_max=0.72,
    short_bottom_sma_ratio=0.99,
    short_top_sma_ratio=1.01,
)

CURRENT_TECH = TechParams(
    volume_spike_mult=1.25,
    short_bottom_bb_max=0.35,
    short_top_bb_min=0.68,
    golden_cross_entry_bb_max=0.72,
    short_bottom_sma_ratio=0.99,
    short_top_sma_ratio=1.01,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKTEST_CACHE_DIR = PROJECT_ROOT / "data" / "backtest_cache"
SHORT_TERM_REPORT_PATH = PROJECT_ROOT / "data" / "short_term_trade_report_top100.json"
DEFAULT_SHORT_TERM_TOP_N = 800
DEFAULT_SHORT_TERM_SEED_N = 1000
DEFAULT_BACKTEST_COMPARE_KR_SYMBOLS = 100
ROLLING_RANK_REPORT_PATH = PROJECT_ROOT / "data" / "rolling_rank_study_last20.json"
SHORT_HORIZON_RANK_REPORT_PATH = PROJECT_ROOT / "data" / "rolling_rank_short_horizon_last20.json"
DAILY_SELECTION_PORTFOLIO_REPORT_PATH = PROJECT_ROOT / "data" / "daily_selection_portfolio_last20.json"
RANK_WEIGHTED_REPORT_PATH = PROJECT_ROOT / "data" / "rank_weighted_portfolio_last20.json"
INTRADAY_REPLAY_REPORT_PATH = PROJECT_ROOT / "data" / "intraday_selected_replay.json"
INTRADAY_SCALPING_REPORT_PATH = PROJECT_ROOT / "data" / "intraday_scalping_report.json"


from scalping_strategy import ScalpParams, calculate_scalp_metrics, scalp_entry_signal, scalp_exit_signal


def _simulation_strategy_snapshot(settings: Any) -> dict[str, Any]:
    return {
        "bar_interval_minutes": int(getattr(settings, "bar_interval_minutes", 2)),
        "decision_on_bar_close_only": bool(getattr(settings, "decision_on_bar_close_only", True)),
        "selection_style": "top1_priority_with_watchlist",
        "ranking_factors": ["RAM", "TEF", "TQP", "relative_strength", "trend_quality"],
        "entry_style": "short_horizon_trend_follow",
        "risk_guards": [
            "shock_reversal_risk",
            "event_spike_exhaustion",
            "market_surge_chase",
            "late_chase",
        ],
    }


def _load_selected_intraday_rows() -> list[dict[str, Any]]:
    path = PROJECT_ROOT / "data" / "selected_intraday_prices.json"
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
    except Exception:
        return []
    return [row for row in list(raw.get("rows") or []) if isinstance(row, dict)]


def _mean(vals: list[float]) -> float:
    return (sum(vals) / float(len(vals))) if vals else 0.0


def _selected_continuation_probe_ready(diag: dict[str, Any]) -> bool:
    blockers = list(diag.get("blockers") or [])
    return (
        bool(diag.get("entry_ready", False))
        and str(diag.get("action") or "") == "HOLD"
        and str(diag.get("market_type") or "") == "HEALTHY_TREND"
        and (not bool(diag.get("watchlist", False)))
        and (not blockers)
        and 1.75 <= float(diag.get("risk_adjusted_momentum", 0.0)) <= 2.60
        and 0.08 <= float(diag.get("trend_efficiency", 0.0)) <= 0.13
        and 1.50 <= float(diag.get("attention_ratio", 0.0)) <= 1.90
        and 1.60 <= float(diag.get("value_spike_ratio", 0.0)) <= 3.20
        and 53.0 <= float(diag.get("daily_rsi", 0.0)) <= 59.0
        and 9.5 <= float(diag.get("momentum_pct", 0.0)) <= 14.5
        and 0.45 <= float(diag.get("trend_pct", 0.0)) <= 0.80
    )




def _cache_path(market: str, symbol: str) -> Path:
    safe_symbol = "".join(ch for ch in str(symbol).strip().upper() if ch.isalnum() or ch in {"_", "-"})
    return BACKTEST_CACHE_DIR / f"{market.lower()}_{safe_symbol}_daily.json"


def _load_cached_bars(market: str, symbol: str, *, limit: int) -> list[dict[str, float]]:
    path = _cache_path(market, symbol)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
    except Exception:
        return []
    bars = raw.get("bars") if isinstance(raw, dict) else None
    if not isinstance(bars, list):
        return []
    cleaned: list[dict[str, float]] = []
    for row in bars:
        if not isinstance(row, dict):
            continue
        try:
            cleaned.append(
                {
                    "date": str(row.get("date") or "").strip(),
                    "open": float(row.get("open", 0.0)),
                    "high": float(row.get("high", 0.0)),
                    "low": float(row.get("low", 0.0)),
                    "close": float(row.get("close", 0.0)),
                    "volume": float(row.get("volume", 0.0)),
                    "value": float(row.get("value", 0.0)),
                }
            )
        except Exception:
            continue
    cleaned = [x for x in cleaned if float(x.get("close", 0.0)) > 0]
    return cleaned[-max(30, int(limit)) :]


def _cached_market_symbols(market: str) -> list[str]:
    prefix = f"{str(market or '').lower()}_"
    out: list[str] = []
    for path in BACKTEST_CACHE_DIR.glob(f"{prefix}*_daily.json"):
        name = path.name
        if not name.startswith(prefix) or not name.endswith("_daily.json"):
            continue
        symbol = name[len(prefix) : -len("_daily.json")].strip().upper()
        if symbol:
            out.append(symbol)
    return list(dict.fromkeys(out))


def _kr_backtest_seed_symbols(settings: Any, *, limit: int) -> list[str]:
    primary = [x for x in selection_universe_symbols(settings) if x.strip().isdigit()]
    cached = [x for x in _cached_market_symbols("KR") if x.strip().isdigit()]
    merged = list(dict.fromkeys(primary + cached))
    if primary:
        return merged[: max(len(primary), int(limit))]
    return merged[: int(limit)]


def _has_corporate_action_like_gap(bars: list[dict[str, float]]) -> bool:
    if len(bars) < 2:
        return False
    for i in range(1, len(bars)):
        prev_close = float((bars[i - 1] or {}).get("close", 0.0) or 0.0)
        cur_open = float((bars[i] or {}).get("open", 0.0) or 0.0)
        cur_close = float((bars[i] or {}).get("close", 0.0) or 0.0)
        if prev_close <= 0 or cur_open <= 0 or cur_close <= 0:
            continue
        if abs((cur_open / prev_close) - 1.0) >= 0.40 or abs((cur_close / prev_close) - 1.0) >= 0.40:
            return True
    return False


def _save_cached_bars(market: str, symbol: str, bars: list[dict[str, float]]) -> None:
    if not bars:
        return
    path = _cache_path(market, symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "market": market,
        "symbol": symbol,
        "saved_at": int(time.time()),
        "bars": bars,
    }
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n")


def _fetch_market_bars(market: str, symbol: str, *, limit: int) -> list[dict[str, float]]:
    if market == "US":
        return _fetch_stooq_daily_bars_us(symbol, limit=limit)
    return _fetch_yahoo_daily_bars_kr(symbol, limit=limit)


def _pstdev(vals: list[float]) -> float:
    if len(vals) < 2:
        return 0.0
    m = _mean(vals)
    return math.sqrt(sum((x - m) ** 2 for x in vals) / float(len(vals)))


def _sma(vals: list[float], n: int) -> float:
    if len(vals) < n:
        return 0.0
    return _mean(vals[-n:])


def _pct_change(newer: float, older: float) -> float:
    if older <= 0:
        return 0.0
    return ((float(newer) / float(older)) - 1.0) * 100.0


def _rsi(vals: list[float], period: int = 14) -> float:
    if len(vals) < period + 1:
        return 50.0
    gains = 0.0
    losses = 0.0
    for i in range(len(vals) - period, len(vals)):
        delta = float(vals[i]) - float(vals[i - 1])
        if delta > 0:
            gains += delta
        elif delta < 0:
            losses += abs(delta)
    if losses <= 0:
        return 100.0 if gains > 0 else 50.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))


def _atr_proxy_pct(closes: list[float], lookback_days: int = 14) -> float:
    if len(closes) < lookback_days + 1:
        return 0.0
    moves: list[float] = []
    for i in range(len(closes) - lookback_days, len(closes)):
        prev = float(closes[i - 1])
        cur = float(closes[i])
        if prev <= 0:
            continue
        moves.append(abs((cur - prev) / prev))
    return _mean(moves) * 100.0 if moves else 0.0


def _trend_structure_ok(bars: list[dict[str, float]]) -> bool:
    if len(bars) < 15:
        return False
    lows = [float(x.get("low", 0.0)) for x in bars[-15:]]
    highs = [float(x.get("high", 0.0)) for x in bars[-15:]]
    if len(lows) < 15 or len(highs) < 15:
        return False
    low_a = min(lows[0:5])
    low_b = min(lows[5:10])
    low_c = min(lows[10:15])
    high_a = max(highs[0:5])
    high_b = max(highs[5:10])
    high_c = max(highs[10:15])
    return (
        (low_b >= (low_a * 0.95))  # Allow 5% lower
        and (low_c >= low_b)       # Allow equal
        and (high_b >= (high_a * 0.95))  # Allow 5% lower
        and (high_c >= high_b)     # Allow equal
    )


def _trend_metrics_from_bars(
    bars: list[dict[str, float]],
    *,
    market_index_pct: float,
    breakout_near_high_pct: float = 97.0,
) -> dict[str, float]:
    closes = [float(x.get("close", 0.0)) for x in bars if float(x.get("close", 0.0)) > 0]
    highs = [float(x.get("high", 0.0) or x.get("close", 0.0)) for x in bars if float(x.get("close", 0.0)) > 0]
    values = [float(x.get("value", 0.0)) for x in bars if float(x.get("close", 0.0)) > 0]
    volumes = [float(x.get("volume", 0.0)) for x in bars if float(x.get("close", 0.0)) > 0]
    if len(closes) < 60:
        return {}
    last = closes[-1]
    ma5 = _sma(closes, 5)
    ma20 = _sma(closes, 20)
    ma60 = _sma(closes, 60)
    # Compare current moving averages with the actual prior moving averages,
    # not with shorter lookback proxies, so trend slope is measured consistently.
    ma20_prev = _mean(closes[-21:-1]) if len(closes) >= 21 else ma20
    ma60_prev = _mean(closes[-61:-1]) if len(closes) >= 61 else ma60
    ret5 = _pct_change(last, closes[-6]) if len(closes) >= 6 else 0.0
    ret20 = _pct_change(last, closes[-21]) if len(closes) >= 21 else 0.0
    ret60 = _pct_change(last, closes[-61]) if len(closes) >= 61 else ret20
    relative_pct = ret20 - market_index_pct
    volatility_pct = _pstdev([_pct_change(closes[i], closes[i - 1]) / 100.0 for i in range(1, len(closes))]) * 100.0 if len(closes) >= 3 else 0.0
    atr14_pct = _atr_proxy_pct(closes, 14)
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
    trend_ok = 1.0 if (ma5 > ma20 > ma60 and ma20 > ma20_prev and ma60 >= ma60_prev) else 0.0
    structure_ok = 1.0 if _trend_structure_ok(bars) else 0.0
    breakout_ok = 1.0 if near_high_pct >= float(breakout_near_high_pct) else 0.0
    overheat = 1.0 if (ret1 >= 18.0 or ret2 >= 25.0) else 0.0
    trend_pct = _pct_change(ma20, ma20_prev) if ma20_prev > 0 else 0.0
    overextension_penalty = (
        max(0.0, ret20 - 12.0) * 0.35
        + max(0.0, trend_pct - 6.0) * 1.20
        + max(0.0, daily_rsi - 72.0) * 0.15
    )
    risk_unit_pct = max(1.0, atr14_pct, volatility_pct)
    risk_adjusted_momentum = ret20 / risk_unit_pct
    risk_adjusted_relative = relative_pct / risk_unit_pct
    trend_efficiency = max(0.0, trend_pct) / risk_unit_pct
    participation_quality = (
        (max(0.0, attention_ratio - 1.0) * 0.55)
        + (max(0.0, value_spike_ratio - 1.0) * 0.90)
        + (max(0.0, volume_ratio - 1.0) * 0.35)
    )
    speculative_participation_penalty = (
        max(0.0, attention_ratio - 1.45) * 10.0
        + max(0.0, value_spike_ratio - 1.65) * 8.0
    ) * max(0.0, 0.22 - trend_efficiency)
    noisy_participation_penalty = (
        max(0.0, attention_ratio - 1.30) * 4.5
        + max(0.0, value_spike_ratio - 1.45) * 3.5
    ) * max(0.0, 0.35 - risk_adjusted_momentum)
    crowded_low_efficiency_penalty = (
        max(0.0, attention_ratio - 1.35) * 4.0
        + max(0.0, value_spike_ratio - 1.55) * 3.0
    ) * max(0.0, 0.12 - trend_efficiency) * max(0.0, 2.60 - risk_adjusted_momentum)
    top_rank_quality_penalty = (
        max(0.0, 3.00 - risk_adjusted_momentum) * 2.8
        + max(0.0, 0.18 - trend_efficiency) * 42.0
    )
    score = (
        0.12 * ret20
        + 0.10 * ret5
        + 0.26 * relative_pct
        + 0.12 * ret60
        + 2.20 * risk_adjusted_momentum
        + 1.80 * risk_adjusted_relative
        + 2.90 * trend_efficiency
        + 3.0 * (attention_ratio - 1.0)
        + 2.5 * (volume_ratio - 1.0)
        + 1.8 * max(0.0, value_spike_ratio - 1.0)
        + 2.0 * participation_quality
        + 0.10 * (near_high_pct - 95.0)
        - 0.30 * volatility_pct
        + 8.0 * trend_ok
        + 6.0 * structure_ok
        + 4.0 * breakout_ok
        - 12.0 * overheat
        - overextension_penalty
        - speculative_participation_penalty
        - noisy_participation_penalty
        - crowded_low_efficiency_penalty
        - top_rank_quality_penalty
    )
    return {
        "score": score,
        "momentum_pct": ret20,
        "ret5_pct": ret5,
        "ret60_pct": ret60,
        "relative_pct": relative_pct,
        "trend_pct": trend_pct,
        "volatility_pct": volatility_pct,
        "atr14_pct": atr14_pct,
        "attention_ratio": attention_ratio,
        "value_spike_ratio": value_spike_ratio,
        "risk_adjusted_momentum": risk_adjusted_momentum,
        "risk_adjusted_relative": risk_adjusted_relative,
        "trend_efficiency": trend_efficiency,
        "participation_quality": participation_quality,
        "speculative_participation_penalty": speculative_participation_penalty,
        "noisy_participation_penalty": noisy_participation_penalty,
        "crowded_low_efficiency_penalty": crowded_low_efficiency_penalty,
        "top_rank_quality_penalty": top_rank_quality_penalty,
        "daily_rsi": daily_rsi,
        "near_high_pct": near_high_pct,
        "trend_ok": trend_ok,
        "structure_ok": structure_ok,
        "breakout_ok": breakout_ok,
        "overheat": overheat,
    }


def _tech_flags(
    *,
    hist_closes: list[float],
    current_price: float,
    current_volume: int,
    hist_volumes: list[int],
    params: TechParams,
) -> dict[str, Any]:
    series = [float(x) for x in hist_closes[-120:] if float(x) > 0]
    if current_price > 0:
        series.append(float(current_price))
    if len(series) < 25:
        return {
            "golden_cross": False,
            "death_cross": False,
            "near_lower": False,
            "near_upper": False,
            "volume_spike": False,
            "short_bottom": False,
            "short_top": False,
            "bb_pos": 0.0,
            "trend_up": False,
            "trend_down": False,
        }
    sma5 = _sma(series, 5)
    sma20 = _sma(series, 20)
    sma60 = _sma(series, 60) if len(series) >= 60 else _sma(series, min(60, len(series)))
    prev = series[:-1]
    prev_sma5 = _sma(prev, 5)
    prev_sma20 = _sma(prev, 20)
    golden_cross = (prev_sma5 <= prev_sma20) and (sma5 > sma20) if prev_sma20 > 0 else False
    death_cross = (prev_sma5 >= prev_sma20) and (sma5 < sma20) if prev_sma20 > 0 else False
    bb_window = series[-20:]
    bb_mid = _mean(bb_window)
    bb_std = _pstdev(bb_window) if len(bb_window) >= 3 else 0.0
    bb_upper = bb_mid + (2.0 * bb_std)
    bb_lower = bb_mid - (2.0 * bb_std)
    bb_span = max(1e-9, bb_upper - bb_lower)
    bb_pos = ((current_price - bb_lower) / bb_span) if current_price > 0 else 0.5
    near_lower = current_price <= (bb_lower * 1.01) if bb_lower > 0 else False
    near_upper = current_price >= (bb_upper * 0.99) if bb_upper > 0 else False
    vol_window = [int(v) for v in hist_volumes[-20:] if int(v) > 0]
    avg_vol = _mean([float(v) for v in vol_window]) if vol_window else 0.0
    volume_spike = (current_volume > (avg_vol * params.volume_spike_mult)) if avg_vol > 0 else (current_volume > 0)
    short_bottom = near_lower and (sma5 >= (sma20 * params.short_bottom_sma_ratio)) and (bb_pos <= params.short_bottom_bb_max)
    short_top = near_upper and ((death_cross or sma5 <= (sma20 * params.short_top_sma_ratio)) and (bb_pos >= params.short_top_bb_min))
    trend_up = (sma20 > 0 and sma60 > 0 and sma20 >= (sma60 * 1.002))
    trend_down = (sma20 > 0 and sma60 > 0 and sma20 <= (sma60 * 0.998))
    return {
        "golden_cross": golden_cross,
        "death_cross": death_cross,
        "near_lower": near_lower,
        "near_upper": near_upper,
        "volume_spike": volume_spike,
        "short_bottom": short_bottom,
        "short_top": short_top,
        "bb_pos": bb_pos,
        "trend_up": trend_up,
        "trend_down": trend_down,
    }


def _fetch_stooq_daily_ohlcv_us(symbol: str, *, limit: int) -> tuple[list[float], list[int]]:
    sym = f"{str(symbol).strip().lower()}.us"
    url = f"https://stooq.com/q/d/l/?s={sym}&i=d"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
    except Exception:
        return [], []
    rows = list(csv.DictReader(io.StringIO(r.text)))
    closes: list[float] = []
    vols: list[int] = []
    for row in rows:
        try:
            c = float(str(row.get("Close") or "").strip())
            v = int(float(str(row.get("Volume") or "0").strip()))
        except Exception:
            continue
        if c > 0:
            closes.append(c)
            vols.append(max(0, v))
    return closes[-max(30, int(limit)) :], vols[-max(30, int(limit)) :]


def _fetch_stooq_daily_bars_us(symbol: str, *, limit: int) -> list[dict[str, float]]:
    sym = f"{str(symbol).strip().lower()}.us"
    url = f"https://stooq.com/q/d/l/?s={sym}&i=d"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
    except Exception:
        return []
    rows = list(csv.DictReader(io.StringIO(r.text)))
    bars: list[dict[str, float]] = []
    for row in rows:
        try:
            o = float(str(row.get("Open") or "").strip())
            h = float(str(row.get("High") or "").strip())
            l = float(str(row.get("Low") or "").strip())
            c = float(str(row.get("Close") or "").strip())
            v = int(float(str(row.get("Volume") or "0").strip()))
        except Exception:
            continue
        if c > 0:
            bars.append(
                {
                    "date": str(row.get("Date") or "").strip(),
                    "open": o if o > 0 else c,
                    "high": h if h > 0 else c,
                    "low": l if l > 0 else c,
                    "close": c,
                    "volume": float(max(0, v)),
                    "value": float(max(0, v)) * c,
                }
            )
    return bars[-max(30, int(limit)) :]


def _fetch_yahoo_daily_ohlcv_kr(symbol: str, *, limit: int) -> tuple[list[float], list[int]]:
    code = str(symbol).strip()
    if not code:
        return [], []
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.KS?range=2y&interval=1d"
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        data = r.json()
    except Exception:
        return [], []
    result = ((data.get("chart") or {}).get("result") or [])
    if not result:
        return [], []
    q = (((result[0].get("indicators") or {}).get("quote") or [{}])[0] or {})
    close_raw = list(q.get("close") or [])
    vol_raw = list(q.get("volume") or [])
    n = min(len(close_raw), len(vol_raw))
    closes: list[float] = []
    vols: list[int] = []
    for i in range(n):
        c = close_raw[i]
        v = vol_raw[i]
        if c is None:
            continue
        try:
            cp = float(c)
            vv = int(float(v or 0))
        except Exception:
            continue
        if cp > 0:
            closes.append(cp)
            vols.append(max(0, vv))
    return closes[-max(30, int(limit)) :], vols[-max(30, int(limit)) :]


def _fetch_yahoo_daily_bars_kr(symbol: str, *, limit: int) -> list[dict[str, float]]:
    code = str(symbol).strip()
    if not code:
        return []
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}.KS?range=2y&interval=1d"
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []
    result = ((data.get("chart") or {}).get("result") or [])
    if not result:
        return []
    result0 = result[0] or {}
    timestamps = list(result0.get("timestamp") or [])
    q = (((result0.get("indicators") or {}).get("quote") or [{}])[0] or {})
    open_raw = list(q.get("open") or [])
    high_raw = list(q.get("high") or [])
    low_raw = list(q.get("low") or [])
    close_raw = list(q.get("close") or [])
    vol_raw = list(q.get("volume") or [])
    n = min(len(open_raw), len(high_raw), len(low_raw), len(close_raw), len(vol_raw), len(timestamps))
    bars: list[dict[str, float]] = []
    for i in range(n):
        try:
            c = float(close_raw[i])
            o = float(open_raw[i]) if open_raw[i] is not None else c
            h = float(high_raw[i]) if high_raw[i] is not None else c
            l = float(low_raw[i]) if low_raw[i] is not None else c
            v = int(float(vol_raw[i] or 0))
            ts = int(float(timestamps[i]))
        except Exception:
            continue
        if c > 0:
            bars.append(
                {
                    "date": time.strftime("%Y-%m-%d", time.gmtime(ts)),
                    "open": o if o > 0 else c,
                    "high": h if h > 0 else c,
                    "low": l if l > 0 else c,
                    "close": c,
                    "volume": float(max(0, v)),
                    "value": float(max(0, v)) * c,
                }
            )
    return bars[-max(30, int(limit)) :]


def _simulate_symbol(
    *,
    market: str,
    bars: list[dict[str, float]],
    days: int,
    initial_cash: float,
    buy_drop_pct: float,
    sell_rise_pct: float,
    signal_confirm_cycles: int,
    params: TechParams,
    guard_params: SessionGuardParams,
    settings: Any,
    market_change_pct_series: list[float] | None = None,
    symbol: str = "",
    capture_trades: bool = False,
) -> dict[str, Any]:
    closes = [float(x.get("close", 0.0)) for x in bars if float(x.get("close", 0.0)) > 0]
    volumes = [int(float(x.get("volume", 0.0))) for x in bars if float(x.get("close", 0.0)) > 0]
    n = min(len(closes), len(volumes))
    if n < 40:
        return {"final_equity": initial_cash, "trade_count": 0.0, "sell_count": 0.0, "win_rate": 0.0, "mdd": 0.0}
    cash = float(initial_cash)
    qty = 0
    avg = 0.0
    peak = cash
    mdd = 0.0
    trade_count = 0
    sell_count = 0
    win_count = 0
    shock_blocked_buys = 0
    streak_sig = ""
    streak_cnt = 0
    peak_price = 0.0
    hold_bars = 0
    entry_stop_atr = 0.0
    entry_take_atr = 0.0
    entry_trailing_atr = 0.0
    entry_stop_floor_pct = 0.0
    entry_take_floor_pct = 0.0
    realized_pnls: list[float] = []
    loss_streak = 0
    max_loss_streak = 0
    entry_bar = -1
    entry_snapshot: dict[str, Any] = {}
    trades: list[dict[str, Any]] = []
    market_status_enabled = bool(getattr(settings, "market_status_filter_enabled", True))
    start = max(1, n - max(10, int(days)))
    for i in range(start, n):
        prev_p = float(closes[i - 1])
        cur_p = float(closes[i])
        if prev_p <= 0 or cur_p <= 0:
            continue
        lookback = min(20, i)
        base_idx = max(0, i - lookback)
        mom_base = float(closes[base_idx]) if base_idx < len(closes) else prev_p
        momentum_pct = (((cur_p / mom_base) - 1.0) * 100.0) if mom_base > 0 else 0.0
        sma_window = [float(x) for x in closes[max(0, i - 19): i + 1]]
        sma = _mean(sma_window) if sma_window else cur_p
        trend_pct = ((cur_p - sma) / sma * 100.0) if sma > 0 else 0.0
        market_chg_pct = 0.0
        if market_change_pct_series:
            offset = len(market_change_pct_series) - n
            mkt_i = i + offset
            if 0 <= mkt_i < len(market_change_pct_series):
                market_chg_pct = float(market_change_pct_series[mkt_i])
        mkt_lb = max(0, i - 20)
        mkt_rets = [float(x) for x in (market_change_pct_series or [])[mkt_lb:i] if isinstance(x, (int, float))]
        market_vol_pct = _pstdev(mkt_rets) if len(mkt_rets) >= 3 else 0.0
        shock_active = market_status_enabled and (
            market_chg_pct <= float(guard_params.market_shock_drop_pct)
            or market_vol_pct >= float(guard_params.vkospi_spike_proxy_pct)
        )
        metrics = _trend_metrics_from_bars(
            bars[: i + 1],
            market_index_pct=market_chg_pct,
            breakout_near_high_pct=float(getattr(settings, "trend_breakout_near_high_pct", 97.0)),
        )
        tf = _tech_flags(
            hist_closes=[float(x) for x in closes[:i]],
            current_price=cur_p,
            current_volume=int(volumes[i]),
            hist_volumes=[int(x) for x in volumes[max(0, i - 80): i]],
            params=params,
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
            trend_daily_rsi_min=float(getattr(settings, "trend_daily_rsi_min", 55.0)),
            trend_daily_rsi_max=float(getattr(settings, "trend_daily_rsi_max", 78.0)),
            trend_min_turnover_ratio_5_to_20=float(getattr(settings, "trend_min_turnover_ratio_5_to_20", 1.20)),
            trend_min_value_spike_ratio=float(getattr(settings, "trend_min_value_spike_ratio", 1.30)),
            trend_gap_skip_up_pct=float(getattr(settings, "trend_gap_skip_up_pct", 6.0)),
            trend_gap_skip_down_pct=float(getattr(settings, "trend_gap_skip_down_pct", -3.0)),
            trend_max_chase_from_open_pct=float(getattr(settings, "trend_max_chase_from_open_pct", 8.0)),
            market_chg_pct=market_chg_pct,
            momentum_pct=momentum_pct,
            trend_pct=trend_pct,
            tech_flags=tf,
            golden_cross_entry_bb_max=params.golden_cross_entry_bb_max,
            prev_price=prev_p,
            current_price=cur_p,
            atr_pct=float(metrics.get("atr14_pct", 0.0)),
            volume_ratio=float(metrics.get("value_spike_ratio", 0.0)),
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
        if (
            market_status_enabled
            and
            market_chg_pct <= float(guard_params.bearish_exception_trigger_pct)
            and qty <= 0
            and action == "HOLD"
            and bearish_long_ok
        ):
            action = "BUY"
            tech_priority = True
        exception_shock_ok = (not market_status_enabled) or (
            bearish_long_ok
            and market_chg_pct > float(guard_params.bearish_exception_max_market_drop_pct)
            and market_vol_pct < float(guard_params.bearish_exception_max_vol_pct)
        )
        if shock_active and qty <= 0 and action == "BUY" and not exception_shock_ok:
            action = "HOLD"
            shock_blocked_buys += 1
        elif (
            market_status_enabled
            and
            market_chg_pct <= float(guard_params.bearish_exception_trigger_pct)
            and qty <= 0
            and action == "BUY"
            and not bearish_long_ok
        ):
            action = "HOLD"

        local_confirm = max(1, int(signal_confirm_cycles))
        if tech_priority and action in {"BUY", "SELL"}:
            local_confirm = 1
        executable_signal = action
        if executable_signal in {"BUY", "SELL"}:
            if executable_signal == streak_sig:
                streak_cnt += 1
            else:
                streak_sig = executable_signal
                streak_cnt = 1
            if streak_cnt < local_confirm:
                action = "HOLD"
        else:
            streak_sig = ""
            streak_cnt = 0

        if action == "BUY" and qty <= 0:
            atr14_pct = float(metrics.get("atr14_pct", 2.0))
            if not market_status_enabled:
                entry_stop_atr = 1.4
                entry_take_atr = 2.4
                entry_trailing_atr = 1.4
                entry_stop_floor_pct = 2.8
                entry_take_floor_pct = 4.5
            elif market_chg_pct >= 0.7:
                entry_stop_atr = 1.8
                entry_take_atr = 3.0
                entry_trailing_atr = 1.8
                entry_stop_floor_pct = 3.5
                entry_take_floor_pct = 7.0
            elif market_chg_pct <= -0.7:
                entry_stop_atr = 1.0
                entry_take_atr = 2.0
                entry_trailing_atr = 1.2
                entry_stop_floor_pct = 2.0
                entry_take_floor_pct = 3.5
            else:
                entry_stop_atr = 1.4
                entry_take_atr = 2.4
                entry_trailing_atr = 1.4
                entry_stop_floor_pct = 2.8
                entry_take_floor_pct = 4.5
            stop_pct = max(entry_stop_floor_pct, atr14_pct * entry_stop_atr) / 100.0
            equity_now = cash + (qty * cur_p)
            risk_budget = max(0.0, equity_now * 0.004)
            # This simulator already receives a per-symbol sleeve from the caller,
            # so applying an additional per-name capital cap here would double-count
            # the position limit and suppress otherwise valid entries.
            capital_cap = equity_now
            qty_cash = int(min(cash, capital_cap) / cur_p)
            qty_risk = int(risk_budget / max(1e-9, cur_p * stop_pct))
            if qty_cash >= 1:
                buy_qty = max(1, min(qty_cash, qty_risk)) if qty_risk > 0 else 1
            else:
                buy_qty = 0
            need = buy_qty * cur_p
            if cash >= need and buy_qty > 0:
                cash -= need
                qty = buy_qty
                avg = cur_p
                peak_price = cur_p
                hold_bars = 0
                entry_bar = i
                entry_snapshot = {
                    "symbol": str(symbol or ""),
                    "buy_bar": i,
                    "buy_date": str((bars[i] or {}).get("date") or ""),
                    "buy_price": float(cur_p),
                    "buy_qty": int(buy_qty),
                    "market_chg_pct": float(market_chg_pct),
                    "momentum_pct": float(momentum_pct),
                    "trend_pct": float(trend_pct),
                    "daily_rsi": float(metrics.get("daily_rsi", 50.0)),
                    "attention_ratio": float(metrics.get("attention_ratio", 0.0)),
                    "value_spike_ratio": float(metrics.get("value_spike_ratio", 0.0)),
                    "bb_pos": float(tf.get("bb_pos", 0.0)),
                    "type": "bearish_exception" if bearish_long_ok and market_chg_pct <= float(guard_params.bearish_exception_trigger_pct) else "controlled_chase_or_pullback",
                }
                trade_count += 1
        elif qty > 0:
            hold_bars += 1
            peak_price = max(peak_price, cur_p)
            atr14_pct = float(metrics.get("atr14_pct", 2.0))
            position_return_pct = _pct_change(cur_p, avg) if avg > 0 else 0.0
            trailing_drawdown_pct = _pct_change(cur_p, peak_price) if peak_price > 0 else 0.0
            if not market_status_enabled:
                current_stop_atr = 1.4
                current_take_atr = 2.4
                current_trailing_atr = 1.4
                current_stop_floor_pct = 2.8
                current_take_floor_pct = 4.5
            elif market_chg_pct >= 0.7:
                current_stop_atr = 1.8
                current_take_atr = 3.0
                current_trailing_atr = 1.8
                current_stop_floor_pct = 3.5
                current_take_floor_pct = 7.0
            elif market_chg_pct <= -0.7:
                current_stop_atr = 1.0
                current_take_atr = 2.0
                current_trailing_atr = 1.2
                current_stop_floor_pct = 2.0
                current_take_floor_pct = 3.5
            else:
                current_stop_atr = 1.4
                current_take_atr = 2.4
                current_trailing_atr = 1.4
                current_stop_floor_pct = 2.8
                current_take_floor_pct = 4.5
            stop_loss_pct = -max(
                max(current_stop_floor_pct, entry_stop_floor_pct),
                atr14_pct * max(current_stop_atr, entry_stop_atr),
            )
            take_profit_pct = max(
                max(current_take_floor_pct, entry_take_floor_pct),
                atr14_pct * max(current_take_atr, entry_take_atr),
            )
            trailing_stop_pct = max(
                2.0,
                atr14_pct * max(current_trailing_atr, entry_trailing_atr),
            )
            quick_take_ready = (
                position_return_pct >= 1.8
                and (
                    float(metrics.get("daily_rsi", 50.0)) >= 66.0
                    or float(tf.get("bb_pos", 0.0)) >= 0.82
                    or bool(tf.get("short_top", False))
                )
            )
            fast_fail_exit_ready = (
                hold_bars <= 1
                and position_return_pct <= -2.4
                and (
                    bool(tf.get("trend_down", False))
                    or bool(tf.get("short_top", False))
                    or action == "SELL"
                    or float(tf.get("bb_pos", 0.0)) <= 0.55
                )
            )
            breakout_fail_fast_ready = (
                hold_bars <= 1
                and position_return_pct <= -1.8
                and (
                    action == "SELL"
                    or bool(tf.get("trend_down", False))
                    or float(tf.get("bb_pos", 0.0)) <= 0.62
                )
                and (
                    float(metrics.get("daily_rsi", 50.0)) < 60.0
                    or not bool(tf.get("volume_spike", False))
                )
            )
            upper_band_reversal_fail_ready = (
                hold_bars <= 1
                and position_return_pct <= -1.2
                and float(tf.get("bb_pos", 0.0)) >= 0.95
                and (
                    action == "SELL"
                    or bool(tf.get("trend_down", False))
                    or bool(tf.get("short_top", False))
                )
            )
            max_hold_exit_ready = (
                (hold_bars >= 2 and position_return_pct >= 0.8 and float(tf.get("bb_pos", 0.0)) >= 0.78)
                or (hold_bars >= 3 and position_return_pct >= 0.2)
            )
            if position_return_pct <= stop_loss_pct:
                action = "SELL"
            elif position_return_pct >= take_profit_pct:
                action = "SELL"
            elif quick_take_ready:
                action = "SELL"
            elif fast_fail_exit_ready:
                action = "SELL"
            elif breakout_fail_fast_ready:
                action = "SELL"
            elif upper_band_reversal_fail_ready:
                action = "SELL"
            elif max_hold_exit_ready:
                action = "SELL"
            elif trailing_drawdown_pct <= -trailing_stop_pct:
                action = "SELL"
            elif float(metrics.get("daily_rsi", 50.0)) < 50.0 and trend_pct < 0.0:
                action = "SELL"
        if action == "SELL" and qty > 0:
            pnl = (cur_p - avg) * qty
            sold_qty = qty
            cash += qty * cur_p
            qty = 0
            avg = 0.0
            peak_price = 0.0
            hold_bars = 0
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
            if capture_trades and entry_snapshot:
                trade_row = dict(entry_snapshot)
                trade_row.update(
                    {
                        "symbol": str(symbol or trade_row.get("symbol") or ""),
                        "sell_bar": i,
                        "sell_date": str((bars[i] or {}).get("date") or ""),
                        "sell_price": float(cur_p),
                        "sell_qty": int(sold_qty),
                        "hold_bars": int(max(0, i - entry_bar)) if entry_bar >= 0 else int(hold_bars),
                        "return_pct": float(_pct_change(cur_p, float(entry_snapshot.get("buy_price", cur_p)))),
                        "realized_pnl": float(pnl),
                    }
                )
                trades.append(trade_row)
            entry_bar = -1
            entry_snapshot = {}

        equity = cash + (qty * cur_p)
        if equity > peak:
            peak = equity
        dd = ((equity - peak) / peak * 100.0) if peak > 0 else 0.0
        if dd < mdd:
            mdd = dd
    last_px = float(closes[n - 1])
    final_equity = cash + (qty * last_px)
    win_rate = (win_count / float(sell_count) * 100.0) if sell_count > 0 else 0.0
    avg_win = _mean([x for x in realized_pnls if x > 0])
    avg_loss_abs = abs(_mean([x for x in realized_pnls if x <= 0]))
    expectancy = _mean(realized_pnls)
    gross_profit = sum(x for x in realized_pnls if x > 0)
    gross_loss_abs = abs(sum(x for x in realized_pnls if x <= 0))
    profit_factor = (gross_profit / gross_loss_abs) if gross_loss_abs > 0 else (999.0 if gross_profit > 0 else 0.0)
    return {
        "final_equity": float(final_equity),
        "trade_count": float(trade_count),
        "sell_count": float(sell_count),
        "win_rate": float(win_rate),
        "mdd": float(mdd),
        "shock_blocked_buys": float(shock_blocked_buys),
        "avg_win": float(avg_win),
        "avg_loss_abs": float(avg_loss_abs),
        "expectancy": float(expectancy),
        "profit_factor": float(profit_factor),
        "max_loss_streak": float(max_loss_streak),
        "trades": trades,
    }


def generate_short_term_trade_report(
    *,
    top_n: int = DEFAULT_SHORT_TERM_TOP_N,
    seed_n: int = DEFAULT_SHORT_TERM_SEED_N,
    data_fetch_limit: int | None = None,
) -> dict[str, Any]:
    s = load_settings()
    guard = SessionGuardParams(
        market_shock_drop_pct=float(getattr(s, "market_shock_drop_pct", -2.0)),
        vkospi_spike_proxy_pct=float(getattr(s, "vkospi_spike_proxy_pct", 3.8)),
        bearish_exception_trigger_pct=float(getattr(s, "bearish_exception_trigger_pct", -0.4)),
        bearish_exception_max_market_drop_pct=float(getattr(s, "bearish_exception_max_market_drop_pct", -9.0)),
        bearish_exception_max_vol_pct=float(getattr(s, "bearish_exception_max_vol_pct", 3.2)),
    )
    universe = _kr_backtest_seed_symbols(s, limit=max(20, int(seed_n)))
    effective_fetch_limit = max(260, int(data_fetch_limit or 0))
    ready = _prepare_market_data(market="KR", symbols=universe, fetch_limit=effective_fetch_limit)
    if not ready:
        report = {
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "report_type": "short_term_report",
            "config": {
                "top_n": int(top_n),
                "seed_n": int(seed_n),
                "data_fetch_limit": int(effective_fetch_limit),
                "strategy": _simulation_strategy_snapshot(s),
            },
            "summary_by_symbol": [],
            "trades": [],
        }
        SHORT_TERM_REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        return report
    scored: list[tuple[str, list[dict[str, float]], float, str]] = []
    try:
        from bot_runtime import _multi_factor_rank_score as _runtime_rank_score, _load_sector_cache, _resolve_sector_map, _parse_symbol_text_map
        market_index_pct = _market_proxy_return_pct(ready, 20)
        sector_cache_path = getattr(s, "sector_cache_path", "data/sector_map_cache.json")
        symbols_only = [symbol for symbol, _bars in ready]
        symbol_sector_map = _resolve_sector_map(
            symbols=symbols_only,
            manual_map=_parse_symbol_text_map(getattr(s, "symbol_sector_map", "")),
            cache_map=_load_sector_cache(sector_cache_path),
            auto_enabled=bool(getattr(s, "sector_auto_map_enabled", True)),
            cache_path=sector_cache_path,
            fetch_limit=12,
        )
        for symbol, bars in ready:
            score, _factors = _runtime_rank_score(bars, market_index_pct=market_index_pct, settings=s)
            if score <= -900.0:
                continue
            sector = str(symbol_sector_map.get(symbol, symbol)).strip() or symbol
            scored.append((symbol, bars, float(score), sector))
    except Exception:
        for symbol, bars in ready:
            values = [float(x.get("value", 0.0)) for x in bars[-20:]]
            turnover20 = _mean(values) if values else 0.0
            scored.append((symbol, bars, turnover20, symbol))
    scored.sort(key=lambda row: float(row[2]), reverse=True)
    top_ready: list[tuple[str, list[dict[str, float]]]] = []
    sector_counts: dict[str, int] = {}
    for symbol, bars, _score, sector in scored:
        used = int(sector_counts.get(sector, 0))
        if used >= int(getattr(s, "trend_max_sector_names", 2)):
            continue
        top_ready.append((symbol, bars))
        sector_counts[sector] = used + 1
        if len(top_ready) >= max(1, int(top_n)):
            break
    market_proxy_rets = _market_proxy_returns_pct(top_ready)
    active_slots = max(
        1,
        min(
            len(top_ready),
            int(getattr(s, "trend_select_count", 5)),
            int(getattr(s, "max_active_positions", 5)),
        ),
    )
    per_cash = float(max(1_000_000.0, float(getattr(s, "initial_cash", 1_000_000.0)))) / float(active_slots)
    trades: list[dict[str, Any]] = []
    for symbol, bars in top_ready:
        result = _simulate_symbol(
            market="KR",
            symbol=symbol,
            bars=bars,
            days=120,
            initial_cash=per_cash,
            buy_drop_pct=float(s.buy_drop_pct),
            sell_rise_pct=float(s.sell_rise_pct),
            signal_confirm_cycles=int(s.signal_confirm_cycles),
            params=CURRENT_TECH,
            guard_params=guard,
            settings=s,
            market_change_pct_series=market_proxy_rets,
            capture_trades=True,
        )
        trades.extend(list(result.get("trades") or []))
    trades.sort(key=lambda row: (str(row.get("sell_date") or ""), str(row.get("symbol") or "")))
    summary_map: dict[str, dict[str, Any]] = {}
    for row in trades:
        symbol = str(row.get("symbol") or "").strip()
        if not symbol:
            continue
        bucket = summary_map.setdefault(
            symbol,
            {"symbol": symbol, "trades": 0, "wins": 0, "win_rate": 0.0, "realized_pnl": 0.0},
        )
        bucket["trades"] += 1
        if float(row.get("realized_pnl", 0.0)) > 0:
            bucket["wins"] += 1
        bucket["realized_pnl"] += float(row.get("realized_pnl", 0.0))
    summary_rows = list(summary_map.values())
    for row in summary_rows:
        trades_count = max(1, int(row.get("trades", 0)))
        row["win_rate"] = (float(row.get("wins", 0)) / float(trades_count)) * 100.0
    summary_rows.sort(key=lambda row: float(row.get("realized_pnl", 0.0)), reverse=True)
    report = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "report_type": "short_term_report",
        "seed_count": len(ready),
        "top_count": len(top_ready),
        "config": {
            "top_n": int(top_n),
            "seed_n": int(seed_n),
            "lookback_days": 120,
            "data_fetch_limit": int(effective_fetch_limit),
            "strategy": _simulation_strategy_snapshot(s),
        },
        "summary_by_symbol": summary_rows,
        "trades": trades,
    }
    SHORT_TERM_REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def generate_rolling_rank_study(
    *,
    window_days: int = 20,
    seed_n: int = 2000,
    data_fetch_limit: int | None = None,
) -> dict[str, Any]:
    s = load_settings()
    universe = _kr_backtest_seed_symbols(s, limit=max(100, int(seed_n)))
    effective_fetch_limit = max(260, int(data_fetch_limit or 0), int(window_days) + 65)
    ready = _prepare_market_data(market="KR", symbols=universe, fetch_limit=effective_fetch_limit)
    if not ready:
        report = {
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "report_type": "rolling_rank_study",
            "window_days": int(window_days),
            "config": {
                "window_days": int(window_days),
                "seed_n": int(seed_n),
                "data_fetch_limit": int(effective_fetch_limit),
                "strategy": _simulation_strategy_snapshot(s),
            },
            "daily_rows": [],
            "summary": {},
        }
        ROLLING_RANK_REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        return report

    from bot_runtime import _multi_factor_rank_score as _runtime_rank_score, _load_sector_cache, _resolve_sector_map, _parse_symbol_text_map

    history_floor = max(90, int(window_days) + 65)
    eligible_ready = [(sym, bars) for sym, bars in ready if len(bars) >= history_floor]
    if not eligible_ready:
        report = {
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "report_type": "rolling_rank_study",
            "window_days": int(window_days),
            "config": {
                "window_days": int(window_days),
                "seed_n": int(seed_n),
                "data_fetch_limit": int(effective_fetch_limit),
                "strategy": _simulation_strategy_snapshot(s),
            },
            "daily_rows": [],
            "summary": {"error": "not_enough_history"},
        }
        ROLLING_RANK_REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        return report

    min_len = min(len(bars) for _, bars in eligible_ready if bars)
    aligned_ready = [(sym, bars[-min_len:]) for sym, bars in eligible_ready]
    sector_cache_path = getattr(s, "sector_cache_path", "data/sector_map_cache.json")
    symbols_only = [sym for sym, _ in aligned_ready]
    symbol_sector_map = _resolve_sector_map(
        symbols=symbols_only,
        manual_map=_parse_symbol_text_map(getattr(s, "symbol_sector_map", "")),
        cache_map=_load_sector_cache(sector_cache_path),
        auto_enabled=bool(getattr(s, "sector_auto_map_enabled", True)),
        cache_path=sector_cache_path,
        fetch_limit=12,
    )

    start_idx = max(80, min_len - max(1, int(window_days)) - 5)
    end_idx = min_len - 6
    daily_rows: list[dict[str, Any]] = []
    fwd1_vals: list[float] = []
    fwd3_vals: list[float] = []
    fwd5_vals: list[float] = []
    top1_vals: list[float] = []
    top1_hit = 0
    selection_days = 0

    for idx in range(start_idx, end_idx + 1):
        sliced_ready = [(sym, bars[: idx + 1]) for sym, bars in aligned_ready]
        market_index_pct = _market_proxy_return_pct(sliced_ready, 20)
        scored: list[tuple[str, float, dict[str, float], str]] = []
        for sym, bars in sliced_ready:
            score, factors = _runtime_rank_score(bars, market_index_pct=market_index_pct, settings=s)
            if score <= -900.0:
                continue
            sector = str(symbol_sector_map.get(sym, sym)).strip() or sym
            scored.append((sym, float(score), factors, sector))
        scored.sort(key=lambda row: float(row[1]), reverse=True)

        capped: list[tuple[str, float, dict[str, float], str]] = []
        sector_counts: dict[str, int] = {}
        for sym, score, factors, sector in scored:
            used = int(sector_counts.get(sector, 0))
            if used >= int(getattr(s, "trend_max_sector_names", 2)):
                continue
            capped.append((sym, score, factors, sector))
            sector_counts[sector] = used + 1
            if len(capped) >= max(1, int(getattr(s, "trend_select_count", 5))):
                break
        if not capped:
            continue

        selection_days += 1
        next_day_returns: list[float] = []
        next3_returns: list[float] = []
        next5_returns: list[float] = []
        selected_rows: list[dict[str, Any]] = []
        for rank, (sym, score, factors, sector) in enumerate(capped, start=1):
            full_bars = next((bars for s2, bars in aligned_ready if s2 == sym), [])
            cur_close = float(full_bars[idx].get("close", 0.0))
            ret1 = _pct_change(float(full_bars[idx + 1].get("close", 0.0)), cur_close) if cur_close > 0 else 0.0
            ret3 = _pct_change(float(full_bars[idx + 3].get("close", 0.0)), cur_close) if cur_close > 0 else 0.0
            ret5 = _pct_change(float(full_bars[idx + 5].get("close", 0.0)), cur_close) if cur_close > 0 else 0.0
            next_day_returns.append(ret1)
            next3_returns.append(ret3)
            next5_returns.append(ret5)
            if rank == 1:
                top1_vals.append(ret1)
                if ret1 > 0:
                    top1_hit += 1
            selected_rows.append(
                {
                    "rank": rank,
                    "symbol": sym,
                    "sector": sector,
                    "score": round(score, 3),
                    "risk_adjusted_momentum": round(float(factors.get("risk_adjusted_momentum", 0.0)), 3),
                    "trend_efficiency": round(float(factors.get("trend_efficiency", 0.0)), 3),
                    "attention_ratio": round(float(factors.get("attention_ratio", 0.0)), 3),
                    "value_spike_ratio": round(float(factors.get("value_spike_ratio", 0.0)), 3),
                    "forward_1d_pct": round(ret1, 3),
                    "forward_3d_pct": round(ret3, 3),
                    "forward_5d_pct": round(ret5, 3),
                }
            )

        day = str(capped and next((bars[idx].get("date") for s2, bars in aligned_ready if s2 == capped[0][0]), "") or "")
        avg1 = _mean(next_day_returns)
        avg3 = _mean(next3_returns)
        avg5 = _mean(next5_returns)
        fwd1_vals.append(avg1)
        fwd3_vals.append(avg3)
        fwd5_vals.append(avg5)
        daily_rows.append(
            {
                "date": day,
                "selected_count": len(selected_rows),
                "avg_forward_1d_pct": round(avg1, 3),
                "avg_forward_3d_pct": round(avg3, 3),
                "avg_forward_5d_pct": round(avg5, 3),
                "top1_forward_1d_pct": round(float(selected_rows[0]["forward_1d_pct"]), 3) if selected_rows else 0.0,
                "selected": selected_rows,
            }
        )

    summary = {
        "selection_days": selection_days,
        "avg_selected_per_day": round(_mean([float(row.get("selected_count", 0)) for row in daily_rows]), 2) if daily_rows else 0.0,
        "avg_forward_1d_pct": round(_mean(fwd1_vals), 3) if fwd1_vals else 0.0,
        "avg_forward_3d_pct": round(_mean(fwd3_vals), 3) if fwd3_vals else 0.0,
        "avg_forward_5d_pct": round(_mean(fwd5_vals), 3) if fwd5_vals else 0.0,
        "top1_avg_forward_1d_pct": round(_mean(top1_vals), 3) if top1_vals else 0.0,
        "top1_hit_rate_pct": round((top1_hit / float(len(top1_vals)) * 100.0), 2) if top1_vals else 0.0,
    }
    report = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "report_type": "rolling_rank_study",
        "window_days": int(window_days),
        "seed_count": len(aligned_ready),
        "config": {
            "window_days": int(window_days),
            "seed_n": int(seed_n),
            "data_fetch_limit": int(effective_fetch_limit),
            "strategy": _simulation_strategy_snapshot(s),
        },
        "summary": summary,
        "daily_rows": daily_rows,
    }
    ROLLING_RANK_REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def generate_short_horizon_rank_study(
    *,
    window_days: int = 20,
    seed_n: int = 2000,
    data_fetch_limit: int | None = None,
) -> dict[str, Any]:
    s = load_settings()
    universe = _kr_backtest_seed_symbols(s, limit=max(100, int(seed_n)))
    effective_fetch_limit = max(260, int(data_fetch_limit or 0), int(window_days) + 30)
    ready = _prepare_market_data(market="KR", symbols=universe, fetch_limit=effective_fetch_limit)
    if not ready:
        report = {
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "report_type": "short_horizon_rank_study",
            "window_days": int(window_days),
            "config": {
                "window_days": int(window_days),
                "seed_n": int(seed_n),
                "data_fetch_limit": int(effective_fetch_limit),
                "strategy": _simulation_strategy_snapshot(s),
            },
            "daily_rows": [],
            "summary": {},
        }
        SHORT_HORIZON_RANK_REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        return report

    from bot_runtime import _multi_factor_rank_score as _runtime_rank_score, _load_sector_cache, _resolve_sector_map, _parse_symbol_text_map

    history_floor = max(75, int(window_days) + 30)
    eligible_ready = [(sym, bars) for sym, bars in ready if len(bars) >= history_floor]
    if not eligible_ready:
        report = {
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "report_type": "short_horizon_rank_study",
            "window_days": int(window_days),
            "config": {
                "window_days": int(window_days),
                "seed_n": int(seed_n),
                "data_fetch_limit": int(effective_fetch_limit),
                "strategy": _simulation_strategy_snapshot(s),
            },
            "daily_rows": [],
            "summary": {"error": "not_enough_history"},
        }
        SHORT_HORIZON_RANK_REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        return report

    min_len = min(len(bars) for _, bars in eligible_ready if bars)
    aligned_ready = [(sym, bars[-min_len:]) for sym, bars in eligible_ready]
    sector_cache_path = getattr(s, "sector_cache_path", "data/sector_map_cache.json")
    symbols_only = [sym for sym, _ in aligned_ready]
    symbol_sector_map = _resolve_sector_map(
        symbols=symbols_only,
        manual_map=_parse_symbol_text_map(getattr(s, "symbol_sector_map", "")),
        cache_map=_load_sector_cache(sector_cache_path),
        auto_enabled=bool(getattr(s, "sector_auto_map_enabled", True)),
        cache_path=sector_cache_path,
        fetch_limit=12,
    )

    start_idx = max(60, min_len - max(1, int(window_days)) - 2)
    end_idx = min_len - 3
    daily_rows: list[dict[str, Any]] = []
    fwd1_vals: list[float] = []
    fwd2_vals: list[float] = []
    top1_1d_vals: list[float] = []
    top1_2d_vals: list[float] = []
    top1_1d_hit = 0
    top1_2d_hit = 0
    selection_days = 0

    for idx in range(start_idx, end_idx + 1):
        sliced_ready = [(sym, bars[: idx + 1]) for sym, bars in aligned_ready]
        market_index_pct = _market_proxy_return_pct(sliced_ready, 20)
        scored: list[tuple[str, float, dict[str, float], str]] = []
        for sym, bars in sliced_ready:
            score, factors = _runtime_rank_score(bars, market_index_pct=market_index_pct, settings=s)
            if score <= -900.0:
                continue
            sector = str(symbol_sector_map.get(sym, sym)).strip() or sym
            scored.append((sym, float(score), factors, sector))
        scored.sort(key=lambda row: float(row[1]), reverse=True)

        capped: list[tuple[str, float, dict[str, float], str]] = []
        sector_counts: dict[str, int] = {}
        for sym, score, factors, sector in scored:
            used = int(sector_counts.get(sector, 0))
            if used >= int(getattr(s, "trend_max_sector_names", 2)):
                continue
            capped.append((sym, score, factors, sector))
            sector_counts[sector] = used + 1
            if len(capped) >= max(1, int(getattr(s, "trend_select_count", 5))):
                break
        if not capped:
            continue

        selection_days += 1
        next1_returns: list[float] = []
        next2_returns: list[float] = []
        selected_rows: list[dict[str, Any]] = []
        for rank, (sym, score, factors, sector) in enumerate(capped, start=1):
            full_bars = next((bars for s2, bars in aligned_ready if s2 == sym), [])
            cur_close = float(full_bars[idx].get("close", 0.0))
            ret1 = _pct_change(float(full_bars[idx + 1].get("close", 0.0)), cur_close) if cur_close > 0 else 0.0
            ret2 = _pct_change(float(full_bars[idx + 2].get("close", 0.0)), cur_close) if cur_close > 0 else 0.0
            next1_returns.append(ret1)
            next2_returns.append(ret2)
            if rank == 1:
                top1_1d_vals.append(ret1)
                top1_2d_vals.append(ret2)
                if ret1 > 0:
                    top1_1d_hit += 1
                if ret2 > 0:
                    top1_2d_hit += 1
            selected_rows.append(
                {
                    "rank": rank,
                    "symbol": sym,
                    "sector": sector,
                    "score": round(score, 3),
                    "risk_adjusted_momentum": round(float(factors.get("risk_adjusted_momentum", 0.0)), 3),
                    "trend_efficiency": round(float(factors.get("trend_efficiency", 0.0)), 3),
                    "attention_ratio": round(float(factors.get("attention_ratio", 0.0)), 3),
                    "value_spike_ratio": round(float(factors.get("value_spike_ratio", 0.0)), 3),
                    "forward_1d_pct": round(ret1, 3),
                    "forward_2d_pct": round(ret2, 3),
                }
            )

        day = str(capped and next((bars[idx].get("date") for s2, bars in aligned_ready if s2 == capped[0][0]), "") or "")
        avg1 = _mean(next1_returns)
        avg2 = _mean(next2_returns)
        fwd1_vals.append(avg1)
        fwd2_vals.append(avg2)
        daily_rows.append(
            {
                "date": day,
                "selected_count": len(selected_rows),
                "avg_forward_1d_pct": round(avg1, 3),
                "avg_forward_2d_pct": round(avg2, 3),
                "top1_forward_1d_pct": round(float(selected_rows[0]["forward_1d_pct"]), 3) if selected_rows else 0.0,
                "top1_forward_2d_pct": round(float(selected_rows[0]["forward_2d_pct"]), 3) if selected_rows else 0.0,
                "selected": selected_rows,
            }
        )

    summary = {
        "selection_days": selection_days,
        "avg_selected_per_day": round(_mean([float(row.get("selected_count", 0)) for row in daily_rows]), 2) if daily_rows else 0.0,
        "avg_forward_1d_pct": round(_mean(fwd1_vals), 3) if fwd1_vals else 0.0,
        "avg_forward_2d_pct": round(_mean(fwd2_vals), 3) if fwd2_vals else 0.0,
        "top1_avg_forward_1d_pct": round(_mean(top1_1d_vals), 3) if top1_1d_vals else 0.0,
        "top1_avg_forward_2d_pct": round(_mean(top1_2d_vals), 3) if top1_2d_vals else 0.0,
        "top1_hit_rate_1d_pct": round((top1_1d_hit / float(len(top1_1d_vals)) * 100.0), 2) if top1_1d_vals else 0.0,
        "top1_hit_rate_2d_pct": round((top1_2d_hit / float(len(top1_2d_vals)) * 100.0), 2) if top1_2d_vals else 0.0,
    }
    report = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "report_type": "short_horizon_rank_study",
        "window_days": int(window_days),
        "seed_count": len(aligned_ready),
        "config": {
            "window_days": int(window_days),
            "seed_n": int(seed_n),
            "data_fetch_limit": int(effective_fetch_limit),
            "strategy": _simulation_strategy_snapshot(s),
        },
        "summary": summary,
        "daily_rows": daily_rows,
    }
    SHORT_HORIZON_RANK_REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def generate_rank_weighted_portfolio_study(
    *,
    window_days: int = 20,
    seed_n: int = 2000,
    rank_weights: list[float] | None = None,
    data_fetch_limit: int | None = None,
) -> dict[str, Any]:
    weights = list(rank_weights or [0.50, 0.30, 0.20])
    effective_fetch_limit = max(260, int(data_fetch_limit or 0), int(window_days) + 65)
    base_report = generate_rolling_rank_study(window_days=window_days, seed_n=seed_n, data_fetch_limit=effective_fetch_limit)
    daily_rows_src = list(base_report.get("daily_rows") or [])
    daily_rows: list[dict[str, Any]] = []
    weighted_1d_vals: list[float] = []
    weighted_3d_vals: list[float] = []
    weighted_5d_vals: list[float] = []

    for row in daily_rows_src:
        selected = [x for x in list(row.get("selected") or [])[: len(weights)] if isinstance(x, dict)]
        if not selected:
            continue
        used_weights = weights[: len(selected)]
        total_weight = sum(used_weights) or 1.0
        normalized = [w / total_weight for w in used_weights]
        weighted_1d = sum(float(item.get("forward_1d_pct", 0.0)) * w for item, w in zip(selected, normalized))
        weighted_3d = sum(float(item.get("forward_3d_pct", 0.0)) * w for item, w in zip(selected, normalized))
        weighted_5d = sum(float(item.get("forward_5d_pct", 0.0)) * w for item, w in zip(selected, normalized))
        weighted_hit = sum((1.0 if float(item.get("forward_1d_pct", 0.0)) > 0 else 0.0) * w for item, w in zip(selected, normalized)) * 100.0
        weighted_1d_vals.append(weighted_1d)
        weighted_3d_vals.append(weighted_3d)
        weighted_5d_vals.append(weighted_5d)
        daily_rows.append(
            {
                "date": row.get("date"),
                "selected_count": len(selected),
                "rank_weights": [round(w, 4) for w in normalized],
                "weighted_forward_1d_pct": round(weighted_1d, 3),
                "weighted_forward_3d_pct": round(weighted_3d, 3),
                "weighted_forward_5d_pct": round(weighted_5d, 3),
                "weighted_hit_1d_pct": round(weighted_hit, 2),
                "selected": selected,
            }
        )

    summary = {
        "selection_days": len(daily_rows),
        "rank_weights": [round(x, 4) for x in weights],
        "weighted_avg_forward_1d_pct": round(_mean(weighted_1d_vals), 3) if weighted_1d_vals else 0.0,
        "weighted_avg_forward_3d_pct": round(_mean(weighted_3d_vals), 3) if weighted_3d_vals else 0.0,
        "weighted_avg_forward_5d_pct": round(_mean(weighted_5d_vals), 3) if weighted_5d_vals else 0.0,
        "weighted_hit_rate_1d_pct": round(_mean([float(row.get("weighted_hit_1d_pct", 0.0)) for row in daily_rows]), 2) if daily_rows else 0.0,
    }
    report = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "report_type": "rank_weighted_portfolio_study",
        "window_days": int(window_days),
        "seed_count": int(base_report.get("seed_count", 0)),
        "config": {
            "window_days": int(window_days),
            "seed_n": int(seed_n),
            "rank_weights": [round(x, 4) for x in weights],
            "data_fetch_limit": int(effective_fetch_limit),
            "strategy": _simulation_strategy_snapshot(load_settings()),
        },
        "summary": summary,
        "daily_rows": daily_rows,
    }
    RANK_WEIGHTED_REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def generate_daily_selection_portfolio_report(
    *,
    window_days: int = 20,
    seed_n: int = 2000,
    initial_cash: float | None = None,
    max_hold_days: int = 2,
    relaxed_selected_entry: bool = False,
    selected_continuation_probe: bool = False,
    data_fetch_limit: int | None = None,
) -> dict[str, Any]:
    s = load_settings()
    cash = float(initial_cash if initial_cash is not None else max(1_000_000.0, float(getattr(s, "initial_cash", 1_000_000.0))))
    initial_cash_value = float(cash)
    universe = _kr_backtest_seed_symbols(s, limit=max(100, int(seed_n)))
    effective_fetch_limit = max(260, int(data_fetch_limit or 0), int(window_days) + 70)
    ready = _prepare_market_data(market="KR", symbols=universe, fetch_limit=effective_fetch_limit)
    if not ready:
        report = {
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "report_type": "daily_selection_portfolio",
            "window_days": int(window_days),
            "config": {
                "window_days": int(window_days),
                "seed_n": int(seed_n),
                "initial_cash": round(initial_cash_value, 2),
                "max_hold_days": int(max_hold_days),
                "data_fetch_limit": int(effective_fetch_limit),
                "relaxed_selected_entry": bool(relaxed_selected_entry),
                "selected_continuation_probe": bool(selected_continuation_probe),
                "strategy": _simulation_strategy_snapshot(s),
            },
            "daily_rows": [],
            "summary": {},
            "trades": [],
        }
        DAILY_SELECTION_PORTFOLIO_REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        return report

    from bot_runtime import _multi_factor_rank_score as _runtime_rank_score, _load_sector_cache, _resolve_sector_map, _parse_symbol_text_map

    history_floor = max(75, int(window_days) + 70)
    eligible_ready = [(sym, bars) for sym, bars in ready if len(bars) >= history_floor]
    if not eligible_ready:
        report = {
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "report_type": "daily_selection_portfolio",
            "window_days": int(window_days),
            "config": {
                "window_days": int(window_days),
                "seed_n": int(seed_n),
                "initial_cash": round(initial_cash_value, 2),
                "max_hold_days": int(max_hold_days),
                "data_fetch_limit": int(effective_fetch_limit),
                "relaxed_selected_entry": bool(relaxed_selected_entry),
                "selected_continuation_probe": bool(selected_continuation_probe),
                "strategy": _simulation_strategy_snapshot(s),
            },
            "daily_rows": [],
            "summary": {"error": "not_enough_history"},
            "trades": [],
        }
        DAILY_SELECTION_PORTFOLIO_REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        return report

    min_len = min(len(bars) for _, bars in eligible_ready if bars)
    aligned_ready = [(sym, bars[-min_len:]) for sym, bars in eligible_ready]
    sector_cache_path = getattr(s, "sector_cache_path", "data/sector_map_cache.json")
    symbols_only = [sym for sym, _ in aligned_ready]
    symbol_sector_map = _resolve_sector_map(
        symbols=symbols_only,
        manual_map=_parse_symbol_text_map(getattr(s, "symbol_sector_map", "")),
        cache_map=_load_sector_cache(sector_cache_path),
        auto_enabled=bool(getattr(s, "sector_auto_map_enabled", True)),
        cache_path=sector_cache_path,
        fetch_limit=12,
    )
    guard = SessionGuardParams(
        market_shock_drop_pct=float(getattr(s, "market_shock_drop_pct", -2.0)),
        vkospi_spike_proxy_pct=float(getattr(s, "vkospi_spike_proxy_pct", 3.8)),
        bearish_exception_trigger_pct=float(getattr(s, "bearish_exception_trigger_pct", -0.4)),
        bearish_exception_max_market_drop_pct=float(getattr(s, "bearish_exception_max_market_drop_pct", -9.0)),
        bearish_exception_max_vol_pct=float(getattr(s, "bearish_exception_max_vol_pct", 3.2)),
    )
    proxy_rets = _market_proxy_returns_pct(aligned_ready)

    start_idx = max(60, min_len - max(1, int(window_days)))
    positions: dict[str, dict[str, Any]] = {}
    trades: list[dict[str, Any]] = []
    daily_rows: list[dict[str, Any]] = []
    equity_curve: list[float] = []
    realized_pnls: list[float] = []
    blocker_totals: Counter[str] = Counter()

    def _mark_to_market(day_idx: int) -> float:
        total = cash
        for sym, pos in positions.items():
            full_bars = next((bars for s2, bars in aligned_ready if s2 == sym), [])
            if not full_bars or day_idx >= len(full_bars):
                continue
            total += float(pos.get("qty", 0)) * float(full_bars[day_idx].get("close", 0.0))
        return total

    for idx in range(start_idx, min_len):
        day = str((aligned_ready[0][1][idx] or {}).get("date") or "")
        sliced_ready = [(sym, bars[: idx + 1]) for sym, bars in aligned_ready]
        market_index_pct = _market_proxy_return_pct(sliced_ready, 20)
        market_chg_pct = float(proxy_rets[idx]) if idx < len(proxy_rets) else 0.0
        scored: list[tuple[str, float, dict[str, float], str, dict[str, Any]]] = []
        stock_diag_map: dict[str, dict[str, Any]] = {}
        for sym, bars in sliced_ready:
            score, factors = _runtime_rank_score(bars, market_index_pct=market_index_pct, settings=s)
            if score <= -900.0:
                continue
            cur_p = float((bars[-1] or {}).get("close", 0.0))
            prev_p = float((bars[-2] or {}).get("close", cur_p)) if len(bars) >= 2 else cur_p
            hist_closes = [float(x.get("close", 0.0)) for x in bars if float(x.get("close", 0.0)) > 0]
            hist_volumes = [int(float(x.get("volume", 0.0))) for x in bars if float(x.get("close", 0.0)) > 0]
            tf = _tech_flags(
                hist_closes=hist_closes[:-1],
                current_price=cur_p,
                current_volume=int(hist_volumes[-1]) if hist_volumes else 0,
                hist_volumes=hist_volumes[:-1],
                params=CURRENT_TECH,
            )
            trend_diag = trend_runtime_diagnostics(
                qty=0,
                trend_ok=bool(factors.get("trend_ok", 0.0)),
                structure_ok=bool(factors.get("structure_ok", 0.0)),
                breakout_ok=bool(factors.get("breakout_ok", 0.0)),
                overheat_flag=bool(factors.get("overheat", 0.0)),
                daily_rsi=float(factors.get("daily_rsi", 50.0)),
                attention_ratio=float(factors.get("attention_ratio", 0.0)),
                value_spike_ratio=float(factors.get("value_spike_ratio", 0.0)),
                gap_from_prev_close_pct=_pct_change(cur_p, prev_p) if prev_p > 0 else 0.0,
                trend_daily_rsi_min=float(getattr(s, "trend_daily_rsi_min", 55.0)),
                trend_daily_rsi_max=float(getattr(s, "trend_daily_rsi_max", 78.0)),
                trend_min_turnover_ratio_5_to_20=float(getattr(s, "trend_min_turnover_ratio_5_to_20", 1.20)),
                trend_min_value_spike_ratio=float(getattr(s, "trend_min_value_spike_ratio", 1.30)),
                trend_gap_skip_up_pct=float(getattr(s, "trend_gap_skip_up_pct", 6.0)),
                trend_gap_skip_down_pct=float(getattr(s, "trend_gap_skip_down_pct", -3.0)),
                trend_max_chase_from_open_pct=float(getattr(s, "trend_max_chase_from_open_pct", 8.0)),
                market_chg_pct=market_chg_pct,
                momentum_pct=float(factors.get("momentum_pct", 0.0)),
                trend_pct=float(factors.get("trend_pct", 0.0)),
                tech_flags=tf,
                prev_price=prev_p,
                current_price=cur_p,
                atr_pct=float(factors.get("atr14_pct", 0.0)),
                volume_ratio=float(factors.get("value_spike_ratio", 0.0)),
            )
            action, _tech_priority, trend_entry_ready = trend_runtime_signal(
                qty=0,
                trend_ok=bool(factors.get("trend_ok", 0.0)),
                structure_ok=bool(factors.get("structure_ok", 0.0)),
                breakout_ok=bool(factors.get("breakout_ok", 0.0)),
                overheat_flag=bool(factors.get("overheat", 0.0)),
                daily_rsi=float(factors.get("daily_rsi", 50.0)),
                attention_ratio=float(factors.get("attention_ratio", 0.0)),
                value_spike_ratio=float(factors.get("value_spike_ratio", 0.0)),
                gap_from_prev_close_pct=_pct_change(cur_p, prev_p) if prev_p > 0 else 0.0,
                trend_daily_rsi_min=float(getattr(s, "trend_daily_rsi_min", 55.0)),
                trend_daily_rsi_max=float(getattr(s, "trend_daily_rsi_max", 78.0)),
                trend_min_turnover_ratio_5_to_20=float(getattr(s, "trend_min_turnover_ratio_5_to_20", 1.20)),
                trend_min_value_spike_ratio=float(getattr(s, "trend_min_value_spike_ratio", 1.30)),
                trend_gap_skip_up_pct=float(getattr(s, "trend_gap_skip_up_pct", 6.0)),
                trend_gap_skip_down_pct=float(getattr(s, "trend_gap_skip_down_pct", -3.0)),
                trend_max_chase_from_open_pct=float(getattr(s, "trend_max_chase_from_open_pct", 8.0)),
                market_chg_pct=market_chg_pct,
                momentum_pct=float(factors.get("momentum_pct", 0.0)),
                trend_pct=float(factors.get("trend_pct", 0.0)),
                tech_flags=tf,
                golden_cross_entry_bb_max=CURRENT_TECH.golden_cross_entry_bb_max,
                prev_price=prev_p,
                current_price=cur_p,
                atr_pct=float(factors.get("atr14_pct", 0.0)),
                volume_ratio=float(factors.get("value_spike_ratio", 0.0)),
            )
            sector = str(symbol_sector_map.get(sym, sym)).strip() or sym
            diag = {
                "symbol": sym,
                "score": float(score),
                "action": action,
                "entry_ready": bool(trend_entry_ready),
                "blockers": list(trend_diag.get("blockers") or []),
                "watchlist": bool(trend_diag.get("watchlist", False)),
                "watch_reason": str(trend_diag.get("watch_reason") or ""),
                "market_type": str(trend_diag.get("market_type") or ""),
                "factors": factors,
                "sector": sector,
                "price": cur_p,
                "risk_adjusted_momentum": float(factors.get("risk_adjusted_momentum", 0.0)),
                "trend_efficiency": float(factors.get("trend_efficiency", 0.0)),
                "attention_ratio": float(factors.get("attention_ratio", 0.0)),
                "value_spike_ratio": float(factors.get("value_spike_ratio", 0.0)),
                "daily_rsi": float(factors.get("daily_rsi", 0.0)),
                "momentum_pct": float(factors.get("momentum_pct", 0.0)),
                "trend_pct": float(factors.get("trend_pct", 0.0)),
            }
            stock_diag_map[sym] = diag
            scored.append((sym, float(score), factors, sector, diag))

        scored.sort(key=lambda row: float(row[1]), reverse=True)
        capped: list[tuple[str, float, dict[str, float], str, dict[str, Any]]] = []
        sector_counts: dict[str, int] = {}
        for sym, score, factors, sector, diag in scored:
            used = int(sector_counts.get(sector, 0))
            if used >= int(getattr(s, "trend_max_sector_names", 2)):
                continue
            capped.append((sym, score, factors, sector, diag))
            sector_counts[sector] = used + 1
            if len(capped) >= max(1, int(getattr(s, "trend_select_count", 5))):
                break
        selected_symbols = [sym for sym, *_ in capped]
        selected_details: list[dict[str, Any]] = []
        daily_blockers: Counter[str] = Counter()
        for rank, (sym, score, factors, sector, diag) in enumerate(capped, start=1):
            blockers = list(diag.get("blockers") or [])
            if not diag.get("entry_ready", False):
                daily_blockers.update(blockers)
                blocker_totals.update(blockers)
            selected_details.append(
                {
                    "rank": rank,
                    "symbol": sym,
                    "sector": sector,
                    "score": round(float(score), 3),
                    "action": str(diag.get("action") or "HOLD"),
                    "entry_ready": bool(diag.get("entry_ready", False)),
                    "entry_allowed": bool(diag.get("entry_ready", False))
                    and (bool(relaxed_selected_entry) or str(diag.get("action") or "") == "BUY"),
                    "selected_continuation_probe": bool(_selected_continuation_probe_ready(diag)),
                    "market_type": str(diag.get("market_type") or ""),
                    "watchlist": bool(diag.get("watchlist", False)),
                    "watch_reason": str(diag.get("watch_reason") or ""),
                    "blockers": blockers[:5],
                    "risk_adjusted_momentum": round(float(factors.get("risk_adjusted_momentum", 0.0)), 3),
                    "trend_efficiency": round(float(factors.get("trend_efficiency", 0.0)), 3),
                    "attention_ratio": round(float(factors.get("attention_ratio", 0.0)), 3),
                    "value_spike_ratio": round(float(factors.get("value_spike_ratio", 0.0)), 3),
                    "daily_rsi": round(float(factors.get("daily_rsi", 0.0)), 2),
                    "momentum_pct": round(float(factors.get("momentum_pct", 0.0)), 3),
                    "trend_pct": round(float(factors.get("trend_pct", 0.0)), 3),
                }
            )

        sells_today: list[dict[str, Any]] = []
        for sym in list(positions.keys()):
            pos = positions[sym]
            full_bars = next((bars for s2, bars in aligned_ready if s2 == sym), [])
            cur_p = float(full_bars[idx].get("close", 0.0))
            hold_days = int(pos.get("hold_days", 0)) + 1
            pos["hold_days"] = hold_days
            pos["peak_price"] = max(float(pos.get("peak_price", cur_p)), cur_p)
            diag = stock_diag_map.get(sym)
            sell_reason = ""
            if hold_days >= int(max_hold_days):
                sell_reason = "max_hold"
            elif sym not in selected_symbols and hold_days >= 1:
                sell_reason = "rotation_out"
            elif isinstance(diag, dict) and str(diag.get("action")) == "SELL":
                sell_reason = "signal_exit"
            if not sell_reason:
                continue
            qty = int(pos.get("qty", 0))
            avg_price = float(pos.get("avg_price", 0.0))
            if qty <= 0 or cur_p <= 0:
                positions.pop(sym, None)
                continue
            realized = (cur_p - avg_price) * qty
            cash += qty * cur_p
            realized_pnls.append(realized)
            trade = {
                "date": day,
                "symbol": sym,
                "side": "SELL",
                "qty": qty,
                "price": round(cur_p, 2),
                "avg_price": round(avg_price, 2),
                "hold_days": hold_days,
                "realized_pnl": round(realized, 2),
                "reason": sell_reason,
            }
            sells_today.append(trade)
            trades.append(trade)
            positions.pop(sym, None)

        max_positions = max(1, min(int(getattr(s, "trend_select_count", 5)), int(getattr(s, "max_active_positions", 5))))
        open_slots = max(0, max_positions - len(positions))
        buy_candidates = [
            (sym, score, factors, sector, diag)
            for sym, score, factors, sector, diag in capped
            if sym not in positions
            and isinstance(diag, dict)
            and (
                (
                    bool(diag.get("entry_ready", False))
                    and (bool(relaxed_selected_entry) or str(diag.get("action")) == "BUY")
                )
                or (
                    bool(selected_continuation_probe)
                    and _selected_continuation_probe_ready(diag)
                )
            )
        ]
        buys_today: list[dict[str, Any]] = []
        if open_slots > 0 and buy_candidates:
            per_slot_cash = cash / float(open_slots)
            for sym, _score, _factors, _sector, diag in buy_candidates[:open_slots]:
                cur_p = float(diag.get("price", 0.0))
                if cur_p <= 0:
                    continue
                qty = int(per_slot_cash / cur_p)
                if qty <= 0 or (qty * cur_p) > cash:
                    continue
                cash -= qty * cur_p
                positions[sym] = {
                    "qty": qty,
                    "avg_price": cur_p,
                    "entry_date": day,
                    "hold_days": 0,
                    "peak_price": cur_p,
                }
                trade = {
                    "date": day,
                    "symbol": sym,
                    "side": "BUY",
                    "qty": qty,
                    "price": round(cur_p, 2),
                    "reason": (
                        "selected_continuation_probe"
                        if (selected_continuation_probe and _selected_continuation_probe_ready(diag))
                        else ("selected_entry_relaxed" if relaxed_selected_entry else "selected_entry")
                    ),
                }
                buys_today.append(trade)
                trades.append(trade)

        equity = _mark_to_market(idx)
        equity_curve.append(equity)
        daily_rows.append(
            {
                "date": day,
                "selected_symbols": selected_symbols,
                "selected_details": selected_details,
                "held_symbols": sorted(list(positions.keys())),
                "buy_count": len(buys_today),
                "sell_count": len(sells_today),
                "cash": round(cash, 2),
                "equity": round(equity, 2),
                "top_blockers": [{"name": key, "count": value} for key, value in daily_blockers.most_common(5)],
                "buys": buys_today,
                "sells": sells_today,
            }
        )

    peak = 0.0
    drawdowns: list[float] = []
    for eq in equity_curve:
        peak = max(peak, eq)
        dd = ((eq - peak) / peak * 100.0) if peak > 0 else 0.0
        drawdowns.append(dd)
    sell_trades = [t for t in trades if str(t.get("side")) == "SELL"]
    win_sells = [t for t in sell_trades if float(t.get("realized_pnl", 0.0)) > 0]
    loss_sells = [t for t in sell_trades if float(t.get("realized_pnl", 0.0)) < 0]
    summary = {
        "selection_days": len(daily_rows),
        "trade_count": len(trades),
        "buy_count": sum(1 for t in trades if str(t.get("side")) == "BUY"),
        "sell_count": len(sell_trades),
        "win_rate": round((len(win_sells) / float(len(sell_trades)) * 100.0), 2) if sell_trades else 0.0,
        "realized_pnl": round(sum(float(t.get("realized_pnl", 0.0)) for t in sell_trades), 2),
        "ending_equity": round(equity_curve[-1] if equity_curve else cash, 2),
        "return_pct": round((((equity_curve[-1] if equity_curve else cash) / initial_cash_value) - 1.0) * 100.0, 3) if initial_cash_value > 0 else 0.0,
        "max_drawdown_pct": round(min(drawdowns) if drawdowns else 0.0, 3),
        "avg_win_pnl": round(_mean([float(t.get("realized_pnl", 0.0)) for t in win_sells]), 2) if win_sells else 0.0,
        "avg_loss_pnl": round(_mean([float(t.get("realized_pnl", 0.0)) for t in loss_sells]), 2) if loss_sells else 0.0,
        "avg_hold_days": round(_mean([float(t.get("hold_days", 0.0)) for t in sell_trades]), 2) if sell_trades else 0.0,
        "top_entry_blockers": [{"name": key, "count": value} for key, value in blocker_totals.most_common(10)],
    }
    report = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "report_type": "daily_selection_portfolio",
        "window_days": int(window_days),
        "seed_count": len(aligned_ready),
        "relaxed_selected_entry": bool(relaxed_selected_entry),
        "selected_continuation_probe": bool(selected_continuation_probe),
        "config": {
            "window_days": int(window_days),
            "seed_n": int(seed_n),
            "initial_cash": round(initial_cash_value, 2),
            "max_hold_days": int(max_hold_days),
            "data_fetch_limit": int(effective_fetch_limit),
            "relaxed_selected_entry": bool(relaxed_selected_entry),
            "selected_continuation_probe": bool(selected_continuation_probe),
            "strategy": _simulation_strategy_snapshot(s),
        },
        "summary": summary,
        "daily_rows": daily_rows,
        "trades": trades,
    }
    DAILY_SELECTION_PORTFOLIO_REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def generate_intraday_selected_replay_report(*, window_days: int = 20, target_day: str = "") -> dict[str, Any]:
    rows = _load_selected_intraday_rows()
    days = sorted({str(row.get("bar_ts") or "")[:10] for row in rows if str(row.get("bar_ts") or "")[:10]})
    target_day = str(target_day or "").strip()
    if target_day:
        rows = [row for row in rows if str(row.get("bar_ts") or "").startswith(target_day)]
    elif window_days > 0:
        keep_days = set(days[-max(1, int(window_days)):])
        rows = [row for row in rows if str(row.get("bar_ts") or "")[:10] in keep_days]

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        sym = str(row.get("symbol") or "").strip()
        day = str(row.get("bar_ts") or "")[:10]
        if not sym or not day:
            continue
        grouped.setdefault((sym, day), []).append(row)
    for key in grouped:
        grouped[key].sort(key=lambda x: str(x.get("bar_ts") or ""))

    trades: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    symbol_returns: dict[str, list[float]] = {}
    all_bar_returns: list[float] = []
    for (sym, day), bars in grouped.items():
        prices = [float(row.get("price") or 0.0) for row in bars if float(row.get("price") or 0.0) > 0]
        if len(prices) < 2:
            continue
        day_ret = ((prices[-1] / prices[0]) - 1.0) * 100.0 if prices[0] > 0 else 0.0
        symbol_returns.setdefault(sym, []).append(day_ret)
        for i in range(1, len(prices)):
            if prices[i - 1] > 0:
                all_bar_returns.append(((prices[i] / prices[i - 1]) - 1.0) * 100.0)
        buy_rows = [row for row in bars if str(row.get("action") or "").upper() == "BUY" and float(row.get("price") or 0.0) > 0]
        if buy_rows:
            entry = buy_rows[0]
            exit_row = bars[-1]
            buy_price = float(entry.get("price") or 0.0)
            sell_price = float(exit_row.get("price") or 0.0)
            ret_pct = ((sell_price / buy_price) - 1.0) * 100.0 if buy_price > 0 and sell_price > 0 else 0.0
            trades.append(
                {
                    "symbol": sym,
                    "buy_bar": str(entry.get("bar_ts") or ""),
                    "sell_bar": str(exit_row.get("bar_ts") or ""),
                    "buy_price": round(buy_price, 2),
                    "sell_price": round(sell_price, 2),
                    "qty": 0,
                    "return_pct": round(ret_pct, 3),
                    "realized_pnl": round(ret_pct * 1000.0, 2),
                    "hold_bars": max(1, len(bars) - bars.index(entry)),
                    "type": "intraday_replay",
                }
            )

    for sym, vals in symbol_returns.items():
        summary_rows.append(
            {
                "symbol": sym,
                "days": len(vals),
                "avg_return_pct": round(sum(vals) / float(len(vals)), 3),
                "realized_pnl": round(sum(vals) * 1000.0, 2),
            }
        )
    summary_rows.sort(key=lambda row: float(row.get("realized_pnl") or 0.0), reverse=True)
    trades.sort(key=lambda row: str(row.get("buy_bar") or ""))
    trade_returns = [float(row.get("return_pct") or 0.0) for row in trades]
    win_rate = (sum(1 for x in trade_returns if x > 0) / float(len(trade_returns)) * 100.0) if trade_returns else 0.0
    report = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "report_type": "intraday_selected_replay",
        "window_days": int(window_days),
        "target_day": target_day,
        "config": {
            "window_days": int(window_days),
            "target_day": target_day,
            "data_source": "data/selected_intraday_prices.json",
            "strategy": {
                "bar_interval_minutes": 2,
                "decision_on_bar_close_only": True,
                "selection_style": "selected_intraday_replay",
                "ranking_factors": ["price", "score", "trend_pct", "attention", "spike"],
                "entry_style": "stored_buy_signal_replay",
                "risk_guards": ["replay_only", "selected_symbols_only"],
            },
        },
        "summary_rows": summary_rows[:20],
        "summary": {
            "trade_count": len(trades),
            "win_rate_pct": round(win_rate, 1),
            "avg_return_pct": round(sum(trade_returns) / float(len(trade_returns)), 3) if trade_returns else 0.0,
            "bar_count": len(rows),
            "symbol_count": len(summary_rows),
            "day_count": len({day for _, day in grouped.keys()}),
            "avg_bar_return_pct": round(sum(all_bar_returns) / float(len(all_bar_returns)), 4) if all_bar_returns else 0.0,
        },
        "trades": trades[-100:],
    }
    INTRADAY_REPLAY_REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def _load_intraday_scaled_rows() -> list[dict[str, Any]]:
    return _load_selected_intraday_rows()


def generate_intraday_scalping_report(
    *,
    window_days: int = 20,
    target_day: str = "",
    params: ScalpParams = ScalpParams(),
    position_size: int = 100,
    initial_cash: float = 10000000.0,
) -> dict[str, Any]:
    rows = _load_intraday_scaled_rows()
    days = sorted({str(row.get("bar_ts") or "")[:10] for row in rows if str(row.get("bar_ts") or "")[:10]})
    target_day = str(target_day or "").strip()
    if target_day:
        rows = [row for row in rows if str(row.get("bar_ts") or "").startswith(target_day)]
    elif window_days > 0:
        keep = set(days[-max(1, int(window_days)):])
        rows = [row for row in rows if str(row.get("bar_ts") or "")[:10] in keep]

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        sym = str(row.get("symbol") or "").strip()
        day = str(row.get("bar_ts") or "")[:10]
        if not sym or not day:
            continue
        grouped.setdefault((sym, day), []).append(row)

    for key in grouped:
        grouped[key].sort(key=lambda x: str(x.get("bar_ts") or ""))

    trades: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    symbol_returns: dict[str, list[float]] = {}

    for (sym, day), bar_rows in grouped.items():
        bars = []
        for row in bar_rows:
            close = float(row.get("price") or 0.0)
            if close <= 0:
                continue
            volume = float(row.get("quote_volume") or row.get("vol", 0.0) or 0.0)
            bars.append({
                "bar_ts": str(row.get("bar_ts") or ""),
                "close": close,
                "open": float(row.get("price") or 0.0),
                "high": float(row.get("price") or 0.0),
                "low": float(row.get("price") or 0.0),
                "volume": volume,
            })
        if len(bars) < 12:
            continue

        position_open = False
        entry_price = 0.0
        entry_idx = 0
        for i, bar in enumerate(bars):
            hist = bars[: i + 1]
            metrics = calculate_scalp_metrics(hist, params)
            price = float(bar.get("close", 0.0))
            if not position_open and scalp_entry_signal(metrics, params):
                position_open = True
                entry_price = price
                entry_idx = i
                trades.append(
                    {
                        "symbol": sym,
                        "day": day,
                        "side": "BUY",
                        "bar_ts": bar.get("bar_ts", ""),
                        "price": round(price, 2),
                        "qty": position_size,
                        "entry_rsi": round(metrics.get("rsi", 0.0), 2),
                        "entry_volume_ratio": round(metrics.get("volume_ratio", 0.0), 2),
                    }
                )
                continue

            if position_open:
                hold_bars = i - entry_idx
                exit_reason = scalp_exit_signal(entry_price, price, hold_bars, float(metrics.get("rsi", 50.0)), params)
                if exit_reason:
                    pnl_pct = ((price / entry_price) - 1.0) * 100.0
                    trades.append(
                        {
                            "symbol": sym,
                            "day": day,
                            "side": "SELL",
                            "bar_ts": bar.get("bar_ts", ""),
                            "price": round(price, 2),
                            "qty": position_size,
                            "pnl_pct": round(pnl_pct, 3),
                            "hold_bars": hold_bars,
                            "exit_reason": exit_reason,
                        }
                    )
                    position_open = False
                    entry_price = 0.0

        if position_open and bars:
            price = float(bars[-1].get("close", 0.0))
            pnl_pct = ((price / entry_price) - 1.0) * 100.0
            trades.append(
                {
                    "symbol": sym,
                    "day": day,
                    "side": "SELL",
                    "bar_ts": bars[-1].get("bar_ts", ""),
                    "price": round(price, 2),
                    "qty": position_size,
                    "pnl_pct": round(pnl_pct, 3),
                    "hold_bars": max(1, len(bars) - entry_idx),
                    "exit_reason": "END_OF_DAY",
                }
            )

    # aggregate
    sell_trades = [t for t in trades if str(t.get("side") or "").upper() == "SELL"]
    wins = [t for t in sell_trades if float(t.get("pnl_pct", 0.0)) > 0]
    losses = [t for t in sell_trades if float(t.get("pnl_pct", 0.0)) <= 0]

    for t in sell_trades:
        sym = str(t.get("symbol") or "")
        p = float(t.get("pnl_pct") or 0.0)
        symbol_returns.setdefault(sym, []).append(p)
    for sym, vals in symbol_returns.items():
        summary_rows.append(
            {
                "symbol": sym,
                "days": len(vals),
                "avg_return_pct": round(sum(vals) / float(len(vals)), 3) if vals else 0.0,
                "realized_pnl": round(sum(vals) * position_size, 2),
            }
        )
    summary_rows.sort(key=lambda r: float(r.get("realized_pnl", 0.0)), reverse=True)

    avg_return = _mean([float(x.get("pnl_pct") or 0.0) for x in sell_trades])
    win_rate = (len(wins) / float(len(sell_trades)) * 100.0) if sell_trades else 0.0

    report = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "report_type": "intraday_scalping",
        "window_days": int(window_days),
        "target_day": target_day,
        "config": {
            "window_days": int(window_days),
            "target_day": target_day,
            "strategy": {
                "bar_interval_minutes": int(params.max_hold_bars),
                "position_size": position_size,
                "scalp_params": asdict(params),
            },
        },
        "summary_rows": summary_rows[:100],
        "summary": {
            "trade_count": len(sell_trades),
            "win_rate": round(win_rate, 2),
            "avg_return_pct": round(avg_return, 3),
            "total_return_pct": round(sum(float(t.get("pnl_pct", 0.0)) for t in sell_trades), 3),
            "symbol_count": len(summary_rows),
            "day_count": len(set(str(r.get("day") or "") for r in sell_trades)),
        },
        "trades": sell_trades,
    }

    INTRADAY_SCALPING_REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def _market_proxy_returns_pct(ready: list[tuple[str, list[dict[str, float]]]]) -> list[float]:
    if not ready:
        return []
    min_len = min(len(bars) for _, bars in ready if bars)
    if min_len < 3:
        return []
    norm_matrix: list[list[float]] = []
    for _, bars in ready:
        closes = [float(x.get("close", 0.0)) for x in bars[-min_len:]]
        seg = [float(x) for x in closes[-min_len:]]
        base = seg[0] if seg and seg[0] > 0 else 1.0
        norm_matrix.append([(x / base) * 100.0 for x in seg])
    proxy: list[float] = []
    for t in range(min_len):
        proxy.append(_mean([row[t] for row in norm_matrix]))
    rets: list[float] = [0.0]
    for t in range(1, len(proxy)):
        prev = float(proxy[t - 1])
        cur = float(proxy[t])
        rets.append(((cur - prev) / prev) * 100.0 if prev > 0 else 0.0)
    return rets


def _market_proxy_return_pct(
    ready: list[tuple[str, list[dict[str, float]]]],
    lookback_days: int,
) -> float:
    if not ready:
        return 0.0
    min_len = min(len(bars) for _, bars in ready if bars)
    if min_len < max(3, int(lookback_days) + 1):
        return 0.0
    norm_matrix: list[list[float]] = []
    for _, bars in ready:
        closes = [float(x.get("close", 0.0)) for x in bars[-min_len:]]
        if not closes:
            continue
        base = closes[0] if closes[0] > 0 else 1.0
        norm_matrix.append([(x / base) * 100.0 for x in closes])
    if not norm_matrix:
        return 0.0
    proxy = [_mean([row[t] for row in norm_matrix]) for t in range(min_len)]
    newer = float(proxy[-1])
    older_idx = max(0, len(proxy) - int(lookback_days) - 1)
    older = float(proxy[older_idx])
    return _pct_change(newer, older)


def _run_market(
    *,
    market: str,
    symbols: list[str],
    days: int,
    cash: float,
    buy_drop_pct: float,
    sell_rise_pct: float,
    signal_confirm_cycles: int,
    params: TechParams,
    guard_params: SessionGuardParams,
    settings: Any,
) -> dict[str, float]:
    fetch_limit = max(180, int(days) + 120)
    ready: list[tuple[str, list[dict[str, float]]]] = []
    for sym in symbols:
        if market == "US":
            bars = _fetch_stooq_daily_bars_us(sym, limit=fetch_limit)
        else:
            bars = _fetch_yahoo_daily_bars_kr(sym, limit=fetch_limit)
        if len(bars) >= 50:
            ready.append((sym, bars))
    if not ready:
        return {"ret": 0.0, "trades": 0.0, "win_rate": 0.0, "mdd": 0.0, "count": 0.0}
    try:
        from bot_runtime import _multi_factor_rank_score as _runtime_rank_score, _load_sector_cache, _resolve_sector_map, _parse_symbol_text_map
        market_index_pct = _market_proxy_return_pct(ready, 20)
        sector_cache_path = getattr(settings, "sector_cache_path", "data/sector_map_cache.json")
        symbols_only = [sym for sym, _bars in ready]
        symbol_sector_map = _resolve_sector_map(
            symbols=symbols_only,
            manual_map=_parse_symbol_text_map(getattr(settings, "symbol_sector_map", "")),
            cache_map=_load_sector_cache(sector_cache_path),
            auto_enabled=bool(getattr(settings, "sector_auto_map_enabled", True)),
            cache_path=sector_cache_path,
            fetch_limit=12,
        )
        filtered_ready: list[tuple[str, list[dict[str, float]], float, str]] = []
        for sym, bars in ready:
            score, _factors = _runtime_rank_score(bars, market_index_pct=market_index_pct, settings=settings)
            if score <= -900.0:
                continue
            sector = str(symbol_sector_map.get(sym, sym)).strip() or sym
            filtered_ready.append((sym, bars, float(score), sector))
        filtered_ready.sort(key=lambda row: float(row[2]), reverse=True)
        capped: list[tuple[str, list[dict[str, float]]]] = []
        sector_counts: dict[str, int] = {}
        for sym, bars, _score, sector in filtered_ready:
            used = int(sector_counts.get(sector, 0))
            if used >= int(getattr(settings, "trend_max_sector_names", 2)):
                continue
            capped.append((sym, bars))
            sector_counts[sector] = used + 1
        if capped:
            ready = capped
    except Exception:
        pass
    per_cash = float(cash) / float(len(ready))
    market_proxy_rets = _market_proxy_returns_pct(ready)
    symbol_returns: list[float] = []
    total_trades = 0.0
    total_sells = 0.0
    total_wins_weighted = 0.0
    total_shock_blocked = 0.0
    mdds: list[float] = []
    avg_wins: list[float] = []
    avg_losses: list[float] = []
    expectancies: list[float] = []
    profit_factors: list[float] = []
    max_loss_streaks: list[float] = []
    for _sym, bars in ready:
        r = _simulate_symbol(
            market=market,
            bars=bars,
            days=days,
            initial_cash=per_cash,
            buy_drop_pct=buy_drop_pct,
            sell_rise_pct=sell_rise_pct,
            signal_confirm_cycles=signal_confirm_cycles,
            params=params,
            guard_params=guard_params,
            settings=settings,
            market_change_pct_series=market_proxy_rets,
        )
        final_equity = float(r["final_equity"])
        symbol_returns.append(((final_equity - per_cash) / per_cash * 100.0) if per_cash > 0 else 0.0)
        total_trades += float(r["trade_count"])
        sells = float(r["sell_count"])
        total_sells += sells
        total_wins_weighted += (float(r["win_rate"]) / 100.0) * sells
        total_shock_blocked += float(r.get("shock_blocked_buys", 0.0))
        mdds.append(float(r["mdd"]))
        avg_wins.append(float(r.get("avg_win", 0.0)))
        avg_losses.append(float(r.get("avg_loss_abs", 0.0)))
        expectancies.append(float(r.get("expectancy", 0.0)))
        profit_factors.append(float(r.get("profit_factor", 0.0)))
        max_loss_streaks.append(float(r.get("max_loss_streak", 0.0)))
    ret = _mean(symbol_returns) if symbol_returns else 0.0
    win_rate = (total_wins_weighted / total_sells * 100.0) if total_sells > 0 else 0.0
    avg_mdd = _mean(mdds) if mdds else 0.0
    return {
        "ret": ret,
        "trades": total_trades,
        "win_rate": win_rate,
        "mdd": avg_mdd,
        "count": float(len(ready)),
        "shock_blocked_buys": total_shock_blocked,
        "avg_win": _mean(avg_wins),
        "avg_loss_abs": _mean(avg_losses),
        "expectancy": _mean(expectancies),
        "profit_factor": _mean([x for x in profit_factors if x < 900.0]) if any(x < 900.0 for x in profit_factors) else _mean(profit_factors),
        "max_loss_streak": max(max_loss_streaks) if max_loss_streaks else 0.0,
    }


def _prepare_market_data(
    *,
    market: str,
    symbols: list[str],
    fetch_limit: int,
) -> list[tuple[str, list[dict[str, float]]]]:
    ready: list[tuple[str, list[dict[str, float]]]] = []
    for sym in symbols:
        bars = _load_cached_bars(market, sym, limit=fetch_limit)
        missing_dates = bool(bars) and not any(str((row or {}).get("date") or "").strip() for row in bars[-10:])
        cache_short = len(bars) < max(30, int(fetch_limit))
        if missing_dates:
            bars = []
        if not bars or cache_short:
            fetched = _fetch_market_bars(market, sym, limit=fetch_limit)
            if fetched and len(fetched) >= len(bars):
                bars = fetched
            if bars:
                _save_cached_bars(market, sym, bars)
        if market == "KR" and _has_corporate_action_like_gap(bars):
            continue
        if len(bars) >= 50:
            ready.append((sym, bars))
    return ready


def _run_market_ready(
    *,
    ready: list[tuple[str, list[dict[str, float]]]],
    days: int,
    cash: float,
    buy_drop_pct: float,
    sell_rise_pct: float,
    signal_confirm_cycles: int,
    params: TechParams,
    guard_params: SessionGuardParams,
    settings: Any,
) -> dict[str, float]:
    if not ready:
        return {"ret": 0.0, "trades": 0.0, "win_rate": 0.0, "mdd": 0.0, "count": 0.0}
    try:
        from bot_runtime import _multi_factor_rank_score as _runtime_rank_score, _load_sector_cache, _resolve_sector_map, _parse_symbol_text_map
        market_index_pct = _market_proxy_return_pct(ready, 20)
        sector_cache_path = getattr(settings, "sector_cache_path", "data/sector_map_cache.json")
        symbols_only = [sym for sym, _bars in ready]
        symbol_sector_map = _resolve_sector_map(
            symbols=symbols_only,
            manual_map=_parse_symbol_text_map(getattr(settings, "symbol_sector_map", "")),
            cache_map=_load_sector_cache(sector_cache_path),
            auto_enabled=bool(getattr(settings, "sector_auto_map_enabled", True)),
            cache_path=sector_cache_path,
            fetch_limit=12,
        )
        filtered_ready: list[tuple[str, list[dict[str, float]], float, str]] = []
        for sym, bars in ready:
            score, _factors = _runtime_rank_score(bars, market_index_pct=market_index_pct, settings=settings)
            if score <= -900.0:
                continue
            sector = str(symbol_sector_map.get(sym, sym)).strip() or sym
            filtered_ready.append((sym, bars, float(score), sector))
        filtered_ready.sort(key=lambda row: float(row[2]), reverse=True)
        capped: list[tuple[str, list[dict[str, float]]]] = []
        sector_counts: dict[str, int] = {}
        for sym, bars, _score, sector in filtered_ready:
            used = int(sector_counts.get(sector, 0))
            if used >= int(getattr(settings, "trend_max_sector_names", 2)):
                continue
            capped.append((sym, bars))
            sector_counts[sector] = used + 1
        if capped:
            ready = capped
    except Exception:
        pass
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
    total_trades = 0.0
    total_sells = 0.0
    total_wins_weighted = 0.0
    total_shock_blocked = 0.0
    mdds: list[float] = []
    avg_wins: list[float] = []
    avg_losses: list[float] = []
    expectancies: list[float] = []
    profit_factors: list[float] = []
    max_loss_streaks: list[float] = []
    for _sym, bars in ready:
        r = _simulate_symbol(
            market="KR" if all(ch.isdigit() for ch in _sym) else "US",
            bars=bars,
            days=days,
            initial_cash=per_cash,
            buy_drop_pct=buy_drop_pct,
            sell_rise_pct=sell_rise_pct,
            signal_confirm_cycles=signal_confirm_cycles,
            params=params,
            guard_params=guard_params,
            settings=settings,
            market_change_pct_series=market_proxy_rets,
        )
        final_equity = float(r["final_equity"])
        symbol_returns.append(((final_equity - per_cash) / per_cash * 100.0) if per_cash > 0 else 0.0)
        total_trades += float(r["trade_count"])
        sells = float(r["sell_count"])
        total_sells += sells
        total_wins_weighted += (float(r["win_rate"]) / 100.0) * sells
        total_shock_blocked += float(r.get("shock_blocked_buys", 0.0))
        mdds.append(float(r["mdd"]))
        avg_wins.append(float(r.get("avg_win", 0.0)))
        avg_losses.append(float(r.get("avg_loss_abs", 0.0)))
        expectancies.append(float(r.get("expectancy", 0.0)))
        profit_factors.append(float(r.get("profit_factor", 0.0)))
        max_loss_streaks.append(float(r.get("max_loss_streak", 0.0)))
    ret = _mean(symbol_returns) if symbol_returns else 0.0
    win_rate = (total_wins_weighted / total_sells * 100.0) if total_sells > 0 else 0.0
    avg_mdd = _mean(mdds) if mdds else 0.0
    return {
        "ret": ret,
        "trades": total_trades,
        "win_rate": win_rate,
        "mdd": avg_mdd,
        "count": float(len(ready)),
        "shock_blocked_buys": total_shock_blocked,
        "avg_win": _mean(avg_wins),
        "avg_loss_abs": _mean(avg_losses),
        "expectancy": _mean(expectancies),
        "profit_factor": _mean([x for x in profit_factors if x < 900.0]) if any(x < 900.0 for x in profit_factors) else _mean(profit_factors),
        "max_loss_streak": max(max_loss_streaks) if max_loss_streaks else 0.0,
    }


def _score_combo(rep: dict[str, dict[str, float]]) -> float:
    # Prioritize 120d performance and drawdown control with some weight on 60d responsiveness.
    kr60 = rep["KR60"]
    us60 = rep["US60"]
    kr120 = rep["KR120"]
    us120 = rep["US120"]
    ret_term = (
        (0.15 * kr60["ret"])
        + (0.25 * us60["ret"])
        + (0.25 * kr120["ret"])
        + (0.35 * us120["ret"])
    )
    mdd_penalty = (
        (0.10 * abs(kr60["mdd"]))
        + (0.20 * abs(us60["mdd"]))
        + (0.20 * abs(kr120["mdd"]))
        + (0.30 * abs(us120["mdd"]))
    )
    win_term = (0.05 * kr120["win_rate"]) + (0.10 * us120["win_rate"])
    pf_term = (0.30 * min(5.0, kr120.get("profit_factor", 0.0))) + (0.50 * min(5.0, us120.get("profit_factor", 0.0)))
    streak_penalty = (0.40 * kr120.get("max_loss_streak", 0.0)) + (0.60 * us120.get("max_loss_streak", 0.0))
    return ret_term - mdd_penalty + (0.02 * win_term) + pf_term - streak_penalty


def _apply_values_to_env(path: Path, values: dict[str, str]) -> None:
    lines = path.read_text().splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        if "=" not in line or line.strip().startswith("#"):
            out.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in values:
            out.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            out.append(line)
    for k, v in values.items():
        if k not in seen:
            out.append(f"{k}={v}")
    path.write_text("\n".join(out) + "\n")


def _apply_values_to_runtime(path: Path, values: dict[str, str]) -> None:
    data: dict[str, str] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text())
            if isinstance(raw, dict):
                data = {str(k): str(v) for k, v in raw.items()}
        except Exception:
            data = {}
    data.update(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=True, indent=2) + "\n")


def optimize_and_optionally_apply(*, apply_changes: bool) -> None:
    s = load_settings()
    guard = SessionGuardParams(
        market_shock_drop_pct=float(getattr(s, "market_shock_drop_pct", -2.0)),
        vkospi_spike_proxy_pct=float(getattr(s, "vkospi_spike_proxy_pct", 3.8)),
        bearish_exception_trigger_pct=float(getattr(s, "bearish_exception_trigger_pct", -0.4)),
        bearish_exception_max_market_drop_pct=float(getattr(s, "bearish_exception_max_market_drop_pct", -9.0)),
        bearish_exception_max_vol_pct=float(getattr(s, "bearish_exception_max_vol_pct", 3.2)),
    )
    us_symbols = [x.strip().upper() for x in str(s.us_mock_symbols or "").split(",") if x.strip()]
    kr_symbols = _kr_backtest_seed_symbols(s, limit=DEFAULT_BACKTEST_COMPARE_KR_SYMBOLS)
    if not us_symbols:
        us_symbols = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "TSLA"]
    if not kr_symbols:
        kr_symbols = ["005930", "000660", "035420", "005380", "051910", "035720"]

    fetch_limit = 260
    kr_ready = _prepare_market_data(market="KR", symbols=kr_symbols, fetch_limit=fetch_limit)
    us_ready = _prepare_market_data(market="US", symbols=us_symbols, fetch_limit=fetch_limit)
    kr_cash = float(max(1_000_000.0, float(s.initial_cash)))
    us_cash = float(max(1_000_000.0, float(s.us_mock_initial_cash)))
    print(f"Prepared data: KR={len(kr_ready)} symbols, US={len(us_ready)} symbols")
    if not kr_ready or not us_ready:
        print("Insufficient data for optimization.")
        return

    buy_opts = [-0.25, -0.30, -0.35, -0.45, -0.55, -0.70, -0.90]
    sell_opts = [0.35, 0.50, 0.60, 0.80, 1.00, 1.20, 1.40]
    confirm_opts = [1, 2]
    candidates: list[dict[str, Any]] = []
    total = len(buy_opts) * len(sell_opts) * len(confirm_opts)
    idx = 0
    for b in buy_opts:
        for se in sell_opts:
            for c in confirm_opts:
                idx += 1
                rep = {
                    "KR60": _run_market_ready(
                        ready=kr_ready,
                        days=60,
                        cash=kr_cash,
                        buy_drop_pct=b,
                        sell_rise_pct=se,
                        signal_confirm_cycles=c,
                        params=TUNED_TECH,
                        guard_params=guard,
                        settings=s,
                    ),
                    "US60": _run_market_ready(
                        ready=us_ready,
                        days=60,
                        cash=us_cash,
                        buy_drop_pct=b,
                        sell_rise_pct=se,
                        signal_confirm_cycles=c,
                        params=TUNED_TECH,
                        guard_params=guard,
                        settings=s,
                    ),
                    "KR120": _run_market_ready(
                        ready=kr_ready,
                        days=120,
                        cash=kr_cash,
                        buy_drop_pct=b,
                        sell_rise_pct=se,
                        signal_confirm_cycles=c,
                        params=TUNED_TECH,
                        guard_params=guard,
                        settings=s,
                    ),
                    "US120": _run_market_ready(
                        ready=us_ready,
                        days=120,
                        cash=us_cash,
                        buy_drop_pct=b,
                        sell_rise_pct=se,
                        signal_confirm_cycles=c,
                        params=TUNED_TECH,
                        guard_params=guard,
                        settings=s,
                    ),
                }
                candidates.append(
                    {
                        "buy_drop_pct": b,
                        "sell_rise_pct": se,
                        "signal_confirm_cycles": c,
                        "score": _score_combo(rep),
                        "report": rep,
                    }
                )
                if (idx % 20) == 0 or idx == total:
                    print(f"progress {idx}/{total}")
    candidates.sort(key=lambda x: float(x["score"]), reverse=True)
    top = candidates[:5]
    print("\nTop 5 parameter sets:")
    for i, row in enumerate(top, start=1):
        rep = row["report"]
        print(
            f"{i}. buy={row['buy_drop_pct']:+.2f} sell={row['sell_rise_pct']:+.2f} "
            f"confirm={row['signal_confirm_cycles']} score={row['score']:+.3f} "
            f"| US120={rep['US120']['ret']:+.2f}% PF={rep['US120']['profit_factor']:.2f} "
            f"EX={rep['US120']['expectancy']:+.0f} LStreak={int(rep['US120']['max_loss_streak'])} "
            f"| KR120={rep['KR120']['ret']:+.2f}% PF={rep['KR120']['profit_factor']:.2f}"
        )
    best = top[0]
    b = float(best["buy_drop_pct"])
    se = float(best["sell_rise_pct"])
    c = int(best["signal_confirm_cycles"])
    print(
        "\nBest selected: "
        + f"BUY_DROP_PCT={b:+.2f}, SELL_RISE_PCT={se:+.2f}, SIGNAL_CONFIRM_CYCLES={c}"
    )
    if not apply_changes:
        print("Dry-run only. Re-run with --apply to write .env and runtime config.")
        return
    values = {
        "BUY_DROP_PCT": f"{b:.2f}",
        "SELL_RISE_PCT": f"{se:.2f}",
        "SIGNAL_CONFIRM_CYCLES": str(c),
        "US_MOCK_BUY_DROP_PCT": f"{b:.2f}",
        "US_MOCK_SELL_RISE_PCT": f"{se:.2f}",
        "US_MOCK_SIGNAL_CONFIRM_CYCLES": str(c),
    }
    _apply_values_to_env(Path(".env"), values)
    _apply_values_to_runtime(Path("data/runtime_config.json"), values)
    print("Applied best parameters to .env and data/runtime_config.json")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--optimize", action="store_true", help="Run parameter sweep")
    parser.add_argument("--apply", action="store_true", help="Apply best parameters to .env/runtime_config")
    parser.add_argument("--rolling-study", action="store_true", help="Run rolling rank study on cached KRX universe")
    parser.add_argument("--short-horizon-study", action="store_true", help="Run 1d/2d short-horizon rolling study")
    parser.add_argument("--rank-weighted-study", action="store_true", help="Run rank-weighted top1/top2/top3 portfolio study")
    parser.add_argument("--daily-selection-portfolio", action="store_true", help="Run daily reselection portfolio backtest")
    parser.add_argument("--intraday-scalping", action="store_true", help="Run intraday scalping replay on saved 10-minute selection data")
    parser.add_argument("--relaxed-selected-entry", action="store_true", help="Allow selected+entry_ready names to buy even if final action is HOLD")
    parser.add_argument("--selected-continuation-probe", action="store_true", help="Backtest-only probe for selected healthy-trend continuation names that remain HOLD")
    args = parser.parse_args()
    if args.daily_selection_portfolio:
        report = generate_daily_selection_portfolio_report(
            window_days=20,
            seed_n=2000,
            relaxed_selected_entry=bool(args.relaxed_selected_entry),
            selected_continuation_probe=bool(args.selected_continuation_probe),
        )
        summary = dict(report.get("summary") or {})
        print("Daily Selection Portfolio (KR, last 20 trading days)")
        print(
            f"seed_count={int(report.get('seed_count', 0))} "
            f"selection_days={int(summary.get('selection_days', 0))} "
            f"buy_count={int(summary.get('buy_count', 0))} sell_count={int(summary.get('sell_count', 0))}"
        )
        print(
            f"return={float(summary.get('return_pct', 0.0)):+.3f}% "
            f"realized_pnl={float(summary.get('realized_pnl', 0.0)):+.0f} "
            f"win_rate={float(summary.get('win_rate', 0.0)):.1f}% "
            f"mdd={float(summary.get('max_drawdown_pct', 0.0)):+.3f}% "
            f"avg_hold={float(summary.get('avg_hold_days', 0.0)):.2f}d"
        )
        print(f"saved={DAILY_SELECTION_PORTFOLIO_REPORT_PATH}")
        return
    if args.intraday_scalping:
        report = generate_intraday_scalping_report(window_days=20, target_day="")
        summary = dict(report.get("summary") or {})
        print("Intraday Scalping Replay")
        print(
            f"trade_count={int(summary.get('trade_count', 0))} "
            f"win_rate={float(summary.get('win_rate', 0.0)):.1f}% "
            f"avg_return_pct={float(summary.get('avg_return_pct', 0.0)):+.3f}% "
            f"total_return_pct={float(summary.get('total_return_pct', 0.0)):+.3f}%"
        )
        print(f"saved={INTRADAY_SCALPING_REPORT_PATH}")
        return
    if args.short_horizon_study:
        report = generate_short_horizon_rank_study(window_days=20, seed_n=2000)
        summary = dict(report.get("summary") or {})
        print("Short Horizon Rank Study (KR, last 20 trading days)")
        print(f"seed_count={int(report.get('seed_count', 0))} selection_days={int(summary.get('selection_days', 0))}")
        print(
            "avg_selected_per_day="
            f"{float(summary.get('avg_selected_per_day', 0.0)):.2f} "
            f"avg_fwd1={float(summary.get('avg_forward_1d_pct', 0.0)):+.3f}% "
            f"avg_fwd2={float(summary.get('avg_forward_2d_pct', 0.0)):+.3f}%"
        )
        print(
            "top1_avg_fwd1="
            f"{float(summary.get('top1_avg_forward_1d_pct', 0.0)):+.3f}% "
            f"top1_hit1={float(summary.get('top1_hit_rate_1d_pct', 0.0)):.1f}% "
            f"top1_avg_fwd2={float(summary.get('top1_avg_forward_2d_pct', 0.0)):+.3f}% "
            f"top1_hit2={float(summary.get('top1_hit_rate_2d_pct', 0.0)):.1f}%"
        )
        print(f"saved={SHORT_HORIZON_RANK_REPORT_PATH}")
        return
    if args.rank_weighted_study:
        report = generate_rank_weighted_portfolio_study(window_days=20, seed_n=2000)
        summary = dict(report.get("summary") or {})
        print("Rank Weighted Portfolio Study (KR, last 20 trading days)")
        print(
            f"seed_count={int(report.get('seed_count', 0))} "
            f"selection_days={int(summary.get('selection_days', 0))} "
            f"weights={summary.get('rank_weights')}"
        )
        print(
            f"weighted_fwd1={float(summary.get('weighted_avg_forward_1d_pct', 0.0)):+.3f}% "
            f"weighted_fwd3={float(summary.get('weighted_avg_forward_3d_pct', 0.0)):+.3f}% "
            f"weighted_fwd5={float(summary.get('weighted_avg_forward_5d_pct', 0.0)):+.3f}% "
            f"weighted_hit1={float(summary.get('weighted_hit_rate_1d_pct', 0.0)):.1f}%"
        )
        print(f"saved={RANK_WEIGHTED_REPORT_PATH}")
        return
    if args.rolling_study:
        report = generate_rolling_rank_study(window_days=20, seed_n=2000)
        summary = dict(report.get("summary") or {})
        print("Rolling Rank Study (KR, last 20 trading days)")
        print(f"seed_count={int(report.get('seed_count', 0))} selection_days={int(summary.get('selection_days', 0))}")
        print(
            "avg_selected_per_day="
            f"{float(summary.get('avg_selected_per_day', 0.0)):.2f} "
            f"avg_fwd1={float(summary.get('avg_forward_1d_pct', 0.0)):+.3f}% "
            f"avg_fwd3={float(summary.get('avg_forward_3d_pct', 0.0)):+.3f}% "
            f"avg_fwd5={float(summary.get('avg_forward_5d_pct', 0.0)):+.3f}%"
        )
        print(
            "top1_avg_fwd1="
            f"{float(summary.get('top1_avg_forward_1d_pct', 0.0)):+.3f}% "
            f"top1_hit_rate={float(summary.get('top1_hit_rate_pct', 0.0)):.1f}%"
        )
        print(f"saved={ROLLING_RANK_REPORT_PATH}")
        return
    if args.optimize:
        optimize_and_optionally_apply(apply_changes=bool(args.apply))
        return
    s = load_settings()
    guard = SessionGuardParams(
        market_shock_drop_pct=float(getattr(s, "market_shock_drop_pct", -2.0)),
        vkospi_spike_proxy_pct=float(getattr(s, "vkospi_spike_proxy_pct", 3.8)),
        bearish_exception_trigger_pct=float(getattr(s, "bearish_exception_trigger_pct", -0.4)),
        bearish_exception_max_market_drop_pct=float(getattr(s, "bearish_exception_max_market_drop_pct", -9.0)),
        bearish_exception_max_vol_pct=float(getattr(s, "bearish_exception_max_vol_pct", 3.2)),
    )
    us_symbols = [x.strip().upper() for x in str(s.us_mock_symbols or "").split(",") if x.strip()]
    kr_symbols = _kr_backtest_seed_symbols(s, limit=DEFAULT_BACKTEST_COMPARE_KR_SYMBOLS)
    if not us_symbols:
        us_symbols = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "TSLA"]
    if not kr_symbols:
        kr_symbols = ["005930", "000660", "035420", "005380", "051910", "035720"]

    print("Backtest Compare (Current Strategy): runtime-aligned settings")
    print(f"US symbols={len(us_symbols)} KR symbols={len(kr_symbols)}")
    print("-" * 112)
    print("market  days  mode      symbols  return_pct  trades  win_rate  avg_mdd  pf   expectancy  maxL  shock_blocked")
    print("-" * 132)
    market_cash = {
        "KR": float(max(1_000_000.0, float(s.initial_cash))),
        "US": float(max(1_000_000.0, float(s.us_mock_initial_cash))),
    }
    for days in (60, 120):
        for mkt, symbols in (("KR", kr_symbols), ("US", us_symbols)):
            current = _run_market(
                market=mkt,
                symbols=symbols,
                days=days,
                cash=float(market_cash.get(mkt, 1_000_000.0)),
                buy_drop_pct=float(s.buy_drop_pct),
                sell_rise_pct=float(s.sell_rise_pct),
                signal_confirm_cycles=int(s.signal_confirm_cycles),
                params=CURRENT_TECH,
                guard_params=guard,
                settings=s,
            )
            print(
                f"{mkt:<6}{days:<6}{'current':<10}{int(current['count']):<9}"
                f"{current['ret']:+8.2f}%  {int(current['trades']):<6}{current['win_rate']:>7.1f}%  {current['mdd']:>+7.2f}%"
                f"  {current.get('profit_factor', 0.0):>4.2f}  {current.get('expectancy', 0.0):>+9.0f}  {int(current.get('max_loss_streak', 0.0)):>4}"
                f"  {int(current.get('shock_blocked_buys', 0.0)):>6}"
            )
    print("-" * 132)


if __name__ == "__main__":
    main()
