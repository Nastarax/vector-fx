"""
Investing.com GDP QoQ fetcher (JPY only).

Japan GDP Growth (QoQ), event id 119. Actual vs Forecast (fall back to Previous
if Forecast missing). The other 7 currencies stay on TE for GDP.

Same fetch/parse architecture as investing_ppi.py.
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
CACHE_FILE = CACHE_DIR / "investing_gdp.json"


GDP_URLS: dict[str, str] = {
    "JPY": "https://www.investing.com/economic-calendar/japan-gross-domestic-product-(gdp)-qoq-119",
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


def _get(url, session=None, profile="chrome120", timeout=20):
    if session is not None:
        if HAS_CFFI:
            return session.get(url, impersonate=profile, timeout=timeout)
        return session.get(url, timeout=timeout)
    if HAS_CFFI:
        return cffi_requests.get(url, impersonate=profile, timeout=timeout)
    return cffi_requests.get(url, headers=_HEADERS, timeout=timeout)


def _fetch_with_retries(url, max_attempts=3):
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
        time.sleep(2 ** (attempt + 1))
    return last_status, None


def _parse_num(s):
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


def _parse_date(raw):
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), "%b %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def parse_latest_release(html, debug_path=None):
    """Reads the 'Latest Release ... Actual ... Forecast ... Previous' block."""
    VAL = r"(-|--|[-−]?[\d.,]+\s*[KMB]?\s*%?)"
    soup = BeautifulSoup(html, "html.parser")
    raw_text_full = soup.get_text(" ", strip=True).replace(" ", " ")
    pat = (r"Latest Release\s+([A-Za-z]+\s+\d+,?\s+\d{4})\s+"
           r"Actual\s+" + VAL + r"\s+"
           r"Forecast\s+" + VAL + r"\s+"
           r"Previous\s+" + VAL)
    m = re.search(pat, raw_text_full)
    if m:
        return {
            "date": _parse_date(m.group(1)),
            "actual": _parse_num(m.group(2)),
            "forecast": _parse_num(m.group(3)),
            "previous": _parse_num(m.group(4)),
        }
    for elem in soup.find_all(string=re.compile(r"Latest Release", re.IGNORECASE)):
        parent = elem.parent
        for _ in range(8):
            if parent is None:
                break
            text = parent.get_text(" ", strip=True).replace(" ", " ")
            if "Latest Release" in text and "Actual" in text and "Previous" in text:
                m = re.search(pat, text)
                if m:
                    return {
                        "date": _parse_date(m.group(1)),
                        "actual": _parse_num(m.group(2)),
                        "forecast": _parse_num(m.group(3)),
                        "previous": _parse_num(m.group(4)),
                    }
                if debug_path:
                    Path(debug_path).write_text(text[:4000], encoding="utf-8")
                break
            parent = parent.parent
    return None


def _load_cache():
    if not CACHE_FILE.exists():
        return {}
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


# Tracks which currencies were freshly fetched on the most recent fetch_gdp
# call. Cleared at start of each call. Read by refresh_investing.py.
_LAST_FRESH: set[str] = set()


def fetch_gdp(sleep_between=4.0):
    """Hit GDP page(s) and return dict keyed by currency."""
    global _LAST_FRESH
    _LAST_FRESH = set()
    cache = _load_cache()
    results = {}
    fresh_count = 0
    cached_count = 0

    for ccy, url in GDP_URLS.items():
        try:
            status, html = _fetch_with_retries(url)
            if status != 200 or not html:
                print(f"[gdp] {ccy} all retries failed (status {status}), using cache")
                if ccy in cache:
                    results[ccy] = cache[ccy]
                    cached_count += 1
                time.sleep(sleep_between)
                continue
            debug_path = CACHE_DIR / f"gdp_debug_{ccy}.html"
            parsed = parse_latest_release(html)
            if not parsed or parsed.get("actual") is None or parsed.get("previous") is None:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                debug_path.write_text(html, encoding="utf-8")
                print(f"[gdp] {ccy} parse failed/incomplete, raw HTML saved to {debug_path.name}, using cache")
                if ccy in cache:
                    results[ccy] = cache[ccy]
                    cached_count += 1
                time.sleep(sleep_between)
                continue
            results[ccy] = parsed
            cache[ccy] = parsed
            fresh_count += 1
            _LAST_FRESH.add(ccy)
            print(f"[gdp] {ccy} {parsed}")
        except Exception as e:
            print(f"[gdp] {ccy} error: {e}, using cache")
            if ccy in cache:
                results[ccy] = cache[ccy]
                cached_count += 1
        time.sleep(sleep_between)

    _save_cache(cache)
    print(f"[gdp] {fresh_count} fresh, {cached_count} from cache, {len(results)}/{len(GDP_URLS)} total")
    return results


def load_cached():
    return _load_cache()


if __name__ == "__main__":
    print(f"curl_cffi installed: {HAS_CFFI}")
    fetch_gdp()
