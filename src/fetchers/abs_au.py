"""
Australian Bureau of Statistics - Monthly Household Spending Indicator.

Replaces Trading Economics's retail sales for AUD. ABS deprecated the old
Retail Trade indicator; the Monthly Household Spending Indicator (MHSI) is
the official successor.

Scoring methodology (Yanaël's spec):
  current month MoM % > previous month MoM %  -> +1 (acceleration, bullish)
  current month MoM % < previous month MoM %  -> -1 (deceleration, bearish)
  equal                                       ->  0

We pull from the time-series table on the ABS "latest release" page (the
"Household spending, current price, seasonally adjusted estimate" table)
because it contains both the latest month AND the previous month. The
Key Statistics summary at the top only shows the latest.

No anti-bot issues with this domain (ABS is a gov site), so we can scrape
on every main.py run.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path

try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except ImportError:
    import requests as cffi_requests  # type: ignore
    HAS_CFFI = False

from bs4 import BeautifulSoup

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"
CACHE_FILE = CACHE_DIR / "abs_au_household.json"

URL = "https://www.abs.gov.au/statistics/economy/finance/monthly-household-spending-indicator/latest-release"


def _parse_pct(s: str) -> float | None:
    if not s:
        return None
    t = s.strip().replace(",", "")
    if t.endswith("%"):
        t = t[:-1]
    try:
        return float(t)
    except ValueError:
        return None


def _parse_month_label(s: str) -> str | None:
    """Convert 'Mar-2026' to '2026-03-01'."""
    if not s:
        return None
    m = re.match(r"([A-Za-z]+)-(\d{4})", s.strip())
    if not m:
        return None
    try:
        d = datetime.strptime(f"{m.group(1)} 01 {m.group(2)}", "%b %d %Y")
        return d.strftime("%Y-%m-%d")
    except ValueError:
        return None


def _fetch_html(timeout: int = 25) -> str | None:
    """ABS isn't bot-protected so a single request with Chrome impersonation is enough.
    Still retries on transient network errors."""
    profiles = ["chrome120", "chrome116", "safari17_2"]
    for profile in profiles:
        try:
            if HAS_CFFI:
                r = cffi_requests.get(URL, impersonate=profile, timeout=timeout)
            else:
                r = cffi_requests.get(URL, timeout=timeout)
            if r.status_code == 200:
                return r.text
        except Exception:
            time.sleep(1.5)
    return None


def parse_mhsi(html: str) -> dict | None:
    """
    Extract the latest two months' MoM % from the ABS MHSI page.
    Returns: {"current_month": "YYYY-MM-DD", "current_mom": float,
              "previous_month": "YYYY-MM-DD", "previous_mom": float}
    """
    soup = BeautifulSoup(html, "html.parser")
    target = None
    for tbl in soup.find_all("table"):
        cap = tbl.find("caption")
        cap_text = cap.get_text(" ", strip=True) if cap else ""
        # Target the "Household spending, current price, seasonally adjusted estimate" table.
        # That table has the full monthly history with Monthly (%) values.
        if (
            "Household spending" in cap_text
            and "current price" in cap_text.lower()
            and "seasonally adjusted estimate" in cap_text.lower()
            and "goods" not in cap_text.lower()
            and "services" not in cap_text.lower()
            and "discretionary" not in cap_text.lower()
        ):
            target = tbl
            break

    if not target:
        return None

    rows = target.find_all("tr")
    # Skip header row (index 0). Data rows are ordered oldest -> newest, so the
    # last two rows are previous month + current month.
    if len(rows) < 3:
        return None

    last = [c.get_text(" ", strip=True) for c in rows[-1].find_all(["th", "td"])]
    prev = [c.get_text(" ", strip=True) for c in rows[-2].find_all(["th", "td"])]

    # Row layout: [month_label, monthly_pct, through_the_year_pct]
    if len(last) < 2 or len(prev) < 2:
        return None

    current_month = _parse_month_label(last[0])
    current_mom = _parse_pct(last[1])
    previous_month = _parse_month_label(prev[0])
    previous_mom = _parse_pct(prev[1])

    if current_mom is None or previous_mom is None:
        return None

    return {
        "current_month": current_month,
        "current_mom": current_mom,
        "previous_month": previous_month,
        "previous_mom": previous_mom,
    }


def _load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(data: dict):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(data, f, indent=2)


def fetch_mhsi() -> dict | None:
    """
    Fetch + parse ABS MHSI. Caches result. Falls back to cache on failure.
    """
    html = _fetch_html()
    if not html:
        print("[abs] fetch failed, falling back to cache")
        cached = _load_cache()
        return cached or None
    parsed = parse_mhsi(html)
    if not parsed:
        print("[abs] parse failed, falling back to cache")
        cached = _load_cache()
        return cached or None
    _save_cache(parsed)
    print(f"[abs] AUD MHSI: {parsed['current_month']} MoM={parsed['current_mom']}% (prev {parsed['previous_month']}: {parsed['previous_mom']}%)")
    return parsed


def load_cached() -> dict | None:
    """Read-only access to the cache, used in backtest mode."""
    return _load_cache() or None


if __name__ == "__main__":
    print(f"curl_cffi installed: {HAS_CFFI}")
    data = fetch_mhsi()
    if data:
        cur, prev = data["current_mom"], data["previous_mom"]
        direction = "UP (bullish)" if cur > prev else ("DOWN (bearish)" if cur < prev else "FLAT")
        print(f"\nScoring: current {cur}% vs previous {prev}% -> {direction}")
