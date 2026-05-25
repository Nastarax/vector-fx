"""
Investing.com CPI YoY fetcher.

Pulls the "Latest Release" block (Actual / Forecast / Previous) from per-currency
CPI YoY calendar pages. Used as the source of truth for the cpi column.

Scoring logic (handled in score_pair.py, this module just fetches data):
- 6 currencies (USD, EUR, GBP, AUD, NZD, CAD): Actual vs Forecast
- JPY: Actual vs Previous (Investing's Japan National CPI YoY page never
  publishes a forecast)
- CHF: Actual vs Forecast if Forecast is present, else Actual vs Previous
  (TE/Investing sometimes don't publish a forecast for the most recent
  Swiss release until close to the print date)

Cached to data/cache/investing_cpi.json. Cloudflare blocks GH Actions IPs
so this module is expected to be refreshed locally (see
scripts/refresh_investing.py).
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
CACHE_FILE = CACHE_DIR / "investing_cpi.json"


# CPI YoY page URLs per currency.
CPI_URLS: dict[str, str] = {
    "USD": "https://www.investing.com/economic-calendar/cpi-733",
    "CAD": "https://www.investing.com/economic-calendar/cpi-741",
    "EUR": "https://www.investing.com/economic-calendar/european-consumer-price-index-(cpi)-yoy-68",
    "GBP": "https://www.investing.com/economic-calendar/united-kingdom-consumer-price-index-(cpi)-yoy-67",
    "AUD": "https://www.investing.com/economic-calendar/australia-consumer-price-index-(cpi)-yoy-1011",
    "NZD": "https://www.investing.com/economic-calendar/new-zealand-consumer-price-index-(cpi)-yoy-1063",
    "CHF": "https://www.investing.com/economic-calendar/switzerland-consumer-price-index-(cpi)-yoy-956",
    "JPY": "https://www.investing.com/economic-calendar/japan-national-consumer-price-index-(cpi)-yoy-992",
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
    """Warmed Cloudflare session: visit homepage + calendar landing first."""
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
    """
    Find 'Latest Release ... Actual ... Forecast ... Previous ...' block.

    CPI YoY pages render values with a trailing '%' (e.g., '2.4%'). The
    regex now allows optional '%' plus optional 'K/M/B'. Forecast can also
    be a placeholder ('--', '&nbsp;', non-breaking space), in which case we
    return None for that field and scoring falls back to Previous.
    """
    # The value pattern: optional unicode/ASCII minus, digits/commas/dots,
    # optional unit suffix (K/M/B), optional %. Also matches lone '-' / '--'.
    VAL = r"(-|--|[-−]?[\d.,]+\s*[KMB]?\s*%?)"

    soup = BeautifulSoup(html, "html.parser")
    # Normalize non-breaking spaces to regular spaces so the regex \s works
    raw_text_full = soup.get_text(" ", strip=True).replace(" ", " ")
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

    # Fallback: walk up from a 'Latest Release' string and try parent text
    for elem in soup.find_all(string=re.compile(r"Latest Release", re.IGNORECASE)):
        parent = elem.parent
        for _ in range(8):
            if parent is None:
                break
            text = parent.get_text(" ", strip=True).replace(" ", " ")
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


# Tracks which currencies were freshly fetched on the most recent fetch_cpi
# call. Cleared at the start of each call. Used by refresh_investing.py to
# know which currencies actually came back from Investing.com vs fell back
# to cache (results dict merges both, so it can't tell them apart on its own).
_LAST_FRESH: set[str] = set()


def fetch_cpi(sleep_between=4.0):
    """
    Hit all 8 CPI YoY pages, return dict keyed by currency.

    Each value: {"date": "2026-05-01", "actual": 2.3, "forecast": 2.4, "previous": 2.4}
    forecast may be None for JPY (never published) or CHF (not out yet).
    """
    global _LAST_FRESH
    _LAST_FRESH = set()
    cache = _load_cache()
    results = {}
    fresh_count = 0
    cached_count = 0

    for ccy, url in CPI_URLS.items():
        try:
            status, html = _fetch_with_retries(url)
            if status != 200 or not html:
                print(f"[cpi] {ccy} all retries failed (status {status}), using cache")
                if ccy in cache:
                    results[ccy] = cache[ccy]
                    cached_count += 1
                time.sleep(sleep_between)
                continue
            debug_path = CACHE_DIR / f"cpi_debug_{ccy}.html"
            parsed = parse_latest_release(html)
            if not parsed or parsed.get("actual") is None or parsed.get("previous") is None:
                # Save raw HTML so we can inspect what changed if it ever
                # fails again. Only on parse failure so we don't fill cache
                # dir with normal-run HTML.
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                debug_path.write_text(html, encoding="utf-8")
                print(f"[cpi] {ccy} parse failed/incomplete, raw HTML saved to {debug_path.name}, using cache")
                if ccy in cache:
                    results[ccy] = cache[ccy]
                    cached_count += 1
                time.sleep(sleep_between)
                continue
            results[ccy] = parsed
            cache[ccy] = parsed
            fresh_count += 1
            _LAST_FRESH.add(ccy)
            print(f"[cpi] {ccy} {parsed}")
        except Exception as e:
            print(f"[cpi] {ccy} error: {e}, using cache")
            if ccy in cache:
                results[ccy] = cache[ccy]
                cached_count += 1
        time.sleep(sleep_between)

    _save_cache(cache)
    print(f"[cpi] {fresh_count} fresh, {cached_count} from cache, {len(results)}/{len(CPI_URLS)} total")
    return results


def load_cached():
    return _load_cache()


# ---------------------------------------------------------------------------
# Japan Tokyo Core CPI YoY (event id 328). Used as the JPY CPI source for both
# scoring (Actual vs Forecast/consensus) and the inflation page history chart.
# The full monthly series (~100 releases back to 2018) is embedded in the
# page's Next.js __NEXT_DATA__ blob, so one page fetch gives latest + history.
# ---------------------------------------------------------------------------
TOKYO_CORE_CPI_URL = (
    "https://www.investing.com/economic-calendar/"
    "japan-tokyo-core-consumer-price-index-(cpi)-yoy-328"
)
TOKYO_CORE_CACHE = CACHE_DIR / "tokyo_core_cpi.json"

_MONTH_ABBR = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _occ_ref_date(occ: dict) -> str | None:
    """Reference-month date (YYYY-MM-01) for one occurrence, using its
    reference_period plus the release year. Tokyo CPI is a same-month release,
    but guard the Dec-data-released-in-Jan edge by rolling the year back."""
    t = occ.get("occurrence_time") or ""
    if len(t) < 7:
        return None
    rel_y, rel_m = int(t[:4]), int(t[5:7])
    ref = (occ.get("reference_period") or "").strip().lower()[:3]
    mo = _MONTH_ABBR.get(ref)
    if mo is None:
        return f"{rel_y:04d}-{rel_m:02d}-01"
    year = rel_y - 1 if mo > rel_m else rel_y
    return f"{year:04d}-{mo:02d}-01"


def fetch_tokyo_core_cpi() -> dict | None:
    """
    Fetch Japan Tokyo Core CPI YoY from Investing.com.

    Returns a dict that drops into the same scoring path as the per-currency
    CPI cache (Actual vs Forecast), plus a deep history list for the chart:
      {date, actual, forecast, consensus, previous, ref_month,
       history:[{date:'YYYY-MM-01', value:float}, ...ascending]}
    Falls back to the local cache on failure. Returns None if nothing usable.
    """
    status, html = _fetch_with_retries(TOKYO_CORE_CPI_URL)
    if status != 200 or not html:
        print(f"[cpi] Tokyo Core CPI fetch failed (status {status}), using cache")
        return load_tokyo_core_cpi()
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        print("[cpi] Tokyo Core CPI: __NEXT_DATA__ not found, using cache")
        return load_tokyo_core_cpi()
    try:
        data = json.loads(m.group(1))
        occ = data["props"]["pageProps"]["state"]["economicCalendarEventStore"]["occurrences"]
    except Exception as e:
        print(f"[cpi] Tokyo Core CPI: __NEXT_DATA__ parse failed ({e}), using cache")
        return load_tokyo_core_cpi()

    released = [o for o in occ if o.get("actual") is not None]
    released.sort(key=lambda o: o.get("occurrence_time") or "")
    history = []
    for o in released:
        d = _occ_ref_date(o)
        if d:
            history.append({"date": d, "value": o["actual"]})
    if not history:
        print("[cpi] Tokyo Core CPI: no released occurrences, using cache")
        return load_tokyo_core_cpi()

    latest = released[-1]
    result = {
        "date": (latest.get("occurrence_time") or "")[:10],
        "actual": latest.get("actual"),
        "forecast": latest.get("forecast"),     # consensus -> scoring benchmark
        "consensus": latest.get("forecast"),
        "previous": latest.get("previous"),
        "ref_month": latest.get("reference_period"),
        "history": history,
    }
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(TOKYO_CORE_CACHE, "w") as f:
            json.dump(result, f, indent=2)
    except Exception:
        pass
    print(f"[cpi] Tokyo Core CPI: {result['ref_month']} actual={result['actual']} "
          f"forecast={result['forecast']} prev={result['previous']} "
          f"({result['date']}); {len(history)} history pts {history[0]['date']}..{history[-1]['date']}")
    return result


def load_tokyo_core_cpi() -> dict | None:
    if not TOKYO_CORE_CACHE.exists():
        return None
    try:
        with open(TOKYO_CORE_CACHE) as f:
            return json.load(f)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Deep CPI YoY history for ALL 8 currencies, from each Investing page's
# __NEXT_DATA__ occurrences. Used to draw the inflation line chart with
# continuous, current monthly data (no FRED publication lag, no interpolated
# straight-line bridges at the right edge). JPY uses the Tokyo Core page.
# Cloudflare blocks GH Actions, so this is refreshed locally (refresh_investing)
# and read from cache by main.py; the persistent archive keeps it long-term.
# ---------------------------------------------------------------------------
CPI_HISTORY_CACHE = CACHE_DIR / "cpi_investing_history.json"


def _parse_cpi_occurrences(html: str) -> list[dict]:
    """Ascending [{date:'YYYY-MM-01', value:float}] from a CPI page's
    __NEXT_DATA__ occurrences (released rows only)."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
        occ = data["props"]["pageProps"]["state"]["economicCalendarEventStore"]["occurrences"]
    except Exception:
        return []
    released = [o for o in occ if o.get("actual") is not None]
    released.sort(key=lambda o: o.get("occurrence_time") or "")
    out = []
    for o in released:
        d = _occ_ref_date(o)
        if d:
            pt = {"date": d, "value": o["actual"]}
            if o.get("forecast") is not None:
                pt["forecast"] = o["forecast"]
            out.append(pt)
    return out


