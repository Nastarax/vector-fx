"""
Services PMI (sPMI) fetcher.

Data sources by currency:
  - USD, EUR, GBP, AUD, JPY, CAD: Investing.com  (momentum scoring: Actual vs Previous)
  - CHF: Investing.com procure.ch PMI page       (Actual vs Forecast scoring)
  - NZD: Myfxbook NZ Services PSI calendar       (Actual vs Forecast scoring)

CHF and NZD use Actual vs Forecast scoring per user preference; the other 6
keep the standard Investing.com momentum methodology used for mPMI. Scoring
branch lives in score_pair.py.

The cache lives at data/cache/spmi.json (separate from mPMI's investing_pmi.json).
Refresh runs locally via scripts/refresh_investing.py because Investing.com
and Myfxbook both block GitHub Actions IPs (Cloudflare).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except ImportError:
    import requests as cffi_requests  # type: ignore
    HAS_CFFI = False

from bs4 import BeautifulSoup

from src.fetchers.investing import (
    _fetch_with_retries,
    parse_latest_release,
    _parse_num,
    _parse_date,
)

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"
CACHE_FILE = CACHE_DIR / "spmi.json"


# Investing.com Services PMI page URLs.
SPMI_INVESTING_URLS: dict[str, str] = {
    "USD": "https://www.investing.com/economic-calendar/united-states-services-purchasing-managers-index-(pmi)-1062",
    "EUR": "https://www.investing.com/economic-calendar/european-services-purchasing-managers-index-(pmi)-272",
    "GBP": "https://www.investing.com/economic-calendar/united-kingdom-services-purchasing-managers-index-(pmi)-274",
    "AUD": "https://www.investing.com/economic-calendar/services-pmi-1839",       # S&P Global Australia Services PMI
    "JPY": "https://www.investing.com/economic-calendar/services-pmi-1912",       # S&P Global Japan Services PMI
    "CAD": "https://www.investing.com/economic-calendar/services-pmi-2265",       # Canada Services PMI
    "CHF": "https://www.investing.com/economic-calendar/procure.ch-pmi-278",      # procure.ch PMI (CHF)
}

# Myfxbook calendar pages. Different parser; needs curl_cffi Chrome impersonation.
SPMI_MYFXBOOK_URLS: dict[str, str] = {
    "NZD": "https://www.myfxbook.com/forex-economic-calendar/new-zealand/services-nz-psi",
}

# TradingEconomics pages. Empty now that CHF moved to Investing and NZD moved
# to Myfxbook. Kept as a hook in case we want to add TE-sourced sPMI in future.
SPMI_TE_URLS: dict[str, str] = {}


_TE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://tradingeconomics.com/",
}


_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def parse_te_meta_description(html: str) -> dict | None:
    """
    Parse TE's meta description, which always follows the same template:
      "Services PMI in <Country> <verb> to <actual> points in <month> from
       <previous> points in <month> of <year>."

    Where <verb> is one of: increased, decreased, rose, fell, remained,
    declined, climbed, edged up/down, etc.

    Returns: {"date": "YYYY-MM-01", "actual": float, "previous": float} or None
    """
    soup = BeautifulSoup(html, "html.parser")
    desc = None
    for meta in soup.find_all("meta"):
        name = (meta.get("name") or meta.get("property") or "").lower()
        if name in ("description", "og:description"):
            content = meta.get("content", "")
            if "Services PMI" in content and "to" in content and "from" in content:
                desc = content
                break

    if not desc:
        return None

    # Pattern: any verb + "to <num> points in <month> from <num> points in <month> of <year>"
    m = re.search(
        r"to\s+([-\d.,]+)\s+points?\s+in\s+([A-Za-z]+)\s+"
        r"from\s+([-\d.,]+)\s+points?\s+in\s+[A-Za-z]+\s+of\s+(\d{4})",
        desc,
    )
    if not m:
        return None

    actual = _parse_num(m.group(1))
    actual_month_name = m.group(2).lower()
    previous = _parse_num(m.group(3))
    year = int(m.group(4))

    actual_month = _MONTHS.get(actual_month_name)
    if actual_month is None:
        return None

    # Note: TE phrases like "to X in April from Y in March of 2026" - the
    # "of 2026" applies to both months. If the actual month is January and the
    # previous month is December, the previous month belongs to year-1, but for
    # our purposes we only need the date of the actual reading.
    date_str = f"{year:04d}-{actual_month:02d}-01"

    if actual is None or previous is None:
        return None

    return {
        "date": date_str,
        "actual": actual,
        "previous": previous,
        "forecast": None,  # TE meta doesn't include forecast
    }


def _fetch_te(url: str, max_attempts: int = 3) -> str | None:
    """Simple retry loop for TE pages. Less aggressive blocking than Investing,
    so we don't bother with warmed sessions."""
    profiles = ["chrome120", "chrome124", "safari17_2"]
    for attempt in range(max_attempts):
        profile = profiles[attempt % len(profiles)]
        try:
            if HAS_CFFI:
                r = cffi_requests.get(url, impersonate=profile, timeout=20)
            else:
                r = cffi_requests.get(url, headers=_TE_HEADERS, timeout=20)
            if r.status_code == 200:
                return r.text
        except Exception:
            pass
        time.sleep(2 ** (attempt + 1))
    return None


