"""
Trading Economics scraper.

Pulls Actual / TEForecast / Previous / Consensus from TE's public indicator
pages (e.g., tradingeconomics.com/japan/manufacturing-pmi). EdgeFinder uses
TEForecast specifically, which is TE's proprietary forecast and often
differs from the analyst consensus that ForexFactory shows. Using TEForecast
brings our surprise scoring into closer alignment with EF.

Strategy:
- Visit TE homepage to get Cloudflare cookies
- Hit each (country, indicator) page individually
- Parse the HTML table for Calendar / Actual / TEForecast / Previous
- Compute surprise = (Actual - TEForecast) / |TEForecast|
- Save as te_history.json (same shape as ff_history.json)
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except ImportError:
    import requests as cffi_requests  # type: ignore
    HAS_CFFI = False

from bs4 import BeautifulSoup

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"
HISTORY_FILE = CACHE_DIR / "te_history.json"

TE_BASE = "https://tradingeconomics.com"


# Country -> URL slug
TE_COUNTRY_SLUGS = {
    "USD": "united-states",
    "EUR": "euro-area",
    "GBP": "united-kingdom",
    "JPY": "japan",
    "CHF": "switzerland",
    "AUD": "australia",
    "CAD": "canada",
    "NZD": "new-zealand",
}

# Indicator id -> URL slug.
TE_INDICATOR_SLUGS = {
    "gdp": "gdp-growth",
    "cpi": "inflation-cpi",
    "ppi": "producer-prices-change",
    "pce": "core-inflation-rate",
    "mpmi": "manufacturing-pmi",
    "spmi": "services-pmi",
    "retail_sales": "retail-sales",
    "unemployment_rate": "unemployment-rate",
    "consumer_conf": "consumer-confidence",
    "rates": "interest-rate",
    "employment": "employment-change",
    "jobless_claims": "initial-jobless-claims",
}


@dataclass
class TERelease:
    country: str
    indicator_id: str
    date: str
    actual: float | None
    forecast: float | None  # TEForecast
    previous: float | None
    consensus: float | None
    surprise: float | None
    impact: str
    source: str


def build_url(country: str, indicator: str) -> str | None:
    cslug = TE_COUNTRY_SLUGS.get(country)
    islug = TE_INDICATOR_SLUGS.get(indicator)
    if not cslug or not islug:
        return None
    return f"{TE_BASE}/{cslug}/{islug}"


def _parse_value(text: str) -> float | None:
    if not text or not text.strip():
        return None
    t = text.strip().replace(",", "").replace(" ", " ").strip()
    multiplier = 1.0
    if t.endswith("K"):
        multiplier = 1_000
        t = t[:-1]
    elif t.endswith("M"):
        multiplier = 1_000_000
        t = t[:-1]
    elif t.endswith("B"):
        multiplier = 1_000_000_000
        t = t[:-1]
    if t.endswith("%"):
        t = t[:-1]
    try:
        return float(t) * multiplier
    except ValueError:
        return None


def _get_session(target_url: str, timeout: int = 25):
    """Visit TE homepage to get cookies, then fetch target URL."""
    profiles = ["chrome120", "chrome116", "safari17_2"]
    for profile in profiles:
        try:
            if HAS_CFFI:
                session = cffi_requests.Session()
                home = session.get(TE_BASE, impersonate=profile, timeout=timeout)
                if home.status_code != 200:
                    continue
                time.sleep(0.8)
                r = session.get(target_url, impersonate=profile, timeout=timeout)
                if r.status_code == 200:
                    return r.text
            else:
                headers = {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Referer": "https://tradingeconomics.com/",
                }
                session = cffi_requests.Session()
                session.headers.update(headers)
                home = session.get(TE_BASE, timeout=timeout)
                if home.status_code != 200:
                    continue
                time.sleep(0.8)
                r = session.get(target_url, timeout=timeout)
                if r.status_code == 200:
                    return r.text
        except Exception:
            continue
    return None


def _parse_te_table(html: str, country: str, indicator: str, debug: bool = False) -> list[TERelease]:
    """
    Find the calendar table (one with "Actual" and "TEForecast" headers).
    Map columns by header text, extract release rows.
    """
    soup = BeautifulSoup(html, "html.parser")
    target_table = None

    for table in soup.find_all("table"):
        headers = [h.get_text(strip=True).lower() for h in table.find_all("th")]
        if "actual" in headers and ("teforecast" in headers or "te-forecast" in headers or "forecast" in headers):
            target_table = table
            break

    if not target_table:
        return []

    headers = [h.get_text(strip=True).lower() for h in target_table.find_all("th")]
    def col(name):
        # Try exact match first, then substring (TE sometimes mashes header
        # text together, e.g., 'calendargmtreference' for the first column).
        for i, h in enumerate(headers):
            if h == name:
                return i
        for i, h in enumerate(headers):
            if name in h:
                return i
        return -1

    idx_calendar = col("calendar")
    idx_actual = col("actual")
    idx_previous = col("previous")
    idx_consensus = col("consensus")
    idx_te = col("teforecast")
    if idx_te < 0:
        idx_te = col("forecast")

    releases = []
    rows = target_table.find_all("tr")
    if debug:
        print(f"[te DEBUG] table has {len(rows)} <tr> rows total")
        print(f"[te DEBUG] (anchoring value columns from END of row, since header count != cell count)")

    for ri, row in enumerate(rows[1:], 1):
        cells = row.find_all("td")
        if not cells:
            if debug and ri <= 3:
                print(f"[te DEBUG] row {ri}: no <td> cells")
            continue
        # TE rows have layout: [date, time, type, reference?, actual, previous, consensus, teforecast]
        # The last 4 cells are always the value columns regardless of column count.
        if len(cells) < 5:
            if debug and ri <= 3:
                print(f"[te DEBUG] row {ri}: only {len(cells)} cells, need at least 5")
            continue

        date_text = cells[0].get_text(strip=True)
        date_str = _normalize_date(date_text)
        if debug and ri <= 5:
            print(f"[te DEBUG] row {ri}: date='{date_text}' -> '{date_str}'  cells={[c.get_text(strip=True) for c in cells]}")
        if not date_str:
            continue

        # Anchor from end
        teforecast_text = cells[-1].get_text(strip=True)
        consensus_text = cells[-2].get_text(strip=True)
        previous_text = cells[-3].get_text(strip=True)
        actual_text = cells[-4].get_text(strip=True)

        actual = _parse_value(actual_text)
        forecast = _parse_value(teforecast_text)
        previous = _parse_value(previous_text)
        consensus = _parse_value(consensus_text)

        if actual is None:
            continue

        surprise = None
        if forecast is not None:
            denom = abs(forecast) if abs(forecast) > 1e-9 else max(abs(actual), 1.0)
            surprise = (actual - forecast) / denom

        releases.append(TERelease(
            country=country,
            indicator_id=indicator,
            date=date_str,
            actual=actual,
            forecast=forecast,
            previous=previous,
            consensus=consensus,
            surprise=surprise,
            impact="high",  # TE doesn't tag impact; assume high since it's standard releases
            source="te",
        ))

    return releases


def _normalize_date(text: str) -> str | None:
    """Convert TE date strings to YYYY-MM-DD."""
    text = text.strip()
    if not text:
        return None
    # YYYY-MM-DD
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return m.group(0)
    # Mmm DD YYYY (e.g., "Feb 15 2026")
    m = re.match(r"^([A-Za-z]+)\s+(\d{1,2})\s+(\d{4})", text)
    if m:
        try:
            d = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%b %d %Y")
            return d.strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


def fetch_indicator(country: str, indicator: str, debug: bool = False) -> list[TERelease]:
    url = build_url(country, indicator)
    if not url:
        if debug:
            print(f"[te DEBUG] no slug for {country}/{indicator}")
        return []
    if debug:
        print(f"[te DEBUG] fetching {url}")
    html = _get_session(url)
    if not html:
        if debug:
            print(f"[te DEBUG] all profiles failed (likely 403/Cloudflare)")
        return []
    if debug:
        print(f"[te DEBUG] got {len(html)} bytes of HTML")
        debug_path = CACHE_DIR / f"te_debug_{country}_{indicator}.html"
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        debug_path.write_text(html, encoding="utf-8")
        print(f"[te DEBUG] saved html to {debug_path}")
        soup = BeautifulSoup(html, "html.parser")
        tables = soup.find_all("table")
        print(f"[te DEBUG] found {len(tables)} <table> elements")
        for i, t in enumerate(tables):
            headers = [h.get_text(strip=True) for h in t.find_all("th")]
            print(f"[te DEBUG]   table[{i}] headers: {headers[:10]}")
    return _parse_te_table(html, country, indicator, debug=debug)


def load_history() -> dict[str, list[dict]]:
    if not HISTORY_FILE.exists():
        return {}
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_history(history: dict[str, list[dict]]):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f)


def update_history(new_releases: list[TERelease]) -> dict:
    history = load_history()
    for r in new_releases:
        if not r.date:
            continue
        key = f"{r.country}|{r.indicator_id}"
        bucket = history.get(key, [])
        sig = r.date
        if any(b.get("date") == sig for b in bucket):
            continue
        bucket.append(asdict(r))
        bucket.sort(key=lambda x: x.get("date") or "", reverse=True)
        history[key] = bucket[:36]
    save_history(history)
    return history


def fetch_te_all(only_indicators: list[str] | None = None) -> dict:
    """
    Sweep all (country, indicator) combos. Polite 2s delay between requests.
    only_indicators lets you scope to e.g. ['mpmi', 'spmi', 'ppi', 'pce']
    if you only want to fill specific gaps.
    """
    indicators = only_indicators or list(TE_INDICATOR_SLUGS.keys())
    countries = list(TE_COUNTRY_SLUGS.keys())
    total = len(countries) * len(indicators)
    print(f"[te] Starting TE sweep: {len(countries)} countries x {len(indicators)} indicators = {total} pages")

    success = 0
    for ccy in countries:
        for ind in indicators:
            url = build_url(ccy, ind)
            print(f"[te] {ccy} / {ind}...", end=" ", flush=True)
            try:
                releases = fetch_indicator(ccy, ind)
                if releases:
                    update_history(releases)
                    success += 1
                    print(f"OK ({len(releases)} releases)")
                else:
                    print("EMPTY")
            except Exception as e:
                print(f"ERR ({e})")
            time.sleep(2)

    print(f"\n[te] Sweep complete. {success}/{total} pages had data.")
    history = load_history()
    print(f"[te] History: {sum(len(v) for v in history.values())} releases across {len(history)} pairs")
    return history


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--gaps-only", action="store_true",
                        help="Only fetch indicators FF doesn't cover well: mpmi, spmi, ppi, pce")
    parser.add_argument("--country", type=str, default=None, help="Limit to a single currency code")
    parser.add_argument("--indicator", type=str, default=None, help="Limit to a single indicator id")
    args = parser.parse_args()

    if args.country and args.indicator:
        print(f"[te] Single test: {args.country} / {args.indicator}")
        rel = fetch_indicator(args.country, args.indicator, debug=True)
        print(f"\n[te] parsed {len(rel)} releases")
        for r in rel[:5]:
            print(f"  {r}")
    elif args.gaps_only:
        fetch_te_all(only_indicators=["mpmi", "spmi", "ppi", "pce"])
    else:
        fetch_te_all()

    history = load_history()
    summary: dict[str, set[str]] = {}
    for key in history:
        ccy, ind = key.split("|")
        summary.setdefault(ind, set()).add(ccy)
    print("\nCoverage summary:")
    for ind, ccys in sorted(summary.items()):
        print(f"  {ind:18s} {sorted(ccys)}")
