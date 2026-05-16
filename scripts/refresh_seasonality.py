"""
Standalone seasonality renderer. Loads cached price data for all pairs (no
yfinance fetch) and rerenders the two seasonality HTML pages (yearly + monthly).
Much faster than the full main.py pipeline when iterating on chart styling.

Usage:
  python scripts/refresh_seasonality.py            # render all 28 pairs (default)
  python scripts/refresh_seasonality.py AUDUSD     # render only specific pairs
  python scripts/refresh_seasonality.py AUDUSD EURUSD GBPUSD
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.output import build_seasonality  # noqa: E402

CACHE_DIR = ROOT / "data" / "cache"


def _load_pair(symbol: str):
    path = CACHE_DIR / f"px_{symbol}.pkl"
    if not path.exists():
        print(f"  {symbol}: no cached price file at {path}")
        return None
    with open(path, "rb") as f:
        return pickle.load(f)


def _all_symbols() -> list[str]:
    with open(ROOT / "config" / "pairs.yaml") as f:
        cfg = yaml.safe_load(f)
    return [p["symbol"] for p in cfg["pairs"]]


def main(argv):
    if len(argv) > 1:
        symbols = [s.upper() for s in argv[1:]]
    else:
        symbols = _all_symbols()

    print(f"[seasonality] loading {len(symbols)} pair(s) from cache...")
    prices = {}
    for sym in symbols:
        df = _load_pair(sym)
        if df is not None and not df.empty:
            prices[sym] = df

    if not prices:
        print("[seasonality] nothing to render. Run main.py once to populate the price cache.")
        return

    yearly, monthly = build_seasonality.render_all(prices, default_pair="AUDUSD")
    print(f"  Yearly  -> {yearly}")
    print(f"  Monthly -> {monthly}")


if __name__ == "__main__":
    main(sys.argv)
