"""
Local Investing.com cache refresh (mPMI + sPMI + CPI + NZD PPI).

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

from src.fetchers import investing, investing_cpi, investing_ppi, services_pmi


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
                + list(services_pmi.SPMI_MYFXBOOK_URLS.keys()))
    print(f"Targeting {len(all_keys)} currencies "
          f"({len(services_pmi.SPMI_INVESTING_URLS)} Investing + "
          f"{len(services_pmi.SPMI_TE_URLS)} TE + "
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
    orig_myfx = services_pmi.SPMI_MYFXBOOK_URLS.copy()
    try:
        for k in list(services_pmi.SPMI_INVESTING_URLS.keys()):
            if k not in failed:
                del services_pmi.SPMI_INVESTING_URLS[k]
        for k in list(services_pmi.SPMI_TE_URLS.keys()):
            if k not in failed:
                del services_pmi.SPMI_TE_URLS[k]
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
    print("REFRESHING PPI YoY (NZD only via Investing)")
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


def main():
    print("=== Investing.com cache refresh (mPMI + sPMI + CPI + PPI) ===")
    refresh_mpmi()
    refresh_spmi()
    refresh_cpi()
    refresh_ppi()
    print("\nDone. Now commit + push:")
    print("  git add data/cache/investing_pmi.json data/cache/spmi.json data/cache/investing_cpi.json data/cache/investing_ppi.json")
    print("  git commit -m 'Refresh Investing cache'")
    print("  git push")


if __name__ == "__main__":
    main()