def fetch_all_cpi_history(sleep_between: float = 4.0) -> dict:
    """Fetch deep monthly CPI YoY history for all 8 currencies from Investing.
    Returns {ccy: [{date,value}] ascending}. Caches the combined result."""
    urls = dict(CPI_URLS)
    urls["JPY"] = TOKYO_CORE_CPI_URL  # Tokyo Core for JPY (matches scoring)
    out: dict[str, list[dict]] = {}
    for ccy, url in urls.items():
        status, html = _fetch_with_retries(url)
        if status == 200 and html:
            pts = _parse_cpi_occurrences(html)
            if pts:
                out[ccy] = pts
                print(f"[cpi-hist] {ccy}: {len(pts)} pts {pts[0]['date']}..{pts[-1]['date']}")
            else:
                print(f"[cpi-hist] {ccy}: no occurrences parsed")
        else:
            print(f"[cpi-hist] {ccy}: fetch failed (status {status})")
        time.sleep(sleep_between)
    if out:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with open(CPI_HISTORY_CACHE, "w") as f:
                json.dump(out, f)
        except Exception:
            pass
    return out


def load_cpi_full_history() -> dict:
    if not CPI_HISTORY_CACHE.exists():
        return {}
    try:
        with open(CPI_HISTORY_CACHE) as f:
            return json.load(f)
    except Exception:
        return {}


if __name__ == "__main__":
    print(f"curl_cffi installed: {HAS_CFFI}")
    data = fetch_cpi()
    print("\nSummary (Actual vs Forecast where present, else vs Previous):")
    for ccy, rel in sorted(data.items()):
        a = rel.get("actual")
        f = rel.get("forecast")
        p = rel.get("previous")
        benchmark = f if f is not None else p
        bench_label = "Forecast" if f is not None else "Previous"
        if a is None or benchmark is None:
            direction = "?"
        elif a > benchmark:
            direction = "+1 (bullish)"
        elif a < benchmark:
            direction = "-1 (bearish)"
        else:
            direction = "0 (flat)"
        print(f"  {ccy}: actual={a}  {bench_label}={benchmark}  -> {direction}")
