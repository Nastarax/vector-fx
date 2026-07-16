"""
Investing.com PMI fetcher.

Pulls the "Latest Release" block (Actual / Forecast / Previous) from per-currency
Manufacturing PMI calendar pages. Used as the source of truth for the mPMI
column (EdgeFinder-style momentum scoring: current Actual vs Previous).

We hit one URL per supported currency. Each page has the same DOM structure:
a "Latest Release <date>" header followed by Actual / Forecast / Previous cells.

Cached to data/cache/investing_pmi.json so we have a fallback when Cloudflare
blocks us or Investing reshuffles their layout.

Uses curl_cffi Chrome impersonation (same trick as ForexFactory / Myfxbook).
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
CACHE_FILE = CACHE_DIR / "investing_pmi.json"


# Manufacturing PMI page URLs per currency.
MPMI_URLS: dict[str, str] = {
    "NZD": "https://www.investing.com/economic-calendar/business-nz-pmi-338",
    "AUD": "https://www.investing.com/economic-calendar/judo-bank-australia-manufacturing-purchasing-managers-index-(pmi)-1838",
    "CHF": "https://www.investing.com/economic-calendar/procure.ch-pmi-278",
    # USD uses ISM Manufacturing PMI (matches EdgeFinder), not S&P Global PMI.
    "USD": "https://www.investing.com/economic-calendar/ism-manufacturing-pmi-173",
    "GBP": "https://www.investing.com/economic-calendar/united-kingdom-manufacturing-purchasing-managers-index-(pmi)-204",
    "EUR": "https://www.investing.com/economic-calendar/european-manufacturing-purchasing-managers-index-(pmi)-201",
    "CAD": "https://www.investing.com/economic-calendar/canada-manufacturing-purchasing-managers-index-(pmi)-1029",
    "JPY": "https://www.investing.com/economic-calendar/japan-manufacturing-purchasing-managers-index-(pmi)-202",
}


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.investing.com/economic-calendar/",
}


def _new_session(profile: str):
    """Open a Cloudflare-warmed session: visit Investing homepage + economic
    calendar landing page first so we collect the cf_clearance / __cf_bm cookies.
    Subsequent requests on this session look like a real browsing user, not a bot.
    """
    if not HAS_CFFI:
        s = cffi_requests.Session()
        s.headers.update(_HEADERS)
        return s
    s = cffi_requests.Session()
    try:
        s.get("https://www.investing.com/", impersonate=profile, timeout=15)
        time.sleep(1.0)
        s.get("https://www.investing.com/economic-calendar/", impersonate=profile, timeout=15)
        time.sleep(0.8)
    except Exception:
        pass
    return s


def _get(url: str, session=None, profile: str = "chrome120", timeout: int = 20):
    """Single request through an existing session (or one-shot fallback)."""
    if session is not None:
        if HAS_CFFI:
            return session.get(url, impersonate=profile, timeout=timeout)
        return session.get(url, timeout=timeout)
    if HAS_CFFI:
        return cffi_requests.get(url, impersonate=profile, timeout=timeout)
    return cffi_requests.get(url, headers=_HEADERS, timeout=timeout)


def _fetch_with_retries(url: str, max_attempts: int = 3) -> "tuple[int, str | None]":
    """
    Fetch a URL, rotating both impersonation profile AND session on 4xx/5xx.
    Returns (status_code, html) or (status_code_of_last_attempt, None) on failure.

    Why this matters: once Cloudflare flags a session with 403, every request on
    that session keeps returning 403. We need a fresh warmed session per retry.

    On GitHub Actions the datacenter IP is Cloudflare-blocked regardless, so when
    an unblocker key is configured we route through the scraping API instead (see
    src/fetchers/unblock.py). Locally, no key -> this stays on curl_cffi.
    """
    from src.fetchers import unblock
    if unblock.enabled():
        return unblock.fetch(url)
    profiles = ["chrome120", "chrome124", "safari17_2", "chrome116"]
    last_status = 0
    for attempt in range(max_attempts):
        profile = profiles[attempt % len(profiles)]
        session = _new_session(profile)
        try:
            r = _get(url, session=session, profile=profile)
            last_status = r.status_code
            if r.status_code == 200:
                return r.status_code, r.text
        except Exception:
            pass
        # exponential backoff between attempts: 2s, 4s, 8s
        time.sleep(2 ** (attempt + 1))
    return last_status, None


def _parse_num(s: str | None) -> float | None:
    if s is None:
        return None
    t = s.strip().replace(",", "")
    if not t or t == "-":
        return None
    multiplier = 1.0
    if t.endswith("K"):
        multiplier, t = 1_000, t[:-1]
    elif t.endswith("M"):
        multiplier, t = 1_000_000, t[:-1]
    elif t.endswith("B"):
        multiplier, t = 1_000_000_000, t[:-1]
    if t.endswith("%"):
        t = t[:-1]
    try:
        return float(t) * multiplier
    except ValueError:
        return None


def _parse_date(raw: str) -> str | None:
    """'May 01, 2026' -> '2026-05-01'."""
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), "%b %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def parse_latest_release(html: str) -> dict | None:
    """
    Find the 'Latest Release ... Actual ... Forecast ... Previous ...' block
    and return a dict with parsed numeric values + ISO date.
    """
    soup = BeautifulSoup(html, "html.parser")
    for elem in soup.find_all(string=re.compile(r"Latest Release", re.IGNORECASE)):
        parent = elem.parent
        for _ in range(8):
            if parent is None:
                break
            text = parent.get_text(" ", strip=True)
            if "Latest Release" in text and "Actual" in text and "Previous" in text:
                m = re.search(
                    r"Latest Release\s+([A-Za-z]+\s+\d+,?\s+\d{4})\s+"
                    r"Actual\s+([-\d.,]+|\s*-?\s*)\s+"
                    r"Forecast\s+([-\d.,]+|\s*-?\s*)\s+"
                    r"Previous\s+([-\d.,]+|\s*-?\s*)",
                    text,
                )
                if m:
                    return {
                        "date": _parse_date(m.group(1)),
                        "actual": _parse_num(m.group(2)),
                        "forecast": _parse_num(m.group(3)),
                        "previous": _parse_num(m.group(4)),
                    }
                return None
            parent = parent.parent
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


# Tracks which currencies were freshly fetched on the most recent fetch_mpmi
# call. Cleared at start of each call. Used by refresh_investing.py to know
# which truly need retry vs which just fell back to cache.
_LAST_FRESH: set[str] = set()


def fetch_mpmi(sleep_between: float = 4.0) -> dict[str, dict]:
    """
    Hit all 8 PMI pages, return dict keyed by currency.

    Each value is a release dict:
      {"date": "2026-05-01", "actual": 54.5, "forecast": 54.0, "previous": 54.0}

    Strategy: warmed session + retries with profile rotation per URL. Falls back
    to last cached reading on per-URL failure so a Cloudflare block doesn't
    blank the whole mPMI column. Adds 4s sleep between currencies to avoid
    triggering Investing's rate limiter.
    """
    global _LAST_FRESH
    _LAST_FRESH = set()
    cache = _load_cache()
    results: dict[str, dict] = {}
    fresh_count = 0
    cached_count = 0

    for ccy, url in MPMI_URLS.items():
        try:
            status, html = _fetch_with_retries(url)
            if status != 200 or not html:
                print(f"[investing] {ccy} all retries failed (last status {status}), using cache")
                if ccy in cache:
                    results[ccy] = cache[ccy]
                    cached_count += 1
                time.sleep(sleep_between)
                continue
            parsed = parse_latest_release(html)
            if not parsed or parsed.get("actual") is None or parsed.get("previous") is None:
                print(f"[investing] {ccy} parse failed/incomplete, using cache")
                if ccy in cache:
                    results[ccy] = cache[ccy]
                    cached_count += 1
                time.sleep(sleep_between)
                continue
            results[ccy] = parsed
            cache[ccy] = parsed
            fresh_count += 1
            _LAST_FRESH.add(ccy)
            print(f"[investing] {ccy} {parsed}")
        except Exception as e:
            print(f"[investing] {ccy} error: {e}, using cache")
            if ccy in cache:
                results[ccy] = cache[ccy]
                cached_count += 1
        time.sleep(sleep_between)

    _save_cache(cache)
    print(f"[investing] mPMI: {fresh_count} fresh, {cached_count} from cache, {len(results)}/{len(MPMI_URLS)} total")
    return results


def load_cached() -> dict[str, dict]:
    """Read-only access to the cache, used in backtest mode."""
    return _load_cache()


if __name__ == "__main__":
    print(f"curl_cffi installed: {HAS_CFFI}")
    data = fetch_mpmi()
    print("\nSummary:")
    for ccy, rel in sorted(data.items()):
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
