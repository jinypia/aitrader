#!/usr/bin/env python3
"""
Scalping Data Loader - Fetch intraday bar data for scalping simulation.

This module provides functions to:
1. Load stored intraday prices from selected_intraday_prices.json
2. Generate synthetic intraday bars from daily data
3. Fetch real market data if available
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Optional, Tuple
import random


@dataclass
class IntradayBar:
    """Represents a single intraday bar for scalping."""
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    symbol: str = ""
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "timestamp": self.timestamp,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "symbol": self.symbol
        }


def _safe_mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except Exception:
        return -1


@lru_cache(maxsize=16)
def _load_json_cached(path_str: str, mtime_ns: int) -> dict:
    if mtime_ns < 0:
        return {}
    path = Path(path_str)
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


@lru_cache(maxsize=8)
def _intraday_rows_index(path_str: str, mtime_ns: int) -> dict[tuple[str, str], list[dict]]:
    data = _load_json_cached(path_str, mtime_ns)
    rows = data.get("rows", []) if isinstance(data, dict) else []
    out: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol", "")).strip()
        ts = str(row.get("bar_ts", "")).strip()
        if not symbol or len(ts) < 10:
            continue
        date_key = ts[:10]
        bar = {
            "timestamp": ts,
            "open": row.get("price", 0),
            "high": row.get("price", 0),
            "low": row.get("price", 0),
            "close": row.get("price", 0),
            "volume": int(row.get("volume", 1000000)),
            "symbol": symbol,
        }
        out.setdefault((symbol, date_key), []).append(bar)
    return out


@lru_cache(maxsize=8)
def _replay_rows_index(path_str: str, mtime_ns: int) -> dict[tuple[str, str], list[dict]]:
    data = _load_json_cached(path_str, mtime_ns)
    rows = data.get("bars", []) or data.get("rows", []) if isinstance(data, dict) else []
    out: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol", "")).strip()
        ts = str(row.get("timestamp", row.get("bar_ts", ""))).strip()
        if not symbol or len(ts) < 10:
            continue
        date_key = ts[:10]
        out.setdefault((symbol, date_key), []).append(row)
    return out


def _normalize_date(raw_ts: str, raw_date: str) -> str:
    bar_date = ""
    if raw_ts:
        parts = raw_ts.replace("T", " ").split()
        if parts:
            bar_date = parts[0]
    elif raw_date:
        bar_date = raw_date
    if len(bar_date) == 8 and bar_date.isdigit():
        return f"{bar_date[:4]}-{bar_date[4:6]}-{bar_date[6:8]}"
    return bar_date


@lru_cache(maxsize=512)
def _daily_bar_for_date(path_str: str, mtime_ns: int, date_str: str) -> dict:
    data = _load_json_cached(path_str, mtime_ns)
    bars = data.get("bars", []) or data.get("rows", []) if isinstance(data, dict) else []
    for daily_bar in bars:
        if not isinstance(daily_bar, dict):
            continue
        raw_ts = str(daily_bar.get("timestamp", "")).strip()
        raw_date = str(
            daily_bar.get("date")
            or daily_bar.get("trd_dd")
            or daily_bar.get("dt")
            or ""
        ).strip()
        if _normalize_date(raw_ts, raw_date) == date_str:
            return daily_bar
    return {}


@lru_cache(maxsize=1024)
def _synthetic_intraday_cached(
    symbol: str,
    date_str: str,
    bar_interval: int,
    daily_open: float,
    daily_high: float,
    daily_low: float,
    daily_close: float,
    daily_volume: int,
) -> tuple[dict, ...]:
    bars = generate_intraday_bars_from_daily(
        daily_open=daily_open,
        daily_high=daily_high,
        daily_low=daily_low,
        daily_close=daily_close,
        daily_volume=daily_volume,
        date_str=date_str,
        symbol=symbol,
        bar_interval=max(1, int(bar_interval)),
    )
    return tuple(bars)


def load_intraday_data_from_json(file_path: Path) -> list[dict]:
    """Load intraday data from JSON file (selected_intraday_prices.json format).
    
    Args:
        file_path: Path to JSON file with intraday prices
        
    Returns:
        List of bar dictionaries with OHLCV data
    """
    if not file_path.exists():
        return []

    idx = _intraday_rows_index(str(file_path), _safe_mtime_ns(file_path))
    bars: list[dict] = []
    for key in idx:
        bars.extend(idx[key])
    return bars


def get_market_hours_bars(date_str: str, bar_interval: int = 10, bar_count: int = None) -> list[str]:
    """Generate market hours time slots for bars.
    
    Args:
        date_str: Date string (YYYY-MM-DD)
        bar_interval: Bar size in minutes (2, 5, 10, etc.)
        bar_count: Maximum bars to generate (optional)
        
    Returns:
        List of timestamps during market hours
    """
    # Korean market hours: 09:00 - 15:30 (390 minutes total)
    market_open = datetime.strptime(f"{date_str} 09:00", "%Y-%m-%d %H:%M")
    market_close = datetime.strptime(f"{date_str} 15:30", "%Y-%m-%d %H:%M")
    
    bars = []
    current = market_open
    
    max_bars = bar_count or (390 // bar_interval)  # Default: fill market hours
    
    while current <= market_close and len(bars) < max_bars:
        bars.append(current.strftime("%Y-%m-%d %H:%M"))
        current += timedelta(minutes=bar_interval)
    
    return bars


def generate_intraday_bars_from_daily(
    daily_open: float,
    daily_high: float,
    daily_low: float,
    daily_close: float,
    daily_volume: int,
    date_str: str,
    symbol: str = "",
    bar_interval: int = 2,
    volatility: float = 0.001
) -> list[dict]:
    """Generate synthetic intraday bars from a single-day OHLCV data.
    
    This simulates realistic intraday price movement within the day's OHLC bounds.
    
    Args:
        daily_open: Day's opening price
        daily_high: Day's high price
        daily_low: Day's low price
        daily_close: Day's closing price
        daily_volume: Total day volume
        date_str: Date (YYYY-MM-DD)
        symbol: Stock symbol
        bar_interval: Bar size in minutes (2, 5, 10, etc.). Default 2 for scalping.
        volatility: Price volatility factor (0.0005 to 0.002)
        
    Returns:
        List of 2/5/10-minute bars
    """
    market_hours = get_market_hours_bars(date_str, bar_interval)
    bars = []
    
    # Distribute volume across bars (with more volume in morning and afternoon)
    bar_count = len(market_hours)
    volume_per_bar = daily_volume // bar_count if bar_count > 0 else 1
    
    # Generate realistic price path
    price_range = daily_high - daily_low
    current_price = daily_open
    
    for i, timestamp in enumerate(market_hours):
        # Random intraday movement within bounds
        random_move = (random.random() - 0.5) * price_range * volatility * bar_interval
        bar_open = current_price
        
        # Slightly bias toward daily close over time
        time_progress = i / max(1, bar_count - 1)
        drift = (daily_close - daily_open) * time_progress * 0.02
        
        bar_close = max(daily_low, min(daily_high, bar_open + random_move + drift))
        bar_high = max(bar_open, bar_close) + abs(random.random() * price_range * 0.001)
        bar_low = min(bar_open, bar_close) - abs(random.random() * price_range * 0.001)
        
        # Ensure bounds
        bar_high = min(bar_high, daily_high + price_range * 0.01)
        bar_low = max(bar_low, daily_low - price_range * 0.01)
        
        # Volume with morning/afternoon spike
        hour = int(timestamp.split()[1].split(":")[0])
        if hour >= 14:  # Afternoon spike
            bar_volume = int(volume_per_bar * (0.8 + random.random()))
        elif hour >= 9:  # Morning spike
            bar_volume = int(volume_per_bar * (0.7 + random.random()))
        else:
            bar_volume = int(volume_per_bar * (0.2 + random.random()))
        
        bar = {
            "timestamp": timestamp,
            "open": round(bar_open, 2),
            "high": round(bar_high, 2),
            "low": round(bar_low, 2),
            "close": round(bar_close, 2),
            "volume": max(10000, bar_volume),
            "symbol": symbol
        }
        bars.append(bar)
        current_price = bar_close
    
    return bars


def get_day_price_data(
    symbol: str,
    date_str: str,
    source: str = "auto",
    bar_interval: int = 2,
) -> Tuple[list[dict], str]:
    """Get intraday bar data for a specific day and symbol.
    
    Args:
        symbol: Stock symbol (e.g., "005930")
        date_str: Target date (YYYY-MM-DD format)
        source: Data source - "auto" (try all), "json" (stored), "generate" (synthetic)
        
    Returns:
        Tuple of (bars list, data source description)
    """
    
    # Try 1: Load from stored intraday data
    if source in ["auto", "json"]:
        intraday_path = Path("data/selected_intraday_prices.json")
        if intraday_path.exists():
            idx = _intraday_rows_index(str(intraday_path), _safe_mtime_ns(intraday_path))
            filtered_bars = idx.get((symbol, date_str), [])
            if filtered_bars:
                return filtered_bars, f"data/selected_intraday_prices.json ({len(filtered_bars)} bars)"
    
    # Try 2: Load from intraday replay report
    if source in ["auto", "json"]:
        replay_path = Path("data/intraday_selected_replay.json")
        if replay_path.exists():
            idx = _replay_rows_index(str(replay_path), _safe_mtime_ns(replay_path))
            filtered_bars = idx.get((symbol, date_str), [])
            if filtered_bars:
                return filtered_bars, f"data/intraday_selected_replay.json ({len(filtered_bars)} bars)"
    
    # Try 3: Generate from daily data if available
    if source in ["auto", "generate"]:
        daily_cache_path = Path(f"data/backtest_cache/kr_{symbol}_daily.json")
        if daily_cache_path.exists():
            try:
                daily_bar = _daily_bar_for_date(
                    str(daily_cache_path),
                    _safe_mtime_ns(daily_cache_path),
                    date_str,
                )
                if daily_bar:
                    interval = max(1, int(bar_interval))
                    intraday_bars = list(
                        _synthetic_intraday_cached(
                            symbol=symbol,
                            date_str=date_str,
                            bar_interval=interval,
                            daily_open=float(daily_bar.get("open", 0) or 0),
                            daily_high=float(daily_bar.get("high", 0) or 0),
                            daily_low=float(daily_bar.get("low", 0) or 0),
                            daily_close=float(daily_bar.get("close", 0) or 0),
                            daily_volume=int(float(daily_bar.get("volume", 0) or 0)),
                        )
                    )
                    return intraday_bars, (
                        f"Generated from daily ({len(intraday_bars)} synthetic bars, "
                        f"{interval}-min)"
                    )
            except Exception as e:
                print(f"Error generating from daily data: {e}")
    
    return [], f"No data found for {symbol} on {date_str}"


def get_today_scalping_data(symbol: str = "005930", bar_interval: int = 2) -> Tuple[list[dict], str]:
    """Get intraday data for today (or latest available day).
    
    Args:
        symbol: Stock symbol
        
    Returns:
        Tuple of (bars list, data source description)
    """
    # Try today first
    today = datetime.now().strftime("%Y-%m-%d")
    bars, source = get_day_price_data(symbol, today, bar_interval=bar_interval)
    if bars:
        return bars, source
    
    # Try yesterday
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    bars, source = get_day_price_data(symbol, yesterday, bar_interval=bar_interval)
    if bars:
        return bars, source
    
    # Try last trading day (skip weekends)
    for days_back in range(2, 7):
        date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        bars, source = get_day_price_data(symbol, date, bar_interval=bar_interval)
        if bars:
            return bars, source
    
    return [], "No recent data available"


def get_day_data_preview(symbol: str, date_str: str, bar_interval: int = 2) -> dict:
    """Get preview information about available day data.
    
    Args:
        symbol: Stock symbol
        date_str: Target date
        
    Returns:
        Dictionary with data preview info
    """
    bars, source = get_day_price_data(symbol, date_str, bar_interval=bar_interval)
    
    if not bars:
        return {
            "symbol": symbol,
            "date": date_str,
            "available": False,
            "message": "No data found"
        }
    
    first_bar = bars[0]
    last_bar = bars[-1]
    
    # Calculate stats
    closes = [b.get("close", 0) for b in bars]
    volumes = [b.get("volume", 0) for b in bars]
    
    return {
        "symbol": symbol,
        "date": date_str,
        "available": True,
        "source": source,
        "bar_count": len(bars),
        "start_time": first_bar.get("timestamp"),
        "end_time": last_bar.get("timestamp"),
        "first_price": first_bar.get("open"),
        "last_price": last_bar.get("close"),
        "day_high": max(b.get("high", 0) for b in bars),
        "day_low": min(b.get("low", 0) for b in bars),
        "day_low": min(b.get("low", 0) for b in bars),
        "day_volume": sum(volumes),
        "price_range": max(closes) - min(closes),
        "price_range_pct": ((max(closes) - min(closes)) / min(closes) * 100) if min(closes) > 0 else 0
    }


# Export API
__all__ = [
    "get_day_price_data",
    "get_today_scalping_data",
    "get_day_data_preview",
    "generate_intraday_bars_from_daily",
    "load_intraday_data_from_json",
    "get_market_hours_bars",
    "IntradayBar"
]
