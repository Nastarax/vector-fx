"""
Local cache refresh for Cloudflare-blocked sources (Investing.com + Myfxbook):
mPMI + sPMI + CPI + NZD PPI + CC + JOLTS + ADP + PCE + CHF PPI.

Why this script exists separately from main.py:
  These indicators are monthly. There's no reason to scrape Investing.com
  24 times a day. Doing so just risks getting our IP Cloudflare-flagged.

Strategy: two passes per indicator. First pass tries all currencies. Second
pass retries ONLY the ones that fell back to cache (Cloudflare-blocked or
parse-failed), with longer sleeps to avoid the rate limiter.

Each fetcher tracks which currencies it actually fetched fresh in a
module-level `_LAST_FRESH` set. The script uses that (NOT the result dict,
which mixes fresh + cached entries) to know what truly needs retry.

Run locally on Yanaël's Windows machine, then commit + push the JSON
caches. GitHub Actions can't run this because Cloudflare blocks GitHub IPs.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.fetchers import (investing, investing_adp, investing_consumer_conf,
                          investing_core, investing_cpi, investing_jolts,
                          investing_pce, investing_ppi, investing_retail_sales,
                          myfxbook_ppi, services_pmi)


def _summarize(label: str, all_keys, fresh_set, results):
    """Print accurate per-pass summary."""
    cached = [c for c in all_keys if c not in fresh_set and c in results]
    missing = [c for c in all_keys if c not in results]
    print(f"\n[{label}] pass complete: {len(fresh_set)} fresh, {len(cached)} fell back to cache, {len(missing)} missing entirely")
    if cached:
        print(f"  cache fallback: {cached}")
    if missing:
        print(f"  missing entirely: {missing}")


def refresh_mpmi():
    print("\n============================================")
    print("REFRESHING MANUFACTURING PMI (mPMI)")
    print("============================================")
    all_keys = list(investing.MPMI_URLS.keys())
    print(f"Targeting {len(all_keys)} currencies\n")

    print("--- Pass 1: full fetch ---")
    first = investing.fetch_mpmi(sleep_between=12.0)
    fresh1 = set(investing._LAST_FRESH)
    _summarize("mpmi", all_keys, fresh1, first)

    failed = [c for c in all_keys if c not in fresh1]
    if not failed:
        print("\nmPMI: all currencies fetched fresh.")
        return

    print(f"\n--- Pass 2: retry {failed} after 60s cooldown ---")
    time.sleep(60)
    orig = investing.MPMI_URLS.copy()
    try:
        for k in list(investing.MPMI_URLS.keys()):
            if k not in failed:
                del investing.MPMI_URLS[k]
        second = investing.fetch_mpmi(sleep_between=18.0)
        fresh2 = set(investing._LAST_FRESH)
    finally:
        investing.MPMI_URLS.clear()
        investing.MPMI_URLS.update(orig)

    still_failed = [c for c in failed if c not in fresh2]
    print(f"\nmPMI summary: pass 1 fresh={sorted(fresh1)}, pass 2 fresh={sorted(fresh2)}")
    if still_failed:
        print(f"mPMI: still not fresh after 2 passes: {still_failed}")
        print("Cache retains last successful values for these currencies.")
    else:
        print("mPMI: all previously-failed currencies recovered on pass 2.")


def refresh_spmi():
    print("\n============================================")
    print("REFRESHING SERVICES PMI (sPMI)")
    print("============================================")
    all_keys = (list(services_pmi.SPMI_INVESTING_URLS.keys())
                + list(services_pmi.SPMI_TE_URLS.keys())
                + list(services_pmi.SPMI_BUSINESSNZ_URLS.keys())
                + list(services_pmi.SPMI_MYFXBOOK_URLS.keys()))
    print(f"Targeting {len(all_keys)} currencies "
          f"({len(services_pmi.SPMI_INVESTING_URLS)} Investing + "
          f"{len(services_pmi.SPMI_TE_URLS)} TE + "
          f"{len(services_pmi.SPMI_BUSINESSNZ_URLS)} BusinessNZ + "
          f"{len(services_pmi.SPMI_MYFXBOOK_URLS)} Myfxbook)\n")

    print("--- Pass 1: full fetch ---")
    first = services_pmi.fetch_spmi(sleep_between=12.0)
    fresh1 = set(services_pmi._LAST_FRESH)
    _summarize("spmi", all_keys, fresh1, first)

    failed = [c for c in all_keys if c not in fresh1]
    if not failed:
        print("\nsPMI: all currencies fetched fresh.")
        return

    print(f"\n--- Pass 2: retry {failed} after 60s cooldown ---")
    time.sleep(60)
    orig_investing = services_pmi.SPMI_INVESTING_URLS.copy()
    orig_te = services_pmi.SPMI_TE_URLS.copy()
    orig_bnz = services_pmi.SPMI_BUSINESSNZ_URLS.copy()
    orig_myfx = services_pmi.SPMI_MYFXBOOK_URLS.copy()
    try:
        for k in list(services_pmi.SPMI_INVESTING_URLS.keys()):
            if k not in failed:
                del services_pmi.SPMI_INVESTING_URLS[k]
        for k in list(services_pmi.SPMI_TE_URLS.keys()):
            if k not in failed:
                del services_pmi.SPMI_TE_URLS[k]
        for k in list(services_pmi.SPMI_BUSINESSNZ_URLS.keys()):
            if k not in failed:
                del services_pmi.SPMI_BUSINESSNZ_URLS[k]
        for k in list(services_pmi.SPMI_MYFXBOOK_URLS.keys()):
            if k not in failed:
                del services_pmi.SPMI_MYFXBOOK_URLS[k]
        second = services_pmi.fetch_spmi(sleep_between=18.0)
        fresh2 = set(services_pmi._LAST_FRESH)
    finally:
        services_pmi.SPMI_INVESTING_URLS.clear()
        services_pmi.SPMI_INVESTING_URLS.update(orig_investing)
        services_pmi.SPMI_TE_URLS.clear()
        services_pmi.SPMI_TE_URLS.update(orig_te)
        services_pmi.SPMI_BUSINESSNZ_URLS.clear()
        services_pmi.SPMI_BUSINESSNZ_URLS.update(orig_bnz)
        services_pmi.SPMI_MYFXBOOK_URLS.clear()
        services_pmi.SPMI_MYFXBOOK_URLS.update(orig_myfx)

    still_failed = [c for c in failed if c not in fresh2]
    print(f"\nsPMI summary: pass 1 fresh={sorted(fresh1)}, pass 2 fresh={sorted(fresh2)}")
    if still_failed:
        print(f"sPMI: still not fresh after 2 passes: {still_failed}")
        print("Cache retains last successful values for these currencies.")
    else:
        print("sPMI: all previously-failed currencies recovered on pass 2.")


def refresh_cpi():
    print("\n============================================")
    print("REFRESHING CPI YoY")
    print("============================================")
    all_keys = list(investing_cpi.CPI_URLS.keys())
    print(f"Targeting {len(all_keys)} currencies\n")

    print("--- Pass 1: full fetch ---")
    first = investing_cpi.fetch_cpi(sleep_between=12.0)
    fresh1 = set(investing_cpi._LAST_FRESH)
    _summarize("cpi", all_keys, fresh1, first)

    failed = [c for c in all_keys if c not in fresh1]
    if not failed:
        print("\nCPI: all currencies fetched fresh.")
        return

    print(f"\n--- Pass 2: retry {failed} after 60s cooldown ---")
    time.sleep(60)
    orig = investing_cpi.CPI_URLS.copy()
    try:
        for k in list(investing_cpi.CPI_URLS.keys()):
            if k not in failed:
                del investing_cpi.CPI_URLS[k]
        second = investing_cpi.fetch_cpi(sleep_between=18.0)
        fresh2 = set(investing_cpi._LAST_FRESH)
    finally:
        investing_cpi.CPI_URLS.clear()
        investing_cpi.CPI_URLS.update(orig)

    still_failed = [c for c in failed if c not in fresh2]
    print(f"\nCPI summary: pass 1 fresh={sorted(fresh1)}, pass 2 fresh={sorted(fresh2)}")
    if still_failed:
        print(f"CPI: still not fresh after 2 passes: {still_failed}")
        print("Cache retains last successful values for these currencies.")
    else:
        print("CPI: all previously-failed currencies recovered on pass 2.")


def refresh_ppi():
    print("\n============================================")
    print("REFRESHING PPI YoY (NZD + GBP via Investing)")
    print("============================================")
    all_keys = list(investing_ppi.PPI_URLS.keys())
    print(f"Targeting {len(all_keys)} currencies\n")

    print("--- Pass 1: full fetch ---")
    first = investing_ppi.fetch_ppi(sleep_between=12.0)
    fresh1 = set(investing_ppi._LAST_FRESH)
    _summarize("ppi", all_keys, fresh1, first)

    failed = [c for c in all_keys if c not in fresh1]
    if not failed:
        print("\nPPI (NZD): fetched fresh.")
        return

    print(f"\n--- Pass 2: retry {failed} after 60s cooldown ---")
    time.sleep(60)
    orig = investing_ppi.PPI_URLS.copy()
    try:
        for k in list(investing_ppi.PPI_URLS.keys()):
            if k not in failed:
                del investing_ppi.PPI_URLS[k]
        second = investing_ppi.fetch_ppi(sleep_between=18.0)
        fresh2 = set(investing_ppi._LAST_FRESH)
    finally:
        investing_ppi.PPI_URLS.clear()
        investing_ppi.PPI_URLS.update(orig)

    still_failed = [c for c in failed if c not in fresh2]
    print(f"\nPPI summary: pass 1 fresh={sorted(fresh1)}, pass 2 fresh={sorted(fresh2)}")
    if still_failed:
        print(f"PPI: still not fresh after 2 passes: {still_failed}")
        print("Cache retains last successful values.")
    else:
        print("PPI: recovered on pass 2.")


def refresh_consumer_conf():
    print("\n============================================")
    print("REFRESHING US CONSUMER CONFIDENCE (CB, USD only via Investing)")
    print("============================================")
    all_keys = list(investing_consumer_conf.CC_URLS.keys())
    print(f"Targeting {len(all_keys)} currencies\n")

    print("--- Pass 1: full fetch ---")
    first = investing_consumer_conf.fetch_consumer_conf(sleep_between=12.0)
    fresh1 = set(investing_consumer_conf._LAST_FRESH)
    _summarize("cc", all_keys, fresh1, first)

    failed = [c for c in all_keys if c not in fresh1]
    if not failed:
        print("\nConsumer Confidence (USD): fetched fresh.")
        return

    print(f"\n--- Pass 2: retry {failed} after 60s cooldown ---")
    time.sleep(60)
    orig = investing_consumer_conf.CC_URLS.copy()
    try:
        for k in list(investing_consumer_conf.CC_URLS.keys()):
            if k not in failed:
                del investing_consumer_conf.CC_URLS[k]
        second = investing_consumer_conf.fetch_consumer_conf(sleep_between=18.0)
        fresh2 = set(investing_consumer_conf._LAST_FRESH)
    finally:
        investing_consumer_conf.CC_URLS.clear()
        investing_consumer_conf.CC_URLS.update(orig)

    still_failed = [c for c in failed if c not in fresh2]
    print(f"\nConsumer Confidence summary: pass 1 fresh={sorted(fresh1)}, pass 2 fresh={sorted(fresh2)}")
    if still_failed:
        print(f"Consumer Confidence: still not fresh after 2 passes: {still_failed}")
        print("Cache retains last successful values.")
    else:
        print("Consumer Confidence: recovered on pass 2.")


def refresh_jolts():
    print("\n============================================")
    print("REFRESHING US JOLTS JOB OPENINGS (USD only via Investing)")
    print("============================================")
    all_keys = list(investing_jolts.JOLTS_URLS.keys())
    print(f"Targeting {len(all_keys)} currencies\n")

    print("--- Pass 1: full fetch ---")
    first = investing_jolts.fetch_jolts(sleep_between=12.0)
    fresh1 = set(investing_jolts._LAST_FRESH)
    _summarize("jolts", all_keys, fresh1, first)

    failed = [c for c in all_keys if c not in fresh1]
    if not failed:
        print("\nJOLTS (USD): fetched fresh.")
        return

    print(f"\n--- Pass 2: retry {failed} after 60s cooldown ---")
    time.sleep(60)
    orig = investing_jolts.JOLTS_URLS.copy()
    try:
        for k in list(investing_jolts.JOLTS_URLS.keys()):
            if k not in failed:
                del investing_jolts.JOLTS_URLS[k]
        second = investing_jolts.fetch_jolts(sleep_between=18.0)
        fresh2 = set(investing_jolts._LAST_FRESH)
    finally:
        investing_jolts.JOLTS_URLS.clear()
        investing_jolts.JOLTS_URLS.update(orig)

    still_failed = [c for c in failed if c not in fresh2]
    print(f"\nJOLTS summary: pass 1 fresh={sorted(fresh1)}, pass 2 fresh={sorted(fresh2)}")
    if still_failed:
        print(f"JOLTS: still not fresh after 2 passes: {still_failed}")
        print("Cache retains last successful values.")
    else:
        print("JOLTS: recovered on pass 2.")


def refresh_adp():
    print("\n============================================")
    print("REFRESHING US ADP EMPLOYMENT CHANGE (USD only via Investing)")
    print("============================================")
    all_keys = list(investing_adp.ADP_URLS.keys())
    print(f"Targeting {len(all_keys)} currencies\n")

    print("--- Pass 1: full fetch ---")
    first = investing_adp.fetch_adp(sleep_between=12.0)
    fresh1 = set(investing_adp._LAST_FRESH)
    _summarize("adp", all_keys, fresh1, first)

    failed = [c for c in all_keys if c not in fresh1]
    if not failed:
        print("\nADP (USD): fetched fresh.")
        return

    print(f"\n--- Pass 2: retry {failed} after 60s cooldown ---")
    time.sleep(60)
    orig = investing_adp.ADP_URLS.copy()
    try:
        for k in list(investing_adp.ADP_URLS.keys()):
            if k not in failed:
                del investing_adp.ADP_URLS[k]
        second = investing_adp.fetch_adp(sleep_between=18.0)
        fresh2 = set(investing_adp._LAST_FRESH)
    finally:
        investing_adp.ADP_URLS.clear()
        investing_adp.ADP_URLS.update(orig)

    still_failed = [c for c in failed if c not in fresh2]
    print(f"\nADP summary: pass 1 fresh={sorted(fresh1)}, pass 2 fresh={sorted(fresh2)}")
    if still_failed:
        print(f"ADP: still not fresh after 2 passes: {still_failed}")
        print("Cache retains last successful values.")
    else:
        print("ADP: recovered on pass 2.")


def refresh_pce():
    print("\n============================================")
    print("REFRESHING US CORE PCE PRICE INDEX YoY (USD only via Investing)")
    print("============================================")
    all_keys = list(investing_pce.PCE_URLS.keys())
    print(f"Targeting {len(all_keys)} currencies\n")

    print("--- Pass 1: full fetch ---")
    first = investing_pce.fetch_pce(sleep_between=12.0)
    fresh1 = set(investing_pce._LAST_FRESH)
    _summarize("pce", all_keys, fresh1, first)

    failed = [c for c in all_keys if c not in fresh1]
    if not failed:
        print("\nCore PCE (USD): fetched fresh.")
        return

    print(f"\n--- Pass 2: retry {failed} after 60s cooldown ---")
    time.sleep(60)
    orig = investing_pce.PCE_URLS.copy()
    try:
        for k in list(investing_pce.PCE_URLS.keys()):
            if k not in failed:
                del investing_pce.PCE_URLS[k]
        second = investing_pce.fetch_pce(sleep_between=18.0)
        fresh2 = set(investing_pce._LAST_FRESH)
    finally:
        investing_pce.PCE_URLS.clear()
        investing_pce.PCE_URLS.update(orig)

    still_failed = [c for c in failed if c not in fresh2]
    print(f"\nCore PCE summary: pass 1 fresh={sorted(fresh1)}, pass 2 fresh={sorted(fresh2)}")
    if still_failed:
        print(f"Core PCE: still not fresh after 2 passes: {still_failed}")
        print("Cache retains last successful values.")
    else:
        print("Core PCE: recovered on pass 2.")


def refresh_mfx_ppi():
    print("\n============================================")
    print("REFRESHING Myfxbook PPI YoY (CHF + AUD)")
    print("============================================")
    all_keys = list(myfxbook_ppi.CHF_PPI_URLS.keys())
    print(f"Targeting {len(all_keys)} currencies\n")

    print("--- Pass 1: full fetch ---")
    first = myfxbook_ppi.fetch_ppi(sleep_between=8.0)
    fresh1 = set(myfxbook_ppi._LAST_FRESH)
    _summarize("mfx-ppi", all_keys, fresh1, first)

    failed = [c for c in all_keys if c not in fresh1]
    if not failed:
        print("\nMyfxbook PPI: fetched fresh.")
        return

    print(f"\n--- Pass 2: retry {failed} after 60s cooldown ---")
    time.sleep(60)
    second = myfxbook_ppi.fetch_ppi(sleep_between=15.0)
    fresh2 = set(myfxbook_ppi._LAST_FRESH)
    still_failed = [c for c in failed if c not in fresh2]
    print(f"\nMyfxbook PPI summary: pass 1 fresh={sorted(fresh1)}, pass 2 fresh={sorted(fresh2)}")
    if still_failed:
        print(f"Myfxbook PPI: still not fresh after 2 passes: {still_failed}")
        print("Cache retains last successful values. (Myfxbook needs curl_cffi; "
              "make sure it is installed locally.)")
    else:
        print("Myfxbook PPI: recovered on pass 2.")


def refresh_cad_retail():
    print("\n============================================")
    print("REFRESHING CAD Retail Sales MoM (via Investing.com)")
    print("============================================")
    all_keys = list(investing_retail_sales.RETAIL_SALES_URLS.keys())
    print(f"Targeting {len(all_keys)} currencies\n")

    print("--- Pass 1: full fetch ---")
    first = investing_retail_sales.fetch_retail_sales(sleep_between=8.0)
    fresh1 = set(investing_retail_sales._LAST_FRESH)
    _summarize("retail-inv", all_keys, fresh1, first)

    failed = [c for c in all_keys if c not in fresh1]
    if not failed:
        print("\nCAD Retail Sales: fetched fresh.")
        return

    print(f"\n--- Pass 2: retry {failed} after 60s cooldown ---")
    time.sleep(60)
    second = investing_retail_sales.fetch_retail_sales(sleep_between=15.0)
    fresh2 = set(investing_retail_sales._LAST_FRESH)
    still_failed = [c for c in failed if c not in fresh2]
    print(f"\nCAD Retail Sales summary: pass 1 fresh={sorted(fresh1)}, pass 2 fresh={sorted(fresh2)}")
    if still_failed:
        print(f"CAD Retail Sales: still not fresh after 2 passes: {still_failed}")
        print("Cache retains last successful values.")
    else:
        print("CAD Retail Sales: recovered on pass 2.")


def refresh_core():
    print("\n============================================")
    print("REFRESHING US CORE CPI + CORE PPI")
    print("============================================")
    all_keys = list(investing_core.CORE_URLS.keys())
    print(f"Targeting {len(all_keys)} indicators\n")

    print("--- Pass 1: full fetch ---")
    first = investing_core.fetch_core(sleep_between=12.0)
    fresh1 = set(investing_core._LAST_FRESH)
    _summarize("core", all_keys, fresh1, first)

    failed = [c for c in all_keys if c not in fresh1]
    if not failed:
        print("\nCore: all indicators fetched fresh.")
        return

    print(f"\n--- Pass 2: retry {failed} after 60s cooldown ---")
    time.sleep(60)
    orig = investing_core.CORE_URLS.copy()
    try:
        for k in list(investing_core.CORE_URLS.keys()):
            if k not in failed:
                del investing_core.CORE_URLS[k]
        second = investing_core.fetch_core(sleep_between=18.0)
        fresh2 = set(investing_core._LAST_FRESH)
    finally:
        investing_core.CORE_URLS.clear()
        investing_core.CORE_URLS.update(orig)

    still_failed = [c for c in failed if c not in fresh2]
    if still_failed:
        print(f"Core: still not fresh after 2 passes: {still_failed}")
    else:
        print("Core: all previously-failed indicators recovered on pass 2.")


def refresh_cpi_history():
    """Deep monthly CPI YoY history for all 8 currencies (Investing
    __NEXT_DATA__). Powers the inflation line chart with continuous, current
    data so there's no FRED publication lag and no straight-line tails."""
    print("\n============================================")
    print("REFRESHING CPI HISTORY (all 8, for inflation chart)")
    print("============================================")
    hist = investing_cpi.fetch_all_cpi_history(sleep_between=5.0)
    got = sorted(hist.keys())
    print(f"\nCPI history: fetched {len(got)}/8 -> {got}")
    missing = [c for c in ("USD","EUR","GBP","JPY","CHF","AUD","CAD","NZD") if c not in hist]
    if missing:
        print(f"  missing (kept from cache/archive): {missing}")


