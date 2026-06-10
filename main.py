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

from src.fetchers import abs_au, cot, forexfactory, fred, investing, investing_adp, investing_consumer_conf, investing_core, investing_cpi, investing_jolts, investing_pce, investing_ppi, investing_retail_sales, myfxbook_ppi, prices, retail, services_pmi, tradingeconomics
from src.output import build_cot, build_economic_heatmap, build_heatmap, build_inflation, build_macro, build_scorecard, build_seasonality, notify
from src.scoring.score_pair import build_heatmap as build_matrix, load_pairs_cfg
from src.scoring import score_history


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
    treasury_2y = fred.fetch_treasury_2y(as_of_date=args.date)

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
        # JPY CPI: use cached Tokyo Core snapshot if its date fits the window.
        tokyo_core = investing_cpi.load_tokyo_core_cpi()
        if tokyo_core and tokyo_core.get("date") and tokyo_core["date"] <= args.date:
            investing_cpi_data["JPY"] = tokyo_core
        # PPI (NZD) cache: same filter
        cached_ppi = investing_ppi.load_cached()
        investing_ppi_data = {
            c: r for c, r in cached_ppi.items()
            if r.get("date") and r["date"] <= args.date
        }
        # US Consumer Confidence (Investing CB) cache: same filter
        cached_cc = investing_consumer_conf.load_cached()
        investing_cc_data = {
            c: r for c, r in cached_cc.items()
            if r.get("date") and r["date"] <= args.date
        }
        # US JOLTS (Investing) cache: same filter
        cached_jolts = investing_jolts.load_cached()
        investing_jolts_data = {
            c: r for c, r in cached_jolts.items()
            if r.get("date") and r["date"] <= args.date
        }
        # US ADP (Investing) cache: same filter
        cached_adp = investing_adp.load_cached()
        investing_adp_data = {
            c: r for c, r in cached_adp.items()
            if r.get("date") and r["date"] <= args.date
        }
        # US PCE (Investing Core PCE YoY) cache: same filter
        cached_pce = investing_pce.load_cached()
        investing_pce_data = {
            c: r for c, r in cached_pce.items()
            if r.get("date") and r["date"] <= args.date
        }
        # CHF PPI (Myfxbook) cache: same date filter as the other sources.
        cached_chf_ppi = myfxbook_ppi.load_cached()
        myfxbook_ppi_data = {
            c: r for c, r in cached_chf_ppi.items()
            if r.get("date") and r["date"] <= args.date
        }
        # CAD Retail Sales (Investing) cache: same filter
        cached_retail_inv = investing_retail_sales.load_cached()
        investing_retail_sales_data = {
            c: r for c, r in cached_retail_inv.items()
            if r.get("date") and r["date"] <= args.date
        }
        # US Core CPI + Core PPI cache: same filter
        cached_core = investing_core.load_cached()
        investing_core_data = {
            k: r for k, r in cached_core.items()
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

        # JPY CPI override: use Investing.com Tokyo Core CPI (Actual vs
        # Forecast/consensus) instead of national CPI. One fetch gives the
        # latest release (scoring) plus ~100 months of history (chart).
        tokyo_core = investing_cpi.fetch_tokyo_core_cpi() or investing_cpi.load_tokyo_core_cpi()
        if tokyo_core and tokyo_core.get("actual") is not None:
            investing_cpi_data = dict(investing_cpi_data or {})
            investing_cpi_data["JPY"] = tokyo_core
            print(f"[cpi] JPY overridden with Tokyo Core CPI ({tokyo_core.get('date')})")

        # PPI YoY cache (NZD only via Investing). Other 7 use TE history.
        investing_ppi_data = investing_ppi.load_cached()
        if investing_ppi_data:
            print(f"[ppi] using cached NZD PPI: {len(investing_ppi_data)} currencies")
        else:
            print("[ppi] no NZD PPI cache - run scripts/refresh_investing.py to populate")

        # US Consumer Confidence cache (Investing CB Consumer Confidence, id 48).
        # Other 7 currencies use TE momentum. Refreshed by refresh_investing.py.
        investing_cc_data = investing_consumer_conf.load_cached()
        if investing_cc_data:
            print(f"[cc] using cached US Consumer Confidence: {len(investing_cc_data)} currencies")
        else:
            print("[cc] no US Consumer Confidence cache - run scripts/refresh_investing.py to populate")

        # US JOLTS cache (Investing JOLTS Job Openings, id 1057). USD only;
        # other currencies stay neutral. Refreshed by refresh_investing.py.
        investing_jolts_data = investing_jolts.load_cached()
        if investing_jolts_data:
            print(f"[jolts] using cached US JOLTS: {len(investing_jolts_data)} currencies")
        else:
            print("[jolts] no US JOLTS cache - run scripts/refresh_investing.py to populate")

        # US ADP cache (Investing ADP Nonfarm Employment Change, id 1). USD
        # only; other currencies stay neutral. Refreshed by refresh_investing.py.
        investing_adp_data = investing_adp.load_cached()
        if investing_adp_data:
            print(f"[adp] using cached US ADP: {len(investing_adp_data)} currencies")
        else:
            print("[adp] no US ADP cache - run scripts/refresh_investing.py to populate")

        # US PCE cache (Investing Core PCE Price Index YoY, id 905). USD only;
        # other currencies stay neutral, TE is the fallback. Refreshed by
        # refresh_investing.py.
        investing_pce_data = investing_pce.load_cached()
        if investing_pce_data:
            print(f"[pce] using cached US Core PCE: {len(investing_pce_data)} currencies")
        else:
            print("[pce] no US PCE cache - run scripts/refresh_investing.py pce to populate")

        # Myfxbook PPI cache (CHF + AUD). NZD stays on Investing, the rest on TE.
        # Refreshed by refresh_investing.py.
        myfxbook_ppi_data = myfxbook_ppi.load_cached()
        if myfxbook_ppi_data:
            print(f"[mfx-ppi] using cached Myfxbook PPI: {len(myfxbook_ppi_data)} currencies")
        else:
            print("[mfx-ppi] no Myfxbook PPI cache - run scripts/refresh_investing.py to populate")

        # CAD Retail Sales cache (Investing retail-sales-260). CAD only;
        # AUD uses ABS MHSI, other 6 stay on TE. Refreshed by refresh_investing.py.
        investing_retail_sales_data = investing_retail_sales.load_cached()
        if investing_retail_sales_data:
            print(f"[retail-inv] using cached CAD Retail Sales: {len(investing_retail_sales_data)} currencies")
        else:
            print("[retail-inv] no CAD Retail Sales cache - run scripts/refresh_investing.py to populate")

        # US Core CPI + Core PPI cache (gold inflation scoring).
        investing_core_data = investing_core.load_cached()
        if investing_core_data:
            print(f"[core] using cached Core CPI/PPI: {len(investing_core_data)} indicators")
        else:
            print("[core] no Core CPI/PPI cache - run scripts/refresh_investing.py core to populate")

        # ABS Monthly Household Spending Indicator (replaces TE retail sales
        # for AUD only). Not Cloudflare-blocked so we can scrape on every run.
        abs_au_mhsi = abs_au.fetch_mhsi()

    print("[5/5] Scoring + rendering...")
    heatmap = build_matrix(macro, cot_data, rt, px, prices_4h=px_4h, as_of_date=args.date, ff_history=ff_history, te_history=te_history, investing_mpmi=investing_mpmi, investing_spmi=investing_spmi, abs_au_mhsi=abs_au_mhsi, investing_cpi=investing_cpi_data, investing_ppi=investing_ppi_data, myfxbook_ppi=myfxbook_ppi_data, investing_cc=investing_cc_data, investing_jolts=investing_jolts_data, investing_adp=investing_adp_data, investing_pce=investing_pce_data, investing_retail_sales=investing_retail_sales_data, rates_outlook=rates_outlook, investing_core=investing_core_data, treasury_2y=treasury_2y)
    out_path = build_heatmap.render(heatmap)

    # COT dashboard: fetch 52w of weekly history (separate from the 4w used
    # by the scoring path) and render both interactive tools.
    cot_path = None
    if cot_data:
        try:
            cot_history = cot.fetch_cot_history(weeks=104, as_of_date=args.date)
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

    # Economic Heatmap (per-currency macro release tables)
    econ_path = None
    econ_data = None
    try:
        econ_data = build_economic_heatmap.build_all(
            te_history=te_history,
            investing_cpi=investing_cpi_data,
            investing_ppi=investing_ppi_data,
            investing_mpmi=investing_mpmi,
            investing_spmi=investing_spmi,
            abs_au_mhsi=abs_au_mhsi,
            rates_outlook=rates_outlook,
            investing_cc=investing_cc_data,
            investing_jolts=investing_jolts_data,
            investing_adp=investing_adp_data,
            investing_pce=investing_pce_data,
            myfxbook_ppi=myfxbook_ppi_data,
            investing_retail_sales=investing_retail_sales_data,
            treasury_2y=treasury_2y,
        )
        econ_path = build_economic_heatmap.render(econ_data)
    except Exception as e:
        print(f"[econ-heatmap] render failed: {e}")

    # Pair-level history (score + range location + setup state), recorded
    # independently of the scorecard so a scorecard failure can't drop it.
    try:
        score_history.save_pair_snapshot(heatmap["rows"], date_str=args.date)
    except Exception as e:
        print(f"[score-history] pair snapshot failed: {e}")

    # WATCH-flip alerts (live runs only: a backtest render must not ping
    # or overwrite the live setup-state baseline).
    if not args.date:
        try:
            notify.check_and_notify(heatmap["rows"])
        except Exception as e:
            print(f"[notify] failed: {e}")

    # Asset Scorecard (per-currency deep dive with gauge + sub-scores)
    scorecard_path = None
    try:
        if econ_data is None:
            print("[scorecard] skipping - econ_data unavailable")
        else:
            scorecards = build_scorecard.build_all(
                per_ccy=heatmap["per_ccy"],
                pair_rows=heatmap["rows"],
                cot_data=cot_data,
                econ_data=econ_data,
            )
            score_history.save_snapshot(scorecards, date_str=args.date)
            scorecard_path = build_scorecard.render(scorecards)
    except Exception as e:
        print(f"[scorecard] render failed: {e}")

    # Inflation Data page (CPI + PPI bars/tables + historical CPI line)
    inflation_path = None
    try:
        if econ_data is None:
            print("[inflation] skipping - econ_data unavailable")
        else:
            # Long CPI index history (separate high-limit fetch, own cache) so
            # the line chart reaches back ~20 years instead of the 5-year
            # window the scoring fetch uses.
            try:
                cpi_index_hist = fred.fetch_cpi_history(as_of_date=args.date)
            except Exception as e:
                print(f"[inflation] CPI history fetch failed: {e}; falling back to scoring CPI window")
                cpi_index_hist = {ccy: macro.get(ccy, {}).get("cpi", []) for ccy in macro}
            # Deep CPI YoY history for all 8 from Investing (__NEXT_DATA__),
            # primary chart source. Cloudflare blocks GH Actions, so this is
            # populated by scripts/refresh_investing.py locally and read here
            # from cache; FRED is the fallback for any gaps.
            investing_history = investing_cpi.load_cpi_full_history()
            if investing_history:
                print(f"[inflation] using Investing CPI history: {len(investing_history)} currencies")
            inflation_payload = build_inflation.build_all(
                econ_data=econ_data,
                cpi_index_by_ccy=cpi_index_hist,
                tokyo_core=tokyo_core,
                investing_history=investing_history,
            )
            inflation_path = build_inflation.render(inflation_payload)
    except Exception as e:
        print(f"[inflation] render failed: {e}")

    # Macro Command Center (central-bank calendar + curated reads, with live
    # currency bias + latest-prints sections fed from this run's data).
    macro_path = None
    try:
        macro_path = build_macro.render(currency_rows=heatmap["rows"], econ_data=econ_data)
    except Exception as e:
        print(f"[macro] render failed: {e}")

    print(f"\nDone in {time.time()-t0:.1f}s")
    print(f"  Heatmap        -> {out_path}")
    if cot_path:
        print(f"  COT page       -> {cot_path}")
    if seasonality_yearly:
        print(f"  Yearly seas    -> {seasonality_yearly}")
    if seasonality_monthly:
        print(f"  Monthly seas   -> {seasonality_monthly}")
    if econ_path:
        print(f"  Econ heatmap   -> {econ_path}")
    if scorecard_path:
        print(f"  Asset scorecard-> {scorecard_path}")
    if inflation_path:
        print(f"  Inflation page -> {inflation_path}")
    if macro_path:
        print(f"  Macro center   -> {macro_path}")
    print("\nTop 5 bullish:")
    for r in heatmap["rows"][:5]:
        print(f"  {r['symbol']:7s}  {r['bias']:13s}  total={r['total']:+d}")
    print("Bottom 5 bearish:")
    for r in heatmap["rows"][-5:]:
        print(f"  {r['symbol']:7s}  {r['bias']:13s}  total={r['total']:+d}")


if __name__ == "__main__":
    main()
