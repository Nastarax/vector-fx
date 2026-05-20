"""
Trading Economics scraper.

Pulls Actual / TEForecast / Previous / Consensus from TE's public indicator
pages. EdgeFinder uses TEForecast specifically, which is TE's proprietary
forecast and often differs from the analyst consensus that ForexFactory shows.
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
RATES_OUTLOOK_FILE = CACHE_DIR / "te_rates_outlook.json"

TE_BASE = "https://tradingeconomics.com"


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

TE_INDICATOR_SLUGS = {
    "gdp": "gdp-growth",
    "cpi": "inflation-cpi",
    "ppi": "producer-prices-change",
    "pce": "pce-price-index-annual-change",
    "mpmi": "manufacturing-pmi",
    "spmi": "services-pmi",
    "retail_sales": "retail-sales",
    "unemployment_rate": "unemployment-rate",
    "consumer_conf": "consumer-confidence",
    "rates": "interest-rate",
    "nfp": "non-farm-payrolls",
    "jobless_claims": "jobless-claims",
    "adp": "adp-employment-change",
    "jolts": "job-offers",
}

TE_INDICATOR_SLUG_OVERRIDES = {
    ("JPY", "retail_sales"): "retail-sales-annual",
    ("CHF", "retail_sales"): "retail-sales-annual",
    # UK PPI uses ppi-input-yoy instead of the standard producer-prices-change
    ("GBP", "ppi"): "ppi-input-yoy",
}


@dataclass
class TERelease:
    country: str
    indicator_id: str
    date: str
    actual: float | None
    forecast: float | None
    previous: float | None
    consensus: float | None
    surprise: float | None
    impact: str
    source: str


def build_url(country: str, indicator: str) -> str | None:
    cslug = TE_COUNTRY_SLUGS.get(country)
    islug = TE_INDICATOR_SLUG_OVERRIDES.get((country, indicator))
    if islug is None:
        islug = TE_INDICATOR_SLUGS.get(indicator)
    if not cslug or not islug:
        return None
    return f"{TE_BASE}/{cslug}/{islug}"


def _parse_value(text):
    if not text or not text.strip():
        return None
    t = text.strip().replace(",", "").replace(" ", " ").strip()
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


def _get_session(target_url, timeout=25):
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
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
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


def _parse_te_table(html, country, indicator, debug=False):
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

    releases = []
    rows = target_table.find_all("tr")
    for ri, row in enumerate(rows[1:], 1):
        cells = row.find_all("td")
        if not cells or len(cells) < 5:
            continue
        date_text = cells[0].get_text(strip=True)
        date_str = _normalize_date(date_text)
        if not date_str:
            continue
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
            country=country, indicator_id=indicator, date=date_str,
            actual=actual, forecast=forecast, previous=previous,
            consensus=consensus, surprise=surprise, impact="high", source="te",
        ))
    return releases


def _normalize_date(text):
    text = text.strip()
    if not text:
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return m.group(0)
    m = re.match(r"^([A-Za-z]+)\s+(\d{1,2})\s+(\d{4})", text)
    if m:
        try:
            d = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%b %d %Y")
            return d.strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


def _parse_te_stats_fallback(html, country, indicator, debug=False):
    """
    Fallback parser for pages with no calendar+forecast table.
    Pulls Actual + Previous from the stats table; date from Related Indicators.
    Used for CAD Consumer Confidence (no forecast published).
    """
    soup = BeautifulSoup(html, "html.parser")
    stats_table = None
    for table in soup.find_all("table"):
        headers = [h.get_text(strip=True).lower() for h in table.find_all("th")]
        if "actual" in headers and "previous" in headers and "highest" in headers and "lowest" in headers:
            stats_table = table
            break
    if not stats_table:
        return []

    rows = stats_table.find_all("tr")
    data_row = None
    for r in rows[1:]:
        cells = [c.get_text(strip=True) for c in r.find_all("td")]
        if cells and len(cells) >= 4:
            data_row = cells
            break
    if not data_row:
        return []

    headers = [h.get_text(strip=True).lower() for h in stats_table.find_all("th")]
    def col(name):
        for i, h in enumerate(headers):
            if h == name:
                return i
        return -1
    idx_actual = col("actual")
    idx_previous = col("previous")
    if idx_actual < 0 or idx_previous < 0:
        return []
    if idx_actual >= len(data_row) or idx_previous >= len(data_row):
        return []
    actual = _parse_value(data_row[idx_actual])
    previous = _parse_value(data_row[idx_previous])
    if actual is None or previous is None:
        return []

    date_str = None
    for table in soup.find_all("table"):
        headers2 = [h.get_text(strip=True).lower() for h in table.find_all("th")]
        if "related" in headers2 and "reference" in headers2:
            ref_idx = headers2.index("reference")
            for row in table.find_all("tr")[1:]:
                cells = [c.get_text(strip=True) for c in row.find_all("td")]
                if not cells or ref_idx >= len(cells):
                    continue
                first = cells[0].lower()
                indicator_label = indicator.replace("_", " ")
                if indicator_label in first or (indicator == "consumer_conf" and "consumer confidence" in first):
                    m = re.match(r"^([A-Za-z]+)\s+(\d{4})", cells[ref_idx])
                    if m:
                        try:
                            d = datetime.strptime(f"{m.group(1)} 1 {m.group(2)}", "%b %d %Y")
                            date_str = d.strftime("%Y-%m-%d")
                        except ValueError:
                            pass
                    break
            if date_str:
                break

    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if debug:
        print(f"[te DEBUG] stats fallback: actual={actual} previous={previous} date={date_str}")

    return [TERelease(
        country=country, indicator_id=indicator, date=date_str,
        actual=actual, forecast=None, previous=previous, consensus=None,
        surprise=None, impact="high", source="te",
    )]


def fetch_indicator(country, indicator, debug=False):
    url = build_url(country, indicator)
    if not url:
        return []
    html = _get_session(url)
    if not html:
        return []
    if debug:
        debug_path = CACHE_DIR / f"te_debug_{country}_{indicator}.html"
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        debug_path.write_text(html, encoding="utf-8")
        print(f"[te DEBUG] saved html to {debug_path}")
    releases = _parse_te_table(html, country, indicator, debug=debug)
    if not releases:
        if debug:
            print(f"[te DEBUG] calendar table empty, trying stats fallback...")
        releases = _parse_te_stats_fallback(html, country, indicator, debug=debug)
    return releases


def load_history():
    if not HISTORY_FILE.exists():
        return {}
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_history(history):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f)


def update_history(new_releases):
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


TOKYO_CORE_CPI_URL = "https://tradingeconomics.com/japan/tokyo-core-cpi"
TOKYO_CORE_CPI_CACHE = CACHE_DIR / "tokyo_core_cpi.json"


def _parse_tokyo_core_table(html: str) -> list[dict]:
    """
    Parse the Tokyo Core CPI calendar table. Column layout (per data row):
      [release_date, time, event, ref_month, Actual, Previous, Consensus, TEForecast]

    Returns rows (newest first by release date) as dicts:
      {date, ref_month, actual, previous, consensus, teforecast}
    Only rows with a non-empty Actual are kept (drops the not-yet-released row).
    """
    soup = BeautifulSoup(html, "html.parser")
    target = None
    for table in soup.find_all("table"):
        heads = " ".join(th.get_text(strip=True).lower() for th in table.find_all("th"))
        if "actual" in heads and ("consensus" in heads or "forecast" in heads):
            target = table
            break
    if not target:
        return []

    body = target.find("tbody") or target
    rows = []
    for tr in body.find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) < 8:
            continue
        actual = _parse_value(cells[4])
        if actual is None:  # skip the future / unreleased row
            continue
        rows.append({
            "date": cells[0],
            "ref_month": cells[3],
            "actual": actual,
            "previous": _parse_value(cells[5]),
            "consensus": _parse_value(cells[6]),
            "teforecast": _parse_value(cells[7]),
        })
    rows.sort(key=lambda r: r.get("date") or "", reverse=True)
    return rows


def fetch_tokyo_core_cpi():
    """
    Fetch Japan Tokyo Core CPI YoY from TE. Used as the JPY CPI source for
    scoring (Actual vs Consensus, TEForecast fallback) and as JPY's series for
    the inflation page.

    Returns a dict shaped like the Investing CPI cache so it can drop into the
    same scoring path:
      {"date","actual","forecast","previous","consensus","recent"}
    where `forecast` = consensus (so existing Actual-vs-Forecast scoring uses
    consensus as the benchmark) and `recent` is the parsed table (newest first)
    for history accumulation. Returns None on failure.
    """
    html = _get_session(TOKYO_CORE_CPI_URL)
    if not html:
        print("[te] Tokyo Core CPI fetch failed")
        return None
    rows = _parse_tokyo_core_table(html)
    if not rows:
        print("[te] Tokyo Core CPI: no released rows parsed")
        return None
    latest = rows[0]
    consensus = latest.get("consensus")
    teforecast = latest.get("teforecast")
    benchmark = consensus if consensus is not None else teforecast
    result = {
        "date": latest["date"],
        "actual": latest["actual"],
        "forecast": benchmark,       # consensus (or TEForecast) for scoring
        "consensus": consensus,
        "previous": latest.get("previous"),
        "ref_month": latest.get("ref_month"),
        "recent": rows,              # full parsed table for history merge
    }
    # Persist a small cache so we have the latest even if a later fetch fails.
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(TOKYO_CORE_CPI_CACHE, "w") as f:
            json.dump(result, f, indent=2)
    except Exception:
        pass
    print(f"[te] Tokyo Core CPI: {latest['ref_month']} actual={latest['actual']} "
          f"consensus={consensus} previous={latest.get('previous')} ({latest['date']})")
    return result


def load_tokyo_core_cpi():
    """Read-only access to the last cached Tokyo Core CPI result."""
    if not TOKYO_CORE_CPI_CACHE.exists():
        return None
    try:
        with open(TOKYO_CORE_CPI_CACHE) as f:
            return json.load(f)
    except Exception:
        return None


def fetch_gdp_only():
    print("[te] refreshing GDP for all 8 currencies...")
    success = 0
    for ccy in TE_COUNTRY_SLUGS:
        try:
            releases = fetch_indicator(ccy, "gdp")
            if releases:
                releases_sorted = sorted(releases, key=lambda r: r.date, reverse=True)
                update_history(releases_sorted)
                success += 1
                latest = releases_sorted[0]
                a = latest.actual if latest.actual is not None else "n/a"
                c = latest.consensus if latest.consensus is not None else "n/a"
                f = latest.forecast if latest.forecast is not None else "n/a"
                print(f"  {ccy}: Actual={a}  Consensus={c}  TEForecast={f}  ({latest.date})")
            else:
                print(f"  {ccy}: no releases")
        except Exception as e:
            print(f"  {ccy}: failed ({e})")
        time.sleep(2)
    print(f"[te] GDP refresh complete: {success}/{len(TE_COUNTRY_SLUGS)} ok")
    return load_history()


def fetch_retail_sales_only():
    print("[te] refreshing retail sales for 7 currencies (AUD uses ABS MHSI)...")
    countries = [c for c in TE_COUNTRY_SLUGS if c != "AUD"]
    success = 0
    for ccy in countries:
        try:
            releases = fetch_indicator(ccy, "retail_sales")
            if releases:
                releases_sorted = sorted(releases, key=lambda r: r.date, reverse=True)
                update_history(releases_sorted)
                success += 1
                latest = releases_sorted[0]
                a = latest.actual if latest.actual is not None else "n/a"
                c = latest.consensus if latest.consensus is not None else "n/a"
                f = latest.forecast if latest.forecast is not None else "n/a"
                print(f"  {ccy}: Actual={a}  Consensus={c}  TEForecast={f}  ({latest.date})")
            else:
                print(f"  {ccy}: no releases")
        except Exception as e:
            print(f"  {ccy}: failed ({e})")
        time.sleep(2)
    print(f"[te] retail sales refresh complete: {success}/{len(countries)} ok")
    return load_history()


def _parse_te_rates_table(html, country):
    """
    Parse a TE interest-rate calendar table.
    Returns list of dicts: {date, actual, forecast (TEForecast), is_future}
    Captures BOTH past releases (actual is not None) AND upcoming
    releases (actual is None, forecast may or may not be set).
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

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = []
    for row in target_table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if not cells or len(cells) < 5:
            continue
        date_text = cells[0].get_text(strip=True)
        date_str = _normalize_date(date_text)
        if not date_str:
            continue
        teforecast_text = cells[-1].get_text(strip=True)
        actual_text = cells[-4].get_text(strip=True)
        actual = _parse_value(actual_text)
        forecast = _parse_value(teforecast_text)
        out.append({
            "date": date_str,
            "actual": actual,
            "forecast": forecast,
            "is_future": date_str > today,
        })
    return out