def _fetch_myfxbook(url: str, max_attempts: int = 3) -> str | None:
    """Cloudflare-protected, needs Chrome TLS impersonation."""
    profiles = ["chrome120", "chrome124", "safari17_2"]
    for attempt in range(max_attempts):
        profile = profiles[attempt % len(profiles)]
        try:
            if HAS_CFFI:
                r = cffi_requests.get(url, impersonate=profile, timeout=20)
            else:
                r = cffi_requests.get(url, headers=_TE_HEADERS, timeout=20)
            if r.status_code == 200:
                return r.text
        except Exception:
            pass
        time.sleep(2 ** (attempt + 1))
    return None


def _parse_myfxbook_calendar(html: str) -> dict | None:
    """
    Parse the latest release from a Myfxbook economic calendar event page.
    The page has a table of releases with columns: Date | Actual | Forecast |
    Previous. We grab the most recent row that has Actual filled in.

    Returns {"date": "YYYY-MM-DD", "actual": float, "forecast": float|None,
             "previous": float|None} or None on failure.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Find any table with Actual/Forecast/Previous header cells. Myfxbook
    # styles the header text in span elements inside <th>.
    target_table = None
    for table in soup.find_all("table"):
        header_text = " ".join(th.get_text(strip=True).lower() for th in table.find_all("th"))
        if "actual" in header_text and "forecast" in header_text and "previous" in header_text:
            target_table = table
            break
    if not target_table:
        return None

    headers = [th.get_text(strip=True).lower() for th in target_table.find_all("th")]
    def col(label: str) -> int:
        for i, h in enumerate(headers):
            if label in h:
                return i
        return -1

    i_date = col("date")
    i_actual = col("actual")
    i_forecast = col("forecast")
    i_previous = col("previous")
    if i_actual < 0:
        return None

    body = target_table.find("tbody") or target_table
    for row in body.find_all("tr"):
        cells = row.find_all("td")
        if not cells or i_actual >= len(cells):
            continue
        actual_raw = cells[i_actual].get_text(strip=True)
        actual = _parse_num(actual_raw)
        if actual is None:
            continue
        forecast = _parse_num(cells[i_forecast].get_text(strip=True)) if i_forecast >= 0 and i_forecast < len(cells) else None
        previous = _parse_num(cells[i_previous].get_text(strip=True)) if i_previous >= 0 and i_previous < len(cells) else None
        date_str = None
        if i_date >= 0 and i_date < len(cells):
            raw_date = cells[i_date].get_text(strip=True)
            date_str = _parse_date(raw_date)
        return {
            "date": date_str or "",
            "actual": actual,
            "forecast": forecast,
            "previous": previous,
        }
    return None


def _load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache: dict):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


# Tracks which currencies were freshly fetched on the most recent fetch_spmi
# call. Cleared at start of each call. Read by refresh_investing.py.
_LAST_FRESH: set[str] = set()


def fetch_spmi(sleep_between: float = 4.0) -> dict[str, dict]:
    """
    Hit all 8 sPMI pages (6 Investing + 2 TE), return dict keyed by currency.
    Falls back to cache on per-URL failure.
    """
    global _LAST_FRESH
    _LAST_FRESH = set()
    cache = _load_cache()
    results: dict[str, dict] = {}
    fresh_count = 0
    cached_count = 0

    # Investing.com pages (use the warmed-session retry logic from investing.py)
    for ccy, url in SPMI_INVESTING_URLS.items():
        try:
            status, html = _fetch_with_retries(url)
            if status != 200 or not html:
                print(f"[spmi] {ccy} (Investing) all retries failed (status {status}), using cache")
                if ccy in cache:
                    results[ccy] = cache[ccy]
                    cached_count += 1
                time.sleep(sleep_between)
                continue
            parsed = parse_latest_release(html)
            if not parsed or parsed.get("actual") is None or parsed.get("previous") is None:
                print(f"[spmi] {ccy} (Investing) parse failed/incomplete, using cache")
                if ccy in cache:
                    results[ccy] = cache[ccy]
                    cached_count += 1
                time.sleep(sleep_between)
                continue
            results[ccy] = parsed
            cache[ccy] = parsed
            fresh_count += 1
            _LAST_FRESH.add(ccy)
            print(f"[spmi] {ccy} (Investing) {parsed}")
        except Exception as e:
            print(f"[spmi] {ccy} (Investing) error: {e}, using cache")
            if ccy in cache:
                results[ccy] = cache[ccy]
                cached_count += 1
        time.sleep(sleep_between)

    # TradingEconomics pages
    for ccy, url in SPMI_TE_URLS.items():
        try:
            html = _fetch_te(url)
            if not html:
                print(f"[spmi] {ccy} (TE) fetch failed, using cache")
                if ccy in cache:
                    results[ccy] = cache[ccy]
                    cached_count += 1
                time.sleep(sleep_between)
                continue
            parsed = parse_te_meta_description(html)
            if not parsed or parsed.get("actual") is None or parsed.get("previous") is None:
                print(f"[spmi] {ccy} (TE) parse failed, using cache")
                if ccy in cache:
                    results[ccy] = cache[ccy]
                    cached_count += 1
                time.sleep(sleep_between)
                continue
            results[ccy] = parsed
            cache[ccy] = parsed
            fresh_count += 1
            _LAST_FRESH.add(ccy)
            print(f"[spmi] {ccy} (TE) {parsed}")
        except Exception as e:
            print(f"[spmi] {ccy} (TE) error: {e}, using cache")
            if ccy in cache:
                results[ccy] = cache[ccy]
                cached_count += 1
        time.sleep(sleep_between)

    # Myfxbook pages (NZD currently)
    for ccy, url in SPMI_MYFXBOOK_URLS.items():
        try:
            html = _fetch_myfxbook(url)
            if not html:
                print(f"[spmi] {ccy} (Myfxbook) fetch failed, using cache")
                if ccy in cache:
                    results[ccy] = cache[ccy]
                    cached_count += 1
                time.sleep(sleep_between)
                continue
            parsed = _parse_myfxbook_calendar(html)
            if not parsed or parsed.get("actual") is None:
                print(f"[spmi] {ccy} (Myfxbook) parse failed, using cache")
                if ccy in cache:
                    results[ccy] = cache[ccy]
                    cached_count += 1
                time.sleep(sleep_between)
                continue
            results[ccy] = parsed
            cache[ccy] = parsed
            fresh_count += 1
            _LAST_FRESH.add(ccy)
            print(f"[spmi] {ccy} (Myfxbook) {parsed}")
        except Exception as e:
            print(f"[spmi] {ccy} (Myfxbook) error: {e}, using cache")
            if ccy in cache:
                results[ccy] = cache[ccy]
                cached_count += 1
        time.sleep(sleep_between)

    _save_cache(cache)
    total_urls = len(SPMI_INVESTING_URLS) + len(SPMI_TE_URLS) + len(SPMI_MYFXBOOK_URLS)
    print(f"[spmi] {fresh_count} fresh, {cached_count} from cache, {len(results)}/{total_urls} total")
    return results


def load_cached() -> dict[str, dict]:
    """Read-only access to the sPMI cache, used by main.py."""
    return _load_cache()


if __name__ == "__main__":
    print(f"curl_cffi installed: {HAS_CFFI}")
    data = fetch_spmi()
    print("\nSummary:")
    for ccy in ["USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD"]:
        rel = data.get(ccy)
        if not rel:
            print(f"  {ccy}: (missing)")
            continue
        a, p = rel.get("actual"), rel.get("previous")
        if a is None or p is None:
            direction = "?"
        elif a > p:
            direction = "UP"
        elif a < p:
            direction = "DOWN"
        else:
            direction = "FLAT"
        print(f"  {ccy}: actual={a} previous={p}  -> {direction}")
