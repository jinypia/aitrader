#!/usr/bin/env python3
"""
Intraday Stock Selector for Scalping

Filters and ranks stocks by current trading activity metrics
(not daily trends, but real-time liquidity and volatility).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Tuple, Optional, Dict
from collections import defaultdict


def load_intraday_prices() -> Dict[str, list]:
    """Load stored intraday prices with current activity."""
    intraday_path = Path("data/selected_intraday_prices.json")
    
    if not intraday_path.exists():
        return {}
    
    try:
        with open(intraday_path) as f:
            data = json.load(f)
        
        # Group by symbol
        by_symbol = defaultdict(list)
        for row in data.get("rows", []):
            symbol = row.get("symbol", "")
            if symbol:
                by_symbol[symbol].append(row)
        
        return dict(by_symbol)
    except Exception as e:
        print(f"Error loading intraday prices: {e}")
        return {}


def calculate_intraday_metrics(bars: list) -> Dict[str, float]:
    """Calculate real-time scalping metrics for a stock.
    
    Args:
        bars: List of 10-minute bars with price/volume data
        
    Returns:
        Dictionary with liquidity and volatility metrics
    """
    if not bars:
        return {}
    
    prices = [float(b.get("price", 0)) for b in bars if float(b.get("price", 0)) > 0]
    volumes = [float(b.get("volume", 0)) for b in bars if float(b.get("volume", 0)) > 0]
    
    if not prices:
        return {}
    
    # Price range (volatility indicator)
    price_high = max(prices)
    price_low = min(prices)
    price_range = price_high - price_low
    price_range_pct = (price_range / price_low * 100) if price_low > 0 else 0
    
    # Volume analysis
    avg_volume = sum(volumes) / len(volumes) if volumes else 0
    recent_volume = sum(volumes[-5:]) / 5 if len(volumes) >= 5 else avg_volume
    volume_spike = recent_volume / avg_volume if avg_volume > 0 else 1.0
    
    # Momentum (price direction)
    current_price = prices[-1]
    prev_price = prices[-3] if len(prices) >= 3 else prices[0]
    momentum_pct = (current_price - prev_price) / prev_price * 100 if prev_price > 0 else 0
    
    # RSI from stored data
    rsi = 50.0  # Default, would be in the bar data if available
    if bars and len(bars) > 0:
        try:
            # Extract from decision_reason if available
            reason = bars[-1].get("decision_reason", "")
            if "rsi=" in reason:
                rsi_str = reason.split("rsi=")[1].split(" ")[0]
                rsi = float(rsi_str)
        except:
            pass
    
    return {
        "price_current": current_price,
        "price_high": price_high,
        "price_low": price_low,
        "price_range_pct": price_range_pct,
        "volume_spike": volume_spike,
        "momentum_pct": momentum_pct,
        "rsi": rsi,
        "bar_count": len(bars),
        "avg_volume": avg_volume
    }


def score_for_scalping(metrics: Dict[str, float]) -> float:
    """Score a stock 0-100 for scalping suitability.
    
    Prioritizes:
    1. Price range (volatility) - 40%
    2. Volume spike - 40%
    3. RSI availability - 20%
    """
    if not metrics:
        return 0.0
    
    # Volatility score (0.5% to 3% optimal)
    range_pct = metrics.get("price_range_pct", 0)
    volatility_score = min(100, (range_pct / 1.5) * 100)  # Peaks at 1.5%
    
    # Volume spike score (1.2x to 2.5x optimal)
    volume_spike = metrics.get("volume_spike", 1.0)
    volume_score = min(100, (volume_spike / 1.8) * 100)  # Peaks at 1.8x
    
    # RSI score (30-70 range is tradeable)
    rsi = metrics.get("rsi", 50)
    rsi_score = 100 if 30 <= rsi <= 70 else 50
    
    # Weighted score
    score = (
        volatility_score * 0.4 +
        volume_score * 0.4 +
        rsi_score * 0.2
    )
    
    return score


def get_scalping_candidates(
    symbol_filter: Optional[List[str]] = None,
    min_score: float = 40.0,
    limit: int = 20
) -> List[Tuple[str, float, Dict]]:
    """Get top stocks for scalping by current activity.
    
    Args:
        symbol_filter: Optional list of symbols to consider (e.g., top 100 cap)
        min_score: Minimum score to include
        limit: Maximum number of candidates to return
        
    Returns:
        List of (symbol, score, metrics) sorted by score descending
    """
    intraday_data = load_intraday_prices()
    candidates = []
    
    for symbol, bars in intraday_data.items():
        # Filter if provided
        if symbol_filter and symbol not in symbol_filter:
            continue
        
        # Calculate metrics for this symbol
        metrics = calculate_intraday_metrics(bars)
        if not metrics:
            continue
        
        # Score it
        score = score_for_scalping(metrics)
        
        if score >= min_score:
            candidates.append((symbol, score, metrics))
    
    # Sort by score descending
    candidates.sort(key=lambda x: x[1], reverse=True)
    
    return candidates[:limit]


def rank_stocks_by_liquidity(limit: int = 50) -> List[str]:
    """Get top liquid stocks from market cap (best for tight spreads).
    
    Returns:
        List of top symbols by market cap
    """
    # Korean market cap leaders (approximate, hardcoded for reliability)
    top_liquid_symbols = [
        "005930",  # Samsung Electronics
        "000660",  # LG Electronics
        "051910",  # LG Chem
        "035720",  # Kakao
        "068270",  # Naver
        "022100",  # Hyundai Heavy
        "032350",  # Amorepacific
        "003670",  # Poongsan
        "000270",  # Kia Motors
        "157480",  # NAVER Financial
        "028260",  # Samsung SDI
        "207940",  # SK Telecom
        "000810",  # Mirae Asset
        "001570",  # Nexon
        "138040",  # Menuone
        "003550",  # LG Corp
        "034730",  # SK Networks
        "015760",  # NSC
    ]
    
    return top_liquid_symbols[:limit]


def get_best_scalping_stocks(limit: int = 10) -> List[Dict]:
    """Get best stocks for scalping right now.
    
    Combines:
    1. Liquidity/market cap filter (tight spreads)
    2. Current intraday activity scoring
    3. Volatility confirmation
    
    Returns:
        List of stock info dicts with rank, symbol, score, metrics
    """
    # Start with liquid stocks
    liquid_symbols = rank_stocks_by_liquidity(50)
    
    # Score by current intraday activity
    candidates = get_scalping_candidates(
        symbol_filter=liquid_symbols,
        min_score=30.0,
        limit=limit
    )
    
    # Format results
    results = []
    for rank, (symbol, score, metrics) in enumerate(candidates, 1):
        results.append({
            "rank": rank,
            "symbol": symbol,
            "score": round(score, 1),
            "price_current": round(metrics.get("price_current", 0), 0),
            "price_range_pct": round(metrics.get("price_range_pct", 0), 2),
            "volume_spike": round(metrics.get("volume_spike", 0), 2),
            "rsi": round(metrics.get("rsi", 50), 1),
            "momentum_pct": round(metrics.get("momentum_pct", 0), 2),
        })
    
    return results


def display_scalping_stocks():
    """Display best scalping stocks in nice format."""
    stocks = get_best_scalping_stocks(limit=10)
    
    if not stocks:
        print("No scalping candidates found.")
        return
    
    print("\n📊 BEST SCALPING STOCKS (Current Activity)")
    print("=" * 100)
    print(f"{'Rank':<5} {'Symbol':<8} {'Score':<7} {'Price':<12} {'Range':<10} {'Volume':<8} {'RSI':<6} {'Momentum'}")
    print("-" * 100)
    
    for s in stocks:
        momentum_indicator = "📈" if s["momentum_pct"] > 0 else "📉" if s["momentum_pct"] < 0 else "→"
        print(f"{s['rank']:<5} {s['symbol']:<8} {s['score']:<7.1f} ₩{s['price_current']:<11,.0f} {s['price_range_pct']:<9.2f}% {s['volume_spike']:<7.2f}x {s['rsi']:<6.1f} {momentum_indicator} {s['momentum_pct']:+.2f}%")
    
    print("=" * 100)


# Export API
__all__ = [
    "get_best_scalping_stocks",
    "get_scalping_candidates",
    "rank_stocks_by_liquidity",
    "calculate_intraday_metrics",
    "score_for_scalping",
    "display_scalping_stocks",
    "load_intraday_prices"
]
