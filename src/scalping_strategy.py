"""
Scalping Strategy Module
High-frequency trading with 2-minute bars and tight risk management.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

@dataclass
class ScalpParams:
    """Parameters optimized for 2-minute bar scalping
    
    Designed for:
    - 195 bars/trading day (vs 39 with 10-min)
    - 2-10 minute hold times
    - Micro-level price movements
    - Real-time liquidity focus
    """
    # RSI settings (faster for 2-min bars)
    rsi_period: int = 5              # Shorter period = faster response
    rsi_entry_min: float = 25.0      # More aggressive buy signals
    rsi_entry_max: float = 75.0      # More aggressive sell signals
    rsi_exit_overbought: float = 80.0    # Tighter exit at extreme
    rsi_exit_oversold: float = 20.0      # Tighter exit at extreme
    
    # Profit targets (tighter for scalping)
    profit_target_pct: float = 0.3   # Quick 0.3% profits
    stop_loss_pct: float = 0.25      # Tight 0.25% stops
    
    # Time constraints (exit faster)
    max_hold_bars: int = 2           # 4 minutes max hold (2 × 2-min)
    
    # Volume requirements (relaxed for 2-min)
    min_volume_ratio: float = 1.2    # Lower threshold
    volume_spike_threshold: float = 1.3  # Slight spike needed
    
    # Trend settings (sensitive to micro-moves)
    trend_strength_threshold: float = 0.05  # Catch very small trends
    momentum_threshold: float = 0.05        # 0.05% move over 2 bars
    
    # New: ATR-based volatility (better than fixed %)
    use_atr_stops: bool = True       # Use ATR instead of fixed %
    atr_period: int = 10             # ATR calculation period
    atr_multiplier_stop: float = 1.0  # 1x ATR for stop
    atr_multiplier_target: float = 1.5  # 1.5x ATR for target

def calculate_scalp_metrics(bars: List[Dict[str, float]], params: ScalpParams) -> Dict[str, float]:
    """Calculate metrics optimized for 2-minute scalping"""
    if len(bars) < 15:
        return {}

    closes = [float(x.get("close", 0.0)) for x in bars if float(x.get("close", 0.0)) > 0]
    volumes = [float(x.get("volume", 0.0)) for x in bars if float(x.get("close", 0.0)) > 0]
    highs = [float(x.get("high", 0.0)) for x in bars if float(x.get("close", 0.0)) > 0]
    lows = [float(x.get("low", 0.0)) for x in bars if float(x.get("close", 0.0)) > 0]

    if len(closes) < 15:
        return {}

    # Fast RSI (custom period for 2-min bars)
    rsi = _calculate_rsi(closes, params.rsi_period)

    # Volume spike detection (vs recent average)
    avg_volume = sum(volumes[:-3]) / max(1, len(volumes) - 3) if len(volumes) > 3 else volumes[-1]
    current_volume = volumes[-1]
    volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1.0

    # Micro-trend (3-bar vs 6-bar MA for 2-min sensitivity)
    ma3 = sum(closes[-3:]) / 3 if len(closes) >= 3 else closes[-1]
    ma6 = sum(closes[-6:]) / 6 if len(closes) >= 6 else closes[-1]
    trend_strength = (ma3 - ma6) / ma6 * 100 if ma6 > 0 else 0

    # Quick momentum (last 2 bars for immediate reaction)
    momentum = (closes[-1] - closes[-3]) / closes[-3] * 100 if len(closes) >= 3 else 0

    # ATR for volatility-based targets/stops
    atr = _calculate_atr(highs, lows, closes, params.atr_period) if len(closes) >= params.atr_period else 0
    
    # Intraday price range
    recent_high = max(closes[-10:]) if len(closes) >= 10 else max(closes)
    recent_low = min(closes[-10:]) if len(closes) >= 10 else min(closes)
    intraday_range = recent_high - recent_low

    return {
        "rsi": rsi,
        "volume_ratio": volume_ratio,
        "trend_strength": trend_strength,
        "momentum": momentum,
        "current_price": closes[-1],
        "ma3": ma3,
        "ma6": ma6,
        "atr": atr,
        "intraday_range": intraday_range,
        "recent_high": recent_high,
        "recent_low": recent_low
    }

def scalp_entry_signal(metrics: Dict[str, float], params: ScalpParams) -> bool:
    """Determine if we should enter a scalp trade on 2-minute bars
    
    Requires confluence of multiple factors:
    1. RSI in tradeable range
    2. Volume above baseline
    3. Direction confirmation
    4. Sufficient momentum
    """
    rsi = metrics.get("rsi", 50)
    volume_ratio = metrics.get("volume_ratio", 1.0)
    trend_strength = metrics.get("trend_strength", 0)
    momentum = metrics.get("momentum", 0)

    # Entry conditions for 2-minute scalping
    # More aggressive than longer timeframes
    rsi_ok = params.rsi_entry_min <= rsi <= params.rsi_entry_max
    volume_ok = volume_ratio >= params.min_volume_ratio  # Relaxed to 1.2x
    trend_ok = abs(trend_strength) >= params.trend_strength_threshold  # Very tight, 0.05%
    momentum_ok = abs(momentum) >= params.momentum_threshold  # 0.05% over 2 bars

    # All conditions must align
    return rsi_ok and volume_ok and trend_ok and momentum_ok

def scalp_exit_signal(
    entry_price: float,
    current_price: float,
    hold_bars: int,
    rsi: float,
    params: ScalpParams,
    atr: float = 0
) -> Optional[str]:
    """Determine if we should exit a scalp trade on 2-minute bars
    
    Priority exit order:
    1. Hard stops (profit target, time exit)
    2. Technical signals (RSI extremes)
    3. Stop loss
    """
    price_change_pct = (current_price - entry_price) / entry_price * 100

    # Profit target hit (most important for scalping)
    if price_change_pct >= params.profit_target_pct:
        return "PROFIT_TARGET"

    # Time exit (max hold 4 minutes = 2 bars of 2-min)
    if hold_bars >= params.max_hold_bars:
        return "TIME_EXIT"

    # RSI extreme exits (momentum reversal signals)
    if rsi >= params.rsi_exit_overbought:
        return "RSI_OVERBOUGHT"
    if rsi <= params.rsi_exit_oversold:
        return "RSI_OVERSOLD"

    # Stop loss (last resort, ATR-based if available)
    if params.use_atr_stops and atr > 0:
        atr_stop = (atr * params.atr_multiplier_stop / entry_price) * 100
        if abs(price_change_pct) >= atr_stop:
            return "ATR_STOP"
    else:
        if price_change_pct <= -params.stop_loss_pct:
            return "STOP_LOSS"

    return None

def _calculate_rsi(prices: List[float], period: int = 14) -> float:
    """Calculate RSI for given period (1-5 for scalping, 14+ for swing)"""
    if len(prices) < period + 1:
        return 50.0

    gains = []
    losses = []

    for i in range(1, len(prices)):
        change = prices[i] - prices[i-1]
        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    return rsi


def _calculate_atr(highs: List[float], lows: List[float], closes: List[float], period: int = 10) -> float:
    """Calculate Average True Range for volatility-based stops/targets"""
    if len(highs) < period:
        return 0

    tr_values = []
    for i in range(len(closes)):
        high = highs[i]
        low = lows[i]
        close_prev = closes[i-1] if i > 0 else closes[0]
        
        tr = max(
            high - low,
            abs(high - close_prev),
            abs(low - close_prev)
        )
        tr_values.append(tr)
    
    atr = sum(tr_values[-period:]) / period
    return atr