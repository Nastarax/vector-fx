"""
EdgeFinder Clone — orchestrator.
Pulls all data, scores it, renders Top Setups heatmap.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# Make `src.*` imports work regardless of cwd
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.fetchers import cot, fred, prices, retail
from src.output import build_heatmap
from src.scoring.score_pair import build_heatmap as build_matrix, load_pairs_cfg


def main():
    load_dotenv()
    t0 = time.time()

    print("[1/4] Fetching macro data from FRED...")
    macro = fred.fetch_all_macro()

    print("[2/4] Fetching COT report...")
    try:
        cot_data = cot.fetch_cot()
    except Exception as e:
        print(f"  COT failed: {e}; using empty fallback")
        cot_data = {}

    print("[3/4] Fetching prices (Daily + 4H) + retail sentiment...")
    pairs_cfg = load_pairs_cfg()
    pair_symbols = [p["symbol"] for p in pairs_cfg["pairs"]]
    px = prices.fetch_prices()
    px_4h = prices.fetch_prices_4h()
    rt = retail.fetch_retail(pair_symbols)

    print("[4/4] Scoring + rendering...")
    heatmap = build_matrix(macro, cot_data, rt, px, prices_4h=px_4h)
    out_path = build_heatmap.render(heatmap)

    print(f"\nDone in {time.time()-t0:.1f}s -> {out_path}")
    print("\nTop 5 bullish:")
    for r in heatmap["rows"][:5]:
        print(f"  {r['symbol']:7s}  {r['bias']:13s}  total={r['total']:+d}")
    print("Bottom 5 bearish:")
    for r in heatmap["rows"][-5:]:
        print(f"  {r['symbol']:7s}  {r['bias']:13s}  total={r['total']:+d}")


if __name__ == "__main__":
    main()
