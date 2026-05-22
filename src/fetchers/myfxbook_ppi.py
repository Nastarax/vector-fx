"""
Myfxbook Switzerland Producer & Import Prices YoY fetcher (CHF only).

Source of truth for the CHF ppi column. Scoring is Actual vs Consensus, fall
back to Previous if consensus is missing (handled in score_pair.py /
build_economic_heatmap.py). The other currencies keep their existing PPI
sources (NZD via Investing, the rest via TradingEconomics).

Myfxbook is Cloudflare-protected and needs Chrome TLS impersonation
(curl_cffi), exactly like the Myfxbook crowd-sentiment / sPMI scrapers. GitHub
Actions IPs get blocked, so this is refreshed locally (see
scripts/refresh_investing.py) and read from cache by main.py.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except ImportError:
    import requests as cffi_requests  # type: ignore
    HAS_CFFI = False

from bs4 import BeautifulSoup

# Reuse the proven Investing number parser (handles %, K/M/B, unicode minus).
from src.fetchers.investing import _parse_num

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"
CACHE_FILE = CACHE_DIR / "myfxbook_ppi.json"


# Myfxbook Switzerland Producer & Import Prices YoY (the Swiss PPI). CHF only.
CHF_PPI_URLS: dict[str, str] = {
    "CHF": "https://www.myfxbook.com/forex-economic-calendar/switzerland/producer-import-prices-yoy",
}


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _fetch_myfxbook(url: str, max_attempts: int = 3) -> str | None:
    """Cloudflare-protected; needs Chrome TLS impersonation via curl_cffi."""
    profiles = ["chrome120", "chrome124", "safari17_2"]
    for attempt in range(max_attempts):
        profile = profiles[attempt % len(profiles)]
        try:
            if HAS_CFFI:
                r = cffi_requests.get(url, impersonate=profile, timeout=20)
            else:
                r = cffi_requests.get(url, headers=_HEADERS, timeout=20)
            if r.status_code == 200:
                return r.text
        except Exception:
            pass
        time.sleep(2 ** (attempt + 1))
    return None


def _parse_date(raw: str | None) -> str | None:
    """Robust date parse for Myfxbook calendar cells. Handles 'Apr 30, 2026',
    '30 Apr 2026', '2026-04-30', with or without a trailing time."""
    if not raw:
        return None
    t = raw.strip()
    m = re.search(r"([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})", t)  # Apr 30, 2026
    if m:
        chunk = m.group(1).replace(",", "")
        for fmt in ("%b %d %Y", "%B %d %Y"):
            try:
                return datetime.strptime(chunk, fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass
    m = re.search(r"(\d{4}-\d{2}-\d{2})", t)  # 2026-04-30
    if m:
        return m.group(1)
    m = re.search(r"(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})", t)  # 30 Apr 2026
    if m:
        for fmt in ("%d %b %Y", "%d %B %Y"):
            try:
                return datetime.strptime(m.group(1), fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass
    return None


def _num_from_label(scope, label: str):
    """Find a '<label>:' element inside `scope` and return the numeric value
    that follows it on the same release line. Myfxbook renders the latest
    release as labeled lines like '<span>Previous:</span><span>-2.7%</span>'."""
    pat = re.compile(r"^\s*" + re.escape(label) + r"\s*:?\s*$", re.I)
    for el in scope.find_all(["span", "td", "b", "div"]):
        if pat.match(el.get_text(strip=True)):
            line = el.find_parent(["div", "tr", "li"]) or el.parent
            txt = line.get_text(" ", strip=True)
            m = re.search(re.escape(label) + r"\s*:?\s*(.+)", txt, re.I)
            cand = (m.group(1) if m else "").strip()
            # Drop any trailing tooltip like "(+0.6%)" and inner whitespace.
            cand = re.sub(r"\s+", "", cand.split("(")[0])
            val = _parse_num(cand)
            if val is not None:
                return val
    return None


def _next_release_date(html: str) -> str | None:
    """Pull the upcoming release date from the add-to-calendar links."""
    m = re.search(r"startdt=(\d{4}-\d{2}-\d{2})", html)
    if m:
        return m.group(1)
    m = re.search(r"dates=(\d{8})T", html)
    if m:
        s = m.group(1)
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return None


def _parse_release_block(html: str) -> dict | None:
    """Parse Myfxbook's 'Latest Release' block (labeled spans, not a table).

    Returns {"date", "actual", "consensus", "forecast"(=consensus), "previous"}
    or None. The block doesn't carry the release date, so we estimate it from
    the next scheduled release (monthly indicator => ~30 days earlier); the
    exact day only matters for the staleness banner / backtest filtering.
    """
    soup = BeautifulSoup(html, "html.parser")
    block = None
    lr = soup.find(string=re.compile(r"Latest\s+Release", re.I))
    if lr:
        block = lr.find_parent("div", class_="calendar-info-release-block")
    if block is None:
        block = soup  # fall back to whole doc; labels are unique enough

    actual = _num_from_label(block, "Actual")
    if actual is None:
        return None
    consensus = _num_from_label(block, "Consensus")
    previous = _num_from_label(block, "Previous")

    date_str = ""
    nxt = _next_release_date(html)
    if nxt:
        try:
            date_str = (datetime.strptime(nxt, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
        except ValueError:
            pass

    return {
        "date": date_str,
        "actual": actual,
        "consensus": consensus,
        "forecast": consensus,  # alias so generic readers still work
        "previous": previous,
    }


def _parse_table(html: str) -> dict | None:
    """Legacy fallback: some Myfxbook layouts use a Date|Actual|Consensus|
    Previous table. Returns the most recent row with an Actual value."""
    soup = BeautifulSoup(html, "html.parser")
    target = None
    for table in soup.find_all("table"):
        htext = " ".join(th.get_text(strip=True).lower() for th in table.find_all("th"))
        if "actual" in htext and "previous" in htext and ("consensus" in htext or "forecast" in htext):
            target = table
            break
    if not target:
        return None
    headers = [th.get_text(strip=True).lower() for th in target.find_all("th")]

    def col(*labels) -> int:
        for lab in labels:
            for i, h in enumerate(headers):
                if lab in h:
                    return i
        return -1

    i_date = col("date")
    i_actual = col("actual")
    i_bench = col("consensus", "forecast")
    i_prev = col("previous")
    if i_actual < 0:
        return None
    body = target.find("tbody") or target
    best = None
    for row in body.find_all("tr"):
        cells = row.find_all("td")
        if not cells or i_actual >= len(cells):
            continue
        actual = _parse_num(cells[i_actual].get_text(strip=True))
        if actual is None:
            continue
        consensus = _parse_num(cells[i_bench].get_text(strip=True)) if 0 <= i_bench < len(cells) else None
        previous = _parse_num(cells[i_prev].get_text(strip=True)) if 0 <= i_prev < len(cells) else None
        date_str = _parse_date(cells[i_date].get_text(" ", strip=True)) if 0 <= i_date < len(cells) else None
        rec = {"date": date_str or "", "actual": actual, "consensus": consensus,
               "forecast": consensus, "previous": previous}
        if best is None:
            best = rec
        elif date_str and (not best.get("date") or date_str > best["date"]):
            best = rec
    return best


def parse_calendar(html: str, debug_path=None) -> dict | None:
    """
    Parse the latest release from a Myfxbook economic-calendar event page.
    Primary layout is the 'Latest Release' block of labeled spans; falls back
    to a table layout if present. Myfxbook labels the forecast "Consensus"
    (some pages use "Forecast"); both are accepted.

    Returns {"date", "actual", "consensus", "forecast"(=consensus), "previous"}
    or None.
    """
    rec = _parse_release_block(html)
    if rec is None:
        rec = _parse_table(html)
    if rec is None and debug_path:
        Path(debug_path).write_text(html[:8000], encoding="utf-8")
    return rec


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


# Tracks which currencies were freshly fetched on the most recent fetch_ppi
# call. Cleared at start of each call. Read by refresh_investing.py.
_LAST_FRESH: set[str] = set()


def fetch_ppi(sleep_between=4.0):
    """Hit the Myfxbook Swiss PPI page and return dict keyed by currency.

    Value: {"date": "2026-04-30", "actual": -0.1, "consensus": 0.0,
    "forecast": 0.0, "previous": -0.2}. consensus may be None if Myfxbook
    hasn't posted one (scoring then falls back to Previous).
    """
    global _LAST_FRESH
    _LAST_FRESH = set()
    cache = _load_cache()
    results = {}
    fresh_count = 0
    cached_count = 0

    for ccy, url in CHF_PPI_URLS.items():
        try:
            html = _fetch_myfxbook(url)
            if not html:
                print(f"[chf-ppi] {ccy} fetch failed (Cloudflare / no curl_cffi?), using cache")
                if ccy in cache:
                    results[ccy] = cache[ccy]
                    cached_count += 1
                time.sleep(sleep_between)
                continue
            debug_path = CACHE_DIR / f"chf_ppi_debug_{ccy}.html"
            parsed = parse_calendar(html, debug_path=debug_path)
            if not parsed or parsed.get("actual") is None:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                debug_path.write_text(html, encoding="utf-8")
                print(f"[chf-ppi] {ccy} parse failed/incomplete, raw HTML saved to {debug_path.name}, using cache")
                if ccy in cache:
                    results[ccy] = cache[ccy]
                    cached_count += 1
                time.sleep(sleep_between)
                continue
            results[ccy] = parsed
            cache[ccy] = parsed
            fresh_count += 1
            _LAST_FRESH.add(ccy)
            print(f"[chf-ppi] {ccy} {parsed}")
        except Exception as e:
            print(f"[chf-ppi] {ccy} error: {e}, using cache")
            if ccy in cache:
                results[ccy] = cache[ccy]
                cached_count += 1
        time.sleep(sleep_between)

    _save_cache(cache)
    print(f"[chf-ppi] {fresh_count} fresh, {cached_count} from cache, {len(results)}/{len(CHF_PPI_URLS)} total")
    return results


def load_cached():
    return _load_cache()


if __name__ == "__main__":
    print(f"curl_cffi installed: {HAS_CFFI}")
    data = fetch_ppi()
    print("\nSummary (Actual vs Consensus):")
    for ccy, rel in sorted(data.items()):
        a = rel.get("actual")
        c = rel.get("consensus")
        p = rel.get("previous")
        benchmark = c if c is not None else p
        bench_label = "Consensus" if c is not None else "Previous"
        if a is None or benchmark is None:
            direction = "?"
        elif a > benchmark:
            direction = "+1 (bullish)"
        elif a < benchmark:
            direction = "-1 (bearish)"
        else:
            direction = "0 (flat)"
        print(f"  {ccy}: actual={a}  {bench_label}={benchmark}  -> {direction}")
