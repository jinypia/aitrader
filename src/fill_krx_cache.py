from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

from backtest_compare import _cached_market_symbols, _fetch_yahoo_daily_bars_kr, _save_cached_bars
from bot_runtime import _fetch_kind_all_symbols
from config import load_settings


def _fill_one(symbol: str, fetch_limit: int) -> tuple[str, int]:
    bars = _fetch_yahoo_daily_bars_kr(symbol, limit=fetch_limit)
    if len(bars) >= 50:
        _save_cached_bars("KR", symbol, bars)
    return symbol, len(bars)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fill KRX backtest cache up to a target count.")
    parser.add_argument("--target", type=int, default=2000, help="Target number of cached KRX symbols.")
    parser.add_argument("--fetch-limit", type=int, default=260, help="Bars to fetch per symbol.")
    parser.add_argument("--workers", type=int, default=8, help="Parallel fetch workers.")
    args = parser.parse_args()

    settings = load_settings()
    existing = [sym for sym in _cached_market_symbols("KR") if sym.isdigit()]
    existing_set = set(existing)
    all_symbols = [
        sym for sym in _fetch_kind_all_symbols(settings.auto_universe_source_url, existing) if str(sym).strip().isdigit()
    ]
    wanted = max(int(args.target), len(existing))
    candidates = [sym for sym in all_symbols if sym not in existing_set][: max(0, wanted - len(existing))]

    print(f"existing_cache={len(existing)}")
    print(f"candidate_pool={len(all_symbols)}")
    print(f"to_fetch={len(candidates)}")

    fetched_ok = 0
    fetched_short = 0
    if candidates:
        with ThreadPoolExecutor(max_workers=max(1, int(args.workers))) as ex:
            futures = {ex.submit(_fill_one, sym, int(args.fetch_limit)): sym for sym in candidates}
            for fut in as_completed(futures):
                sym, bar_count = fut.result()
                if bar_count >= 50:
                    fetched_ok += 1
                else:
                    fetched_short += 1
                print(f"{sym} bars={bar_count}")

    final_count = len([sym for sym in _cached_market_symbols("KR") if sym.isdigit()])
    print(f"fetched_ok={fetched_ok}")
    print(f"fetched_short={fetched_short}")
    print(f"final_cache={final_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
