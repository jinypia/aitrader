#!/usr/bin/env python3
"""
Quick test of scalping data loading functionality.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from scalping_data_loader import (
    get_day_data_preview,
    get_day_price_data,
    get_today_scalping_data
)

print("=" * 80)
print("SCALPING DATA LOADER - QUICK TEST")
print("=" * 80)

# Test 1: Check preview for a known date
print("\n1️⃣  Testing get_day_data_preview()...")
preview = get_day_data_preview("005930", "2026-03-30")
if preview.get("available"):
    print(f"   ✅ Found: {preview['bar_count']} bars")
    print(f"   📊 Source: {preview['source']}")
    print(f"   📈 Price range: ₩{preview['day_low']:,.0f} - ₩{preview['day_high']:,.0f}")
else:
    print(f"   ℹ️  No data for 2026-03-30, trying to generate...")

# Test 2: Load actual bars
print("\n2️⃣  Testing get_day_price_data()...")
bars, source = get_day_price_data("005930", "2026-03-30")
if bars:
    print(f"   ✅ Loaded {len(bars)} bars from:")
    print(f"   📁 {source}")
    if len(bars) > 0:
        first_bar = bars[0]
        print(f"   📈 First bar (timestamp={first_bar.get('timestamp')}):")
        print(f"      Open: ₩{first_bar.get('open', 0):,.0f}")
        print(f"      Close: ₩{first_bar.get('close', 0):,.0f}")
        print(f"      Volume: {first_bar.get('volume', 0):,}")
else:
    print(f"   ⚠️  No data found for 2026-03-30")

# Test 3: Get today or latest data
print("\n3️⃣  Testing get_today_scalping_data()...")
bars, source = get_today_scalping_data("005930")
if bars:
    print(f"   ✅ Found recent data: {len(bars)} bars")
    print(f"   📁 Source: {source}")
else:
    print(f"   ℹ️  No recent data available")

print("\n" + "=" * 80)
print("✅ All tests completed!")
print("=" * 80)