def fetch_rates_outlook():
    """
    For each of 8 currencies, scrape TE interest-rate page and extract:
      - Current rate: latest past release with Actual
      - Forecast rate: next upcoming release's TEForecast (if published).
        If no upcoming TEForecast available, falls back to current rate
        (so the score becomes 0 = "no expected change").

    Returns: {ccy: {"date": "...", "current": float, "forecast": float}}
    Cached to te_rates_outlook.json. Refreshed every main.py run.
    """
    print("[te-rates] refreshing rate outlook for all 8 currencies...")
    out: dict[str, dict] = {}
    for ccy in TE_COUNTRY_SLUGS:
        try:
            url = build_url(ccy, "rates")
            if not url:
                continue
            html = _get_session(url)
            if not html:
                print(f"  {ccy}: fetch failed")
                continue
            rows = _parse_te_rates_table(html, ccy)
            if not rows:
                print(f"  {ccy}: no rows parsed")
                continue
            # Latest past release (actual not None) by date desc
            past = [r for r in rows if r["actual"] is not None]
            past.sort(key=lambda x: x["date"], reverse=True)
            current = past[0]["actual"] if past else None
            # Earliest upcoming release with TEForecast
            future = [r for r in rows if r["is_future"]]
            future.sort(key=lambda x: x["date"])
            next_meeting_date = future[0]["date"] if future else None
            forecast = None
            for r in future:
                if r["forecast"] is not None:
                    forecast = r["forecast"]
                    break
            if current is None:
                print(f"  {ccy}: no current rate found")
                continue
            # If no forecast published yet, treat as same as current (score=0)
            if forecast is None:
                forecast = current
                forecast_source = "= current (no TEForecast yet)"
            else:
                forecast_source = "TEForecast"
            out[ccy] = {
                "date": next_meeting_date or "",
                "current": current,
                "forecast": forecast,
            }
            direction = "+1" if forecast > current else ("-1" if forecast < current else "0")
            print(f"  {ccy}: current={current}  forecast={forecast} ({forecast_source})  next={next_meeting_date}  -> {direction}")
        except Exception as e:
            print(f"  {ccy}: failed ({e})")
        time.sleep(2)
    _save_rates_outlook(out)
    print(f"[te-rates] outlook complete: {len(out)}/{len(TE_COUNTRY_SLUGS)} ok")
    return out


