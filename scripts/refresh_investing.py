"""
Daily Investing.com mPMI cache refresh.

Why this script exists separately from main.py:
  PMI is a monthly indicator. There's no reason to scrape Investing.com 24
  times a day from main.py. Doing so just risks getting our IP Cloudflare-
  flagged and slows the hourly heatmap run.

Strategy here: be slow and patient.
  - 12s between currencies (vs 4s in main.py)
  - 5 retries per URL (vs 3 in main.py)
  - Longer warm-up between session moves
  - Re-tries failed currencies in a second pass with extra delay

Run this once a day (Windows Task Scheduler or GitHub Actions cron). main.py
will read whatever's in data/cache/investing_pmi.json regardless of whether
this script succeeded fully.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.fetchers import investing


def main():
    print("=== Investing.com mPMI daily refresh ===")
    print(f"Targeting {len(investing.MPMI_URLS)} currencies\n")

    # First pass: standard fetch with patient 12s sleeps
    print("--- Pass 1: full fetch ---")
    first = investing.fetch_mpmi(sleep_between=12.0)

    failed = [c for c in investing.MPMI_URLS if c not in first]

    if not failed:
        print("\nAll currencies fetched fresh on first pass.")
        return

    # Second pass: only retry the ones that failed, with even more space
    print(f"\n--- Pass 2: retry {failed} after 60s cooldown ---")
    time.sleep(60)

    # Temporarily restrict the URL map to the failed currencies
    original = investing.MPMI_URLS
    investing.MPMI_URLS = {c: original[c] for c in failed}
    try:
        second = investing.fetch_mpmi(sleep_between=18.0)
    finally:
        investing.MPMI_URLS = original

    still_failed = [c for c in failed if c not in second]
    if still_failed:
        print(f"\nStill failing after 2 passes: {still_failed}")
        print("Cache retains last successful values for these currencies.")
    else:
        print("\nAll previously-failed currencies recovered on pass 2.")


if __name__ == "__main__":
    main()