# Registry of refreshable targets, in default run order. Lets you refresh a
# subset from the CLI instead of the full (slow) sweep, e.g.:
#   python scripts/refresh_investing.py jolts adp
#   python scripts/refresh_investing.py cc
# With no args, runs everything in this order.
REFRESHERS = {
    "mpmi": refresh_mpmi,
    "spmi": refresh_spmi,
    "cpi": refresh_cpi,
    "cpi_history": refresh_cpi_history,
    "ppi": refresh_ppi,
    "cc": refresh_consumer_conf,
    "jolts": refresh_jolts,
    "adp": refresh_adp,
    "pce": refresh_pce,
    "mfx_ppi": refresh_mfx_ppi,
    "cad_retail": refresh_cad_retail,
    "core": refresh_core,
}

# Cache file each target writes (for the commit hint).
_CACHE_FILES = {
    "mpmi": "data/cache/investing_pmi.json",
    "spmi": "data/cache/spmi.json",
    "cpi": "data/cache/investing_cpi.json",
    "cpi_history": "data/cache/cpi_investing_history.json",
    "ppi": "data/cache/investing_ppi.json",
    "cc": "data/cache/investing_consumer_conf.json",
    "jolts": "data/cache/investing_jolts.json",
    "adp": "data/cache/investing_adp.json",
    "pce": "data/cache/investing_pce.json",
    "mfx_ppi": "data/cache/myfxbook_ppi.json",
    "cad_retail": "data/cache/investing_retail_sales.json",
    "core": "data/cache/investing_core.json",
}
# JPY CPI snapshot rides along with the CPI refresh.
_EXTRA_CACHE_FILES = {"cpi": "data/cache/tokyo_core_cpi.json"}


def main():
    args = [a.lower().lstrip("-") for a in sys.argv[1:]]
    if args:
        unknown = [a for a in args if a not in REFRESHERS]
        if unknown:
            print(f"Unknown refresh target(s): {unknown}")
            print(f"Available: {', '.join(REFRESHERS)}")
            return
        order = args
    else:
        order = list(REFRESHERS)

    print(f"=== Investing.com cache refresh: {', '.join(order)} ===")
    for name in order:
        REFRESHERS[name]()

    files = []
    for name in order:
        files.append(_CACHE_FILES[name])
        if name in _EXTRA_CACHE_FILES:
            files.append(_EXTRA_CACHE_FILES[name])
    print("\nDone. Now commit + push:")
    print("  git add " + " ".join(files))
    print("  git commit -m 'Refresh Investing cache'")
    print("  git push")


if __name__ == "__main__":
    main()