def _save_rates_outlook(data):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(RATES_OUTLOOK_FILE, "w") as f:
        json.dump(data, f, indent=2)


def load_rates_outlook():
    if not RATES_OUTLOOK_FILE.exists():
        return {}
    try:
        with open(RATES_OUTLOOK_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def fetch_nfp_only():
    """
    Refresh Non-Farm Payrolls for USD only.

    Scoring spec: Actual vs Consensus (priority), fall back to TEForecast
    if Consensus missing. NFP is a US-only indicator. The TE slug is
    'non-farm-payrolls'.
    """
    print("[te] refreshing NFP (USD only)...")
    try:
        releases = fetch_indicator("USD", "nfp")
        if releases:
            releases_sorted = sorted(releases, key=lambda r: r.date, reverse=True)
            update_history(releases_sorted)
            latest = releases_sorted[0]
            a = latest.actual if latest.actual is not None else "n/a"
            c = latest.consensus if latest.consensus is not None else "n/a"
            f = latest.forecast if latest.forecast is not None else "n/a"
            print(f"  USD: Actual={a}  Consensus={c}  TEForecast={f}  ({latest.date})")
        else:
            print("  USD: no releases")
    except Exception as e:
        print(f"  USD: failed ({e})")
    print("[te] NFP refresh complete")
    return load_history()


def fetch_pce_only():
    """
    Refresh PCE Price Index YoY for USD only.

    Scoring spec: Actual vs Consensus (priority), fall back to TEForecast
    if Consensus missing. PCE is a US-only indicator. The TE slug is
    'pce-price-index-annual-change' which is the YoY % change of the
    headline PCE Price Index (not Core PCE).
    """
    print("[te] refreshing PCE YoY (USD only)...")
    try:
        releases = fetch_indicator("USD", "pce")
        if releases:
            releases_sorted = sorted(releases, key=lambda r: r.date, reverse=True)
            update_history(releases_sorted)
            latest = releases_sorted[0]
            a = latest.actual if latest.actual is not None else "n/a"
            c = latest.consensus if latest.consensus is not None else "n/a"
            f = latest.forecast if latest.forecast is not None else "n/a"
            print(f"  USD: Actual={a}  Consensus={c}  TEForecast={f}  ({latest.date})")
        else:
            print("  USD: no releases")
    except Exception as e:
        print(f"  USD: failed ({e})")
    print("[te] PCE refresh complete")
    return load_history()


def fetch_ppi_only():
    """
    Refresh PPI YoY data for 7 currencies from TE (NZD uses Investing.com,
    see src/fetchers/investing_ppi.py).

    Scoring spec: Actual vs Consensus, fall back to TEForecast if Consensus
    is missing. Always picks the latest release by date.

    GBP uses ppi-input-yoy slug (handled by TE_INDICATOR_SLUG_OVERRIDES).
    Polite 2s delay between requests.
    """
    print("[te] refreshing PPI YoY for 7 currencies (NZD via Investing.com)...")
    countries = [c for c in TE_COUNTRY_SLUGS if c != "NZD"]
    success = 0
    for ccy in countries:
        try:
            releases = fetch_indicator(ccy, "ppi")
            if releases:
                releases_sorted = sorted(releases, key=lambda r: r.date, reverse=True)
                update_history(releases_sorted)
                success += 1
                latest = releases_sorted[0]
                a = latest.actual if latest.actual is not None else "n/a"
                c = latest.consensus if latest.consensus is not None else "n/a"
                f = latest.forecast if latest.forecast is not None else "n/a"
                print(f"  {ccy}: Actual={a}  Consensus={c}  TEForecast={f}  ({latest.date})")
            else:
                print(f"  {ccy}: no releases")
        except Exception as e:
            print(f"  {ccy}: failed ({e})")
        time.sleep(2)
    print(f"[te] PPI refresh complete: {success}/{len(countries)} ok")
    return load_history()


def fetch_jolts_only():
    """
    Refresh JOLTS Job Openings for USD only.

    Scoring spec (handled in score_pair.py): Actual vs Consensus (priority),
    fall back to TEForecast if Consensus missing. Direction is up_is_bullish
    (more job openings = strong economy = stronger USD). Released monthly,
    roughly one month lag. The TE slug is 'job-offers'.
    """
    print("[te] refreshing JOLTS Job Openings (USD only)...")
    try:
        releases = fetch_indicator("USD", "jolts")
        if releases:
            releases_sorted = sorted(releases, key=lambda r: r.date, reverse=True)
            update_history(releases_sorted)
            latest = releases_sorted[0]
            a = latest.actual if latest.actual is not None else "n/a"
            c = latest.consensus if latest.consensus is not None else "n/a"
            f = latest.forecast if latest.forecast is not None else "n/a"
            print(f"  USD: Actual={a}  Consensus={c}  TEForecast={f}  ({latest.date})")
        else:
            print("  USD: no releases")
    except Exception as e:
        print(f"  USD: failed ({e})")
    print("[te] JOLTS refresh complete")
    return load_history()


def fetch_adp_only():
    """
    Refresh ADP Employment Change for USD only.

    Scoring spec (handled in score_pair.py): Actual vs Consensus (priority),
    fall back to TEForecast if Consensus missing. Direction is up_is_bullish
    (more jobs added = stronger USD). Released monthly, 2 days before NFP.
    The TE slug is 'adp-employment-change'.
    """
    print("[te] refreshing ADP Employment (USD only)...")
    try:
        releases = fetch_indicator("USD", "adp")
        if releases:
            releases_sorted = sorted(releases, key=lambda r: r.date, reverse=True)
            update_history(releases_sorted)
            latest = releases_sorted[0]
            a = latest.actual if latest.actual is not None else "n/a"
            c = latest.consensus if latest.consensus is not None else "n/a"
            f = latest.forecast if latest.forecast is not None else "n/a"
            print(f"  USD: Actual={a}  Consensus={c}  TEForecast={f}  ({latest.date})")
        else:
            print("  USD: no releases")
    except Exception as e:
        print(f"  USD: failed ({e})")
    print("[te] ADP refresh complete")
    return load_history()


def fetch_jobless_claims_only():
    """
    Refresh Jobless Claims for USD only.

    Scoring spec (handled in score_pair.py): Actual vs Consensus (priority),
    fall back to TEForecast if Consensus missing. Direction is
    down_is_bullish (lower claims = stronger USD). The TE slug is
    'jobless-claims'. Released weekly (Thursdays).
    """
    print("[te] refreshing Jobless Claims (USD only)...")
    try:
        releases = fetch_indicator("USD", "jobless_claims")
        if releases:
            releases_sorted = sorted(releases, key=lambda r: r.date, reverse=True)
            update_history(releases_sorted)
            latest = releases_sorted[0]
            a = latest.actual if latest.actual is not None else "n/a"
            c = latest.consensus if latest.consensus is not None else "n/a"
            f = latest.forecast if latest.forecast is not None else "n/a"
            print(f"  USD: Actual={a}  Consensus={c}  TEForecast={f}  ({latest.date})")
        else:
            print("  USD: no releases")
    except Exception as e:
        print(f"  USD: failed ({e})")
    print("[te] Jobless Claims refresh complete")
    return load_history()


def fetch_unemployment_only():
    """
    Refresh Unemployment Rate for all 8 currencies from TE.

    Scoring spec (handled in score_pair.py): Actual vs Consensus (priority),
    fall back to TEForecast if Consensus missing. Direction is
    down_is_bullish (lower unemployment = stronger currency).
    The TE slug is 'unemployment-rate' (standard for all 8 countries).
    Polite 2s delay between requests.
    """
    print("[te] refreshing Unemployment Rate for all 8 currencies...")
    success = 0
    for ccy in TE_COUNTRY_SLUGS:
        try:
            releases = fetch_indicator(ccy, "unemployment_rate")
            if releases:
                releases_sorted = sorted(releases, key=lambda r: r.date, reverse=True)
                update_history(releases_sorted)
                success += 1
                latest = releases_sorted[0]
                a = latest.actual if latest.actual is not None else "n/a"
                c = latest.consensus if latest.consensus is not None else "n/a"
                f = latest.forecast if latest.forecast is not None else "n/a"
                print(f"  {ccy}: Actual={a}  Consensus={c}  TEForecast={f}  ({latest.date})")
            else:
                print(f"  {ccy}: no releases")
        except Exception as e:
            print(f"  {ccy}: failed ({e})")
        time.sleep(2)
    print(f"[te] Unemployment Rate refresh complete: {success}/{len(TE_COUNTRY_SLUGS)} ok")
    return load_history()


def fetch_consumer_conf_only():
    """
    Refresh Consumer Confidence for all 8 currencies. Scoring uses TEForecast
    (Actual vs TEForecast) for 7 ccys; CAD has no TEForecast so the parser
    falls back to stats table and scoring uses momentum (Actual vs Previous).
    """
    print("[te] refreshing consumer confidence for all 8 currencies...")
    success = 0
    for ccy in TE_COUNTRY_SLUGS:
        try:
            releases = fetch_indicator(ccy, "consumer_conf")
            if releases:
                releases_sorted = sorted(releases, key=lambda r: r.date, reverse=True)
                update_history(releases_sorted)
                success += 1
                latest = releases_sorted[0]
                a = latest.actual if latest.actual is not None else "n/a"
                f = latest.forecast if latest.forecast is not None else "n/a"
                c = latest.consensus if latest.consensus is not None else "n/a"
                p = latest.previous if latest.previous is not None else "n/a"
                print(f"  {ccy}: Actual={a}  TEForecast={f}  Consensus={c}  Previous={p}  ({latest.date})")
            else:
                print(f"  {ccy}: no releases")
        except Exception as e:
            print(f"  {ccy}: failed ({e})")
        time.sleep(2)
    print(f"[te] consumer conf refresh complete: {success}/{len(TE_COUNTRY_SLUGS)} ok")
    return load_history()


def fetch_te_all(only_indicators=None):
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
    parser.add_argument("--gaps-only", action="store_true")
    parser.add_argument("--country", type=str, default=None)
    parser.add_argument("--indicator", type=str, default=None)
    args = parser.parse_args()
    if args.country and args.indicator:
        rel = fetch_indicator(args.country, args.indicator, debug=True)
        print(f"\n[te] parsed {len(rel)} releases")
        for r in rel[:5]:
            print(f"  {r}")
    elif args.gaps_only:
        fetch_te_all(only_indicators=["mpmi", "spmi", "ppi", "pce"])
    else:
        fetch_te_all()
