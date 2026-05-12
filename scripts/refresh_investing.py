"""
Local PMI cache refresh (manufacturing + services).

Why this script exists separately from main.py:
  PMI is a monthly indicator. There's no reason to scrape Investing.com 24
  times a day from main.py. Doing so just risks getting our IP Cloudflare-
  flagged and slows the hourly heatmap run.

Strategy here: be slow and patient. Each fetcher does two passes (full sweep,
then a retry-only second pass) with extended sleeps for the failed currencies.

Run this locally on Yanaël's Windows machine, then commit + push the JSON
caches. GitHub Actions can't run this because Cloudflare blocks GitHub IPs.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.fetchers import investing, services_pmi


def _retry_failed(module, all_urls: dict, failed: list, sleep_between: float, cooldown: int = 60):
    """Common second-pass logic: cool down, then retry only the failed currencies."""
    if not failed:
        return {}
    print(f"\n--- Pass 2: retry {failed} after {cooldown}s cooldown ---")
    time.sleep(cooldown)
    original = all_urls.copy()
    # Temporarily restrict to failed currencies
    keys_to_restore = list(all_urls.keys())
    for k in keys_to_restore:
        if k not in failed:
            del all_urls[k]
    try:
        result = module.fetch_mpmi(sleep_between=sleep_between) if module is investing else module.fetch_spmi(sleep_between=sleep_between)
    finally:
        all_urls.clear()
        all_urls.update(original)
    return result


def refresh_mpmi():
    print("\n============================================")
    print("REFRESHING MANUFACTURING PMI (mPMI)")
    print("============================================")
    print(f"Targeting {len(investing.MPMI_URLS)} currencies\n")

    print("--- Pass 1: full fetch ---")
    first = investing.fetch_mpmi(sleep_between=12.0)

    failed = [c for c in investing.MPMI_URLS if c not in first]
    if not failed:
        print("\nmPMI: all currencies fetched fresh on first pass.")
        return

    second = _retry_failed(investing, investing.MPMI_URLS, failed, sleep_between=18.0)
    still_failed = [c for c in failed if c not in second]
    if still_failed:
        print(f"\nmPMI: still failing after 2 passes: {still_failed}")
        print("Cache retains last successful values for these currencies.")
    else:
        print("\nmPMI: all previously-failed currencies recovered on pass 2.")


def refresh_spmi():
    print("\n============================================")
    print("REFRESHING SERVICES PMI (sPMI)")
    print("============================================")
    total = len(services_pmi.SPMI_INVESTING_URLS) + len(services_pmi.SPMI_TE_URLS)
    print(f"Targeting {total} currencies ({len(services_pmi.SPMI_INVESTING_URLS)} Investing + {len(services_pmi.SPMI_TE_URLS)} TE)\n")

    print("--- Pass 1: full fetch ---")
    first = services_pmi.fetch_spmi(sleep_between=12.0)

    all_keys = list(services_pmi.SPMI_INVESTING_URLS.keys()) + list(services_pmi.SPMI_TE_URLS.keys())
    failed = [c for c in all_keys if c not in first]
    if not failed:
        print("\nsPMI: all currencies fetched fresh on first pass.")
        return

    # For pass 2, restrict both Investing and TE URL dicts to the failed currencies
    print(f"\n--- Pass 2: retry {failed} after 60s cooldown ---")
    time.sleep(60)
    orig_investing = services_pmi.SPMI_INVESTING_URLS.copy()
    orig_te = services_pmi.SPMI_TE_URLS.copy()
    try:
        for k in list(services_pmi.SPMI_INVESTING_URLS.keys()):
            if k not in failed:
                del services_pmi.SPMI_INVESTING_URLS[k]
        for k in list(services_pmi.SPMI_TE_URLS.keys()):
            if k not in failed:
                del services_pmi.SPMI_TE_URLS[k]
        second = services_pmi.fetch_spmi(sleep_between=18.0)
    finally:
        services_pmi.SPMI_INVESTING_URLS.clear()
        services_pmi.SPMI_INVESTING_URLS.update(orig_investing)
        services_pmi.SPMI_TE_URLS.clear()
        services_pmi.SPMI_TE_URLS.update(orig_te)

    still_failed = [c for c in failed if c not in second]
    if still_failed:
        print(f"\nsPMI: still failing after 2 passes: {still_failed}")
        print("Cache retains last successful values for these currencies.")
    else:
        print("\nsPMI: all previously-failed currencies recovered on pass 2.")


def main():
    print("=== PMI cache refresh (mPMI + sPMI) ===")
    refresh_mpmi()
    refresh_spmi()
    print("\nDone. Now commit + push:")
    print("  git add data/cache/investing_pmi.json data/cache/spmi.json")
    print("  git commit -m 'Refresh PMI cache'")
    print("  git push")


if __name__ == "__main__":
    main()
