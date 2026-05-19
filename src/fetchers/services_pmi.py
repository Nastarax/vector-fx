"""
Services PMI (sPMI) fetcher.

Data sources by currency:
  - USD, EUR, GBP, AUD, JPY, CAD: Investing.com  (momentum scoring: Actual vs Previous)
  - CHF: Investing.com procure.ch PMI page       (Actual vs Forecast scoring)
  - NZD: BusinessNZ official PSI page            (Actual vs Previous scoring, no forecast)

CHF uses Actual vs Forecast (forecast available); NZD falls back to Actual vs
Previous because BusinessNZ doesn't publish a consensus forecast. Scoring
branch in score_pair.py already handles the fallback (forecast -> previous).
The other 6 keep the standard Investing.com momentum methodology used for mPMI.

The cache lives at data/cache/spmi.json (separate from mPMI's investing_pmi.json).
Refresh runs locally via scripts/refresh_investing.py because Investing.com
blocks GitHub Actions IPs (Cloudflare). BusinessNZ is open and could be moved
to GH Actions, but staying local for now to keep one refresh path.
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

# BusinessNZ PSI landing page. We scrape the landing page, find the latest
# release article, then parse the headline value, previous month value, and
# reported month from the article body. No forecast is published.
SPMI_BUSINESSNZ_URLS: dict[str, str] = {
    "NZD": "https://businessnz.org.nz/psi",
}

# Myfxbook calendar pages. Kept empty (NZD moved to BusinessNZ direct).
# Hook left in place in case we want to add a Myfxbook-sourced sPMI again.
SPMI_MYFXBOOK_URLS: dict[str, str] = {}

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


def _fetch_businessnz(url: str, max_attempts: int = 3) -> str | None:
    """BusinessNZ uses Cloudflare too; chrome impersonation is safest."""
    profiles = ["chrome120", "chrome124", "safari17_2"]
    for attempt in range(max_attempts):
        profile = profiles[attempt % len(profiles)]
        try:
            if HAS_CFFI:
                r = cffi_requests.get(url, impersonate=profile, timeout=30)
            else:
                r = cffi_requests.get(url, headers=_TE_HEADERS, timeout=30)
            if r.status_code == 200:
                return r.text
        except Exception:
            pass
        time.sleep(2 ** (attempt + 1))
    return None


def _find_latest_businessnz_article_url(landing_html: str) -> str | None:
    """The landing page lists every PSI release post. The first link that
    points to /psi/<slug> (not /psi/ itself, not /our-resources/psi/) is the
    most recent release."""
    soup = BeautifulSoup(landing_html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/psi/" not in href:
            continue
        # Skip the index page and the resources hub
        if href.rstrip("/").endswith("/psi"):
            continue
        if "/our-resources/" in href:
            continue
        # Skip pagination, category links, etc.
        if any(seg in href for seg in ["/page/", "/category/", "/tag/", "?"]):
            continue
        # Must have actual link text (skip empty <a> wrappers)
        if not a.get_text(strip=True):
            continue
        return href
    return None


def _parse_businessnz_article(html: str) -> dict | None:
    """
    Pull (latest, previous, date) from a BusinessNZ PSI release article.

    The body always contains two sentences in the form:
      "The PSI for <Month> was <value>"
      "The PSI reading for <Month> was <value>"

    Date is taken from the article:published_time meta. The reported month
    determines the YYYY-MM-01 release date. If the reported month is later in
    the calendar than the publish month (e.g. December reported in a January
    article), the report belongs to the previous year.
    """
    # Pattern 1: latest reading (e.g. "The PSI for April was 48.9")
    m_latest = re.search(
        r"The PSI for\s+([A-Za-z]+)\s+was\s+(\d+(?:[.,]\d+)?)",
        html, re.IGNORECASE,
    )
    # Pattern 2: previous month reading
    m_prev = re.search(
        r"The PSI reading for\s+([A-Za-z]+)\s+was\s+(\d+(?:[.,]\d+)?)",
        html, re.IGNORECASE,
    )
    if not m_latest:
        return None

    latest_month_name = m_latest.group(1).lower()
    latest_val = _parse_num(m_latest.group(2))
    prev_val = _parse_num(m_prev.group(2)) if m_prev else None

    latest_month = _MONTHS.get(latest_month_name)
    if latest_month is None or latest_val is None:
        return None

    # Determine year from published_time meta
    year = None
    pub_match = re.search(
        r'article:published_time"[^>]*content="(\d{4})-(\d{2})',
        html,
    )
    if pub_match:
        pub_year = int(pub_match.group(1))
        pub_month = int(pub_match.group(2))
        # If reported month is ahead of publish month, it's last year's data
        year = pub_year - 1 if latest_month > pub_month else pub_year
    if year is None:
        # Fall back: assume current year, but skip if we can't be sure
        from datetime import datetime
        year = datetime.utcnow().year

    date_str = f"{year:04d}-{latest_month:02d}-01"
    return {
        "date": date_str,
        "actual": latest_val,
        "previous": prev_val,
        "forecast": None,  # BusinessNZ does not publish a consensus forecast
    }


def fetch_businessnz_psi(url: str) -> dict | None:
    """Two-step fetch: landing page -> latest article URL -> article body."""
    landing = _fetch_businessnz(url)
    if not landing:
        return None
    article_url = _find_latest_businessnz_article_url(landing)
    if not article_url:
        return None
    article_html = _fetch_businessnz(article_url)
    if not article_html:
        return None
    return _parse_businessnz_article(article_html)


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

    # BusinessNZ (NZD)
    for ccy, url in SPMI_BUSINESSNZ_URLS.items():
        try:
            parsed = fetch_businessnz_psi(url)
            if not parsed or parsed.get("actual") is None:
                print(f"[spmi] {ccy} (BusinessNZ) parse failed, using cache")
                if ccy in cache:
                    results[ccy] = cache[ccy]
                    cached_count += 1
                time.sleep(sleep_between)
                continue
            results[ccy] = parsed
            cache[ccy] = parsed
            fresh_count += 1
            _LAST_FRESH.add(ccy)
            print(f"[spmi] {ccy} (BusinessNZ) {parsed}")
        except Exception as e:
            print(f"[spmi] {ccy} (BusinessNZ) error: {e}, using cache")
            if ccy in cache:
                results[ccy] = cache[ccy]
                cached_count += 1
        time.sleep(sleep_between)

    # Myfxbook pages (kept for future use; SPMI_MYFXBOOK_URLS is empty now)
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
    total_urls = (
        len(SPMI_INVESTING_URLS)
        + len(SPMI_TE_URLS)
        + len(SPMI_BUSINESSNZ_URLS)
        + len(SPMI_MYFXBOOK_URLS)
    )
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
