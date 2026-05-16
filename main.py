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

from src.fetchers import abs_au, cot, forexfactory, fred, investing, investing_cpi, investing_ppi, prices, retail, services_pmi, tradingeconomics
from src.output import build_cot, build_heatmap, build_seasonality
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
        # Backtest mode: use accumulated history filtered to releases on/before that date
        raw_ff = forexfactory.load_history()
        ff_history = {
            k: [r for r in v if r.get("date", "") <= args.date]
            for k, v in raw_ff.items()
        }
        ff_history = {k: v for k, v in ff_history.items() if v}

        raw_te = tradingeconomics.load_history()
        te_history = {
            k: [r for r in v if r.get("date", "") <= args.date]
            for k, v in raw_te.items()
        }
        te_history = {k: v for k, v in te_history.items() if v}

        print(f"[ff] backtest history: {sum(len(v) for v in ff_history.values())} releases across {len(ff_history)} pairs")
        print(f"[te] backtest history: {sum(len(v) for v in te_history.values())} releases across {len(te_history)} pairs")

        # Investing.com mPMI doesn't have backfill history; use cached snapshot
        # only if its date is <= backtest date.
        cached = investing.load_cached()
        investing_mpmi = {
            c: r for c, r in cached.items()
            if r.get("date") and r["date"] <= args.date
        }
        # Same logic for sPMI
        cached_s = services_pmi.load_cached()
        investing_spmi = {
            c: r for c, r in cached_s.items()
            if r.get("date") and r["date"] <= args.date
        }
        # CPI cache: filter by backtest date
        cached_cpi = investing_cpi.load_cached()
        investing_cpi_data = {
            c: r for c, r in cached_cpi.items()
            if r.get("date") and r["date"] <= args.date
        }
        # PPI (NZD) cache: same filter
        cached_ppi = investing_ppi.load_cached()
        investing_ppi_data = {
            c: r for c, r in cached_ppi.items()
            if r.get("date") and r["date"] <= args.date
        }
        # Rates outlook in backtest mode: use whatever's cached. The
        # snapshot is point-in-time; for honest backtests the user should
        # have refreshed near the date in question.
        rates_outlook = tradingeconomics.load_rates_outlook()

        # ABS MHSI cache (AUD retail sales). Only one snapshot is cached, so we
        # can either use it or skip; the date check keeps backtests honest.
        cached_abs = abs_au.load_cached() or {}
        abs_au_mhsi = cached_abs if cached_abs.get("current_month") and cached_abs["current_month"] <= args.date else None
    else:
        ff_history = forexfactory.fetch_ff()
        # Upcoming rate decisions from TE (free TEForecast on per-country
        # interest-rate pages). Refreshed every run.
        rates_outlook = tradingeconomics.fetch_rates_outlook()
        # Always refresh GDP and retail sales from TE so we have the latest
        # Consensus values for both columns. Other TE indicators (PPI, PCE)
        # stay cached until next manual sweep.
        tradingeconomics.fetch_gdp_only()
        tradingeconomics.fetch_retail_sales_only()
        tradingeconomics.fetch_consumer_conf_only()
        tradingeconomics.fetch_ppi_only()
        tradingeconomics.fetch_pce_only()
        tradingeconomics.fetch_nfp_only()
        tradingeconomics.fetch_adp_only()
        tradingeconomics.fetch_unemployment_only()
        tradingeconomics.fetch_jobless_claims_only()
        tradingeconomics.fetch_jolts_only()
        te_history = tradingeconomics.load_history()
        print(f"[te] using cached history: {sum(len(v) for v in te_history.values())} releases across {len(te_history)} pairs")

        # Investing.com mPMI: read from cache only. The cache is refreshed by
        # scripts/refresh_investing.py (run locally). PMI is monthly so hourly
        # scraping is wasteful and Cloudflare blocks GitHub Actions IPs anyway.
        investing_mpmi = investing.load_cached()
        if investing_mpmi:
            print(f"[investing] using cached mPMI: {len(investing_mpmi)} currencies")
        else:
            print("[investing] no mPMI cache - run scripts/refresh_investing.py to populate")

        # Services PMI cache: same pattern as mPMI.
        investing_spmi = services_pmi.load_cached()
        if investing_spmi:
            print(f"[spmi] using cached sPMI: {len(investing_spmi)} currencies")
        else:
            print("[spmi] no sPMI cache - run scripts/refresh_investing.py to populate")

        # CPI YoY cache: same pattern. Refreshed by scripts/refresh_investing.py.
        investing_cpi_data = investing_cpi.load_cached()
        if investing_cpi_data:
            print(f"[cpi] using cached CPI: {len(investing_cpi_data)} currencies")
        else:
            print("[cpi] no CPI cache - run scripts/refresh_investing.py to populate")

        # PPI YoY cache (NZD only via Investing). Other 7 use TE history.
        investing_ppi_data = investing_ppi.load_cached()
        if investing_ppi_data:
            print(f"[ppi] using cached NZD PPI: {len(investing_ppi_data)} currencies")
        else:
            print("[ppi] no NZD PPI cache - run scripts/refresh_investing.py to populate")

        # ABS Monthly Household Spending Indicator (replaces TE retail sales
        # for AUD only). Not Cloudflare-blocked so we can scrape on every run.
        abs_au_mhsi = abs_au.fetch_mhsi()

    print("[5/5] Scoring + rendering...")
    heatmap = build_matrix(macro, cot_data, rt, px, prices_4h=px_4h, as_of_date=args.date, ff_history=ff_history, te_history=te_history, investing_mpmi=investing_mpmi, investing_spmi=investing_spmi, abs_au_mhsi=abs_au_mhsi, investing_cpi=investing_cpi_data, investing_ppi=investing_ppi_data, rates_outlook=rates_outlook)
    out_path = build_heatmap.render(heatmap)

    # COT dashboard: fetch 52w of weekly history (separate from the 4w used
    # by the scoring path) and render both interactive tools.
    cot_path = None
    if cot_data:
        try:
            cot_history = cot.fetch_cot_history(weeks=52, as_of_date=args.date)
        except Exception as e:
            print(f"[cot-history] fetch failed: {e}")
            cot_history = None
        cot_path = build_cot.render(cot_data, cot_history=cot_history)

    # Seasonality pages (yearly + monthly), all pairs, dropdown-driven.
    seasonality_yearly = seasonality_monthly = None
    try:
        seasonality_yearly, seasonality_monthly = build_seasonality.render_all(px, default_pair="AUDUSD")
    except Exception as e:
        print(f"[seasonality] render failed: {e}")

    print(f"\nDone in {time.time()-t0:.1f}s")
    print(f"  Heatmap        -> {out_path}")
    if cot_path:
        print(f"  COT page       -> {cot_path}")
    if seasonality_yearly:
        print(f"  Yearly seas    -> {seasonality_yearly}")
    if seasonality_monthly:
        print(f"  Monthly seas   -> {seasonality_monthly}")
    print("\nTop 5 bullish:")
    for r in heatmap["rows"][:5]:
        print(f"  {r['symbol']:7s}  {r['bias']:13s}  total={r['total']:+d}")
    print("Bottom 5 bearish:")
    for r in heatmap["rows"][-5:]:
        print(f"  {r['symbol']:7s}  {r['bias']:13s}  total={r['total']:+d}")


if __name__ == "__main__":
    main()
