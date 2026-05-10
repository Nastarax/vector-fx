"""
EdgeFinder Clone - orchestrator.
Pulls all data, scores it, renders Top Setups heatmap.

Live mode (default):
  python main.py
  -> writes data/output.html with current data

Backtest mode:
  python main.py --date 2025-03-15
  -> writes data/backtest_2025-03-15.html
     uses only data available on or before 2025-03-15
     retail sentiment falls back to neutral (no historical data)
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.fetchers import cot, forexfactory, fred, prices, retail, tradingeconomics
from src.output import build_cot, build_heatmap
from src.scoring.score_pair import build_heatmap as build_matrix, load_pairs_cfg


def parse_args():
    p = argparse.ArgumentParser(description="EdgeFinder Clone heatmap builder.")
    p.add_argument(
        "--date",
        type=str,
        default=None,
        help="Backtest date (YYYY-MM-DD). Builds heatmap as it would have looked on that date.",
    )
    return p.parse_args()


def main():
    args = parse_args()
    load_dotenv()
    t0 = time.time()

    if args.date:
        print(f"=== BACKTEST MODE: as of {args.date} ===")
        print("(Retail sentiment will be neutral - no historical data available)\n")

    print("[1/5] Fetching macro data from FRED...")
    macro = fred.fetch_all_macro(as_of_date=args.date)

    print("[2/5] Fetching COT report...")
    try:
        cot_data = cot.fetch_cot(as_of_date=args.date)
    except Exception as e:
        print(f"  COT failed: {e}; using empty fallback")
        cot_data = {}

    print("[3/5] Fetching prices (Daily + 4H) + retail sentiment...")
    pairs_cfg = load_pairs_cfg()
    pair_symbols = [p["symbol"] for p in pairs_cfg["pairs"]]
    px = prices.fetch_prices(as_of_date=args.date)
    px_4h = prices.fetch_prices_4h(as_of_date=args.date)
    if args.date:
        # No historical retail data, default to neutral
        rt = {sym: retail.RetailReading(sym, 50.0, 50.0) for sym in pair_symbols}
    else:
        rt = retail.fetch_retail(pair_symbols)

    print("[4/5] Fetching ForexFactory + Trading Economics surprise data...")
    if args.date:
        ff_history = {}
        te_history = {}
    else:
        ff_history = forexfactory.fetch_ff()
        te_history = tradingeconomics.load_history()  # use accumulated cache; sweep manually
        print(f"[te] using cached history: {sum(len(v) for v in te_history.values())} releases across {len(te_history)} pairs")

    print("[5/5] Scoring + rendering...")
    heatmap = build_matrix(macro, cot_data, rt, px, prices_4h=px_4h, as_of_date=args.date, ff_history=ff_history, te_history=te_history)
    out_path = build_heatmap.render(heatmap)
    cot_path = build_cot.render(cot_data) if cot_data else None

    print(f"\nDone in {time.time()-t0:.1f}s")
    print(f"  Heatmap   -> {out_path}")
    if cot_path:
        print(f"  COT page  -> {cot_path}")
    print("\nTop 5 bullish:")
    for r in heatmap["rows"][:5]:
        print(f"  {r['symbol']:7s}  {r['bias']:13s}  total={r['total']:+d}")
    print("Bottom 5 bearish:")
    for r in heatmap["rows"][-5:]:
        print(f"  {r['symbol']:7s}  {r['bias']:13s}  total={r['total']:+d}")


if __name__ == "__main__":
    main()
