"""
CFTC Commitment of Traders fetcher (Legacy Futures Only - Non-Commercial).

Uses CFTC's public Socrata API at publicreporting.cftc.gov. The Legacy
COT report's "Non-Commercial" category matches EdgeFinder's "smart money"
definition exactly (speculators including hedge funds and CTAs).

API endpoint: https://publicreporting.cftc.gov/resource/6dca-aqww.json
- Free, no key required
- Supports filtering by market name and date
- Returns JSON, no zip handling
- Always current (CFTC updates Friday 3:30 PM ET)
"""
from __future__ import annotations

import json
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

CFTC_API = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"

# COT publishes weekly (Friday 3:30 PM ET, reports Tue close). Anything older
# than 14 days means the market got renamed, the API is broken, or CFTC stopped
# publishing it. Flag it so it's visible on the heatmap instead of silently
# scoring with stale data (e.g., USD reading from 2022 that we just fixed).
MAX_STALE_DAYS = 14  # weekly publish + a buffer week

# Map currencies to their CFTC market_and_exchange_names value (Legacy uses
# the same labels as TFF for currency futures).
CFTC_NAMES = {
    "USD": "USD INDEX - ICE FUTURES U.S.",
    "EUR": "EURO FX - CHICAGO MERCANTILE EXCHANGE",
    "GBP": "BRITISH POUND - CHICAGO MERCANTILE EXCHANGE",
    "JPY": "JAPANESE YEN - CHICAGO MERCANTILE EXCHANGE",
    "CHF": "SWISS FRANC - CHICAGO MERCANTILE EXCHANGE",
    "AUD": "AUSTRALIAN DOLLAR - CHICAGO MERCANTILE EXCHANGE",
    "CAD": "CANADIAN DOLLAR - CHICAGO MERCANTILE EXCHANGE",
    "NZD": "NZ DOLLAR - CHICAGO MERCANTILE EXCHANGE",
    "XAU": "GOLD - COMMODITY EXCHANGE INC.",
    "XPT": "PLATINUM - NEW YORK MERCANTILE EXCHANGE",
    "XAG": "SILVER - COMMODITY EXCHANGE INC.",
    "NKY": "NIKKEI STOCK AVERAGE YEN DENOM - CHICAGO MERCANTILE EXCHANGE",
    "NDX": "NASDAQ MINI - CHICAGO MERCANTILE EXCHANGE",
}

# Non-FX assets scored as standalone instruments (own COT, contrarian crowd from
# non-reportable positioning, no base/quote macro diff). XAU = Gold,
# XPT = Platinum, XAG = Silver, NKY = Nikkei 225, NDX = NASDAQ-100,
# UKX = FTSE 100, DAXI = DAX 40 (UKX/DAXI have no CFTC COT feed -> COT/crowd
# stay neutral, matching EdgeFinder).
COMMODITY_CCYS = {"XAU", "XPT", "XAG", "NKY", "NDX", "UKX", "DAXI"}


@dataclass
class CotReading:
    currency: str
    report_date: str
    long_contracts: int
    short_contracts: int
    long_change: int
    short_change: int
    net_position: int
    long_pct: float
    short_pct: float
    weekly_change_pct: float
    long_pct_change: float
    open_interest: int
    open_interest_change: int
    retail_long_pct: float = 50.0
    is_stale: bool = False
    days_old: int = 0


def _cache_path() -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / "cot_legacy_api.json"


def _is_fresh(path: Path, max_age_hours: int = 24) -> bool:
    if not path.exists():
        return False
    return (time.time() - path.stat().st_mtime) < max_age_hours * 3600


def _fetch_market(market: str, as_of_date: str | None = None, limit: int = 4) -> list[dict]:
    """
    Fetch up to `limit` most recent reports for a single market via Socrata API.
    If as_of_date is given, restricts to reports on or before that date.

    Default limit=4 is plenty for current scoring (need current + prev week).
    Use limit=52 for the history dashboard.
    """
    where_parts = [f"market_and_exchange_names = '{market}'"]
    if as_of_date:
        where_parts.append(f"report_date_as_yyyy_mm_dd <= '{as_of_date}T23:59:59.999'")
    where = " AND ".join(where_parts)

    params = {
        "$where": where,
        "$order": "report_date_as_yyyy_mm_dd DESC",
        # Backtests: one spare row, since the newest report on/before as_of may
        # not have been RELEASED yet and gets dropped below.
        "$limit": str(limit + 1 if as_of_date else limit),
    }
    url = f"{CFTC_API}?{urllib.parse.urlencode(params)}"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    rows = r.json()
    if as_of_date:
        # report_date is the Tuesday positions date; CFTC publishes it Friday
        # (Monday after a holiday week). Filtering on report_date let backtests
        # see positioning ~3 days before publication. Keep only released ones.
        rows = [x for x in rows
                if cot_release_date((x.get("report_date_as_yyyy_mm_dd") or "")[:10]) <= as_of_date]
    return rows[:limit]


def _to_int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def fetch_cot(as_of_date: str | None = None) -> dict[str, CotReading]:
    """
    Returns dict[currency] -> latest CotReading using Legacy Non-Commercial.
    """
    out: dict[str, CotReading] = {}
    not_found: list[str] = []

    for ccy, market in CFTC_NAMES.items():
        try:
            rows = _fetch_market(market, as_of_date)
        except Exception as e:
            print(f"[cot] {ccy} ({market}) fetch failed: {e}")
            continue

        if not rows:
            not_found.append(ccy)
            continue

        latest = rows[0]
        prev = rows[1] if len(rows) > 1 else None

        long_c = _to_int(latest.get("noncomm_positions_long_all"))
        short_c = _to_int(latest.get("noncomm_positions_short_all"))
        long_chg = _to_int(latest.get("change_in_noncomm_long_all"))
        short_chg = _to_int(latest.get("change_in_noncomm_short_all"))
        oi = _to_int(latest.get("open_interest_all"))
        oi_chg = _to_int(latest.get("change_in_open_interest_all"))

        nonrept_long = _to_int(latest.get("nonrept_positions_long_all"))
        nonrept_short = _to_int(latest.get("nonrept_positions_short_all"))
        nonrept_total = nonrept_long + nonrept_short
        retail_long_pct = 100 * nonrept_long / nonrept_total if nonrept_total else 50.0

        net = long_c - short_c
        total = long_c + short_c
        long_pct = 100 * long_c / total if total else 50.0
        short_pct = 100 - long_pct

        net_chg = long_chg - short_chg
        weekly_change_pct = 100 * net_chg / total if total else 0.0

        # EF Net % Change: this week's long% MINUS last week's long%
        if prev:
            prev_long = _to_int(prev.get("noncomm_positions_long_all"))
            prev_short = _to_int(prev.get("noncomm_positions_short_all"))
            prev_total = prev_long + prev_short
            prev_long_pct = 100 * prev_long / prev_total if prev_total else 50.0
        else:
            prev_long_pct = long_pct
        long_pct_change = long_pct - prev_long_pct

        # Date string: API returns "2026-05-05T00:00:00.000" -> trim to YYYY-MM-DD
        report_date = (latest.get("report_date_as_yyyy_mm_dd") or "")[:10]

        # Freshness check. Compare report_date against the as_of_date in
        # backtest mode, otherwise against today.
        ref_date_str = as_of_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            ref_dt = datetime.strptime(ref_date_str, "%Y-%m-%d")
            rep_dt = datetime.strptime(report_date, "%Y-%m-%d")
            days_old = (ref_dt - rep_dt).days
        except (ValueError, TypeError):
            days_old = 0
        is_stale = days_old > MAX_STALE_DAYS
        if is_stale:
            print(f"[cot] WARNING: {ccy} COT is STALE. Last report {report_date} ({days_old} days old). Market may have been renamed.")

        out[ccy] = CotReading(
            currency=ccy,
            report_date=report_date,
            long_contracts=long_c,
            short_contracts=short_c,
            long_change=long_chg,
            short_change=short_chg,
            net_position=net,
            long_pct=long_pct,
            short_pct=short_pct,
            weekly_change_pct=weekly_change_pct,
            long_pct_change=long_pct_change,
            open_interest=oi,
            open_interest_change=oi_chg,
            retail_long_pct=retail_long_pct,
            is_stale=is_stale,
            days_old=days_old,
        )

    if not_found:
        print(f"[cot] WARNING: not found in CFTC API: {not_found}")
    return out


def fetch_cot_history(weeks: int = 52, as_of_date: str | None = None) -> dict[str, list[dict]]:
    """
    Fetch the last `weeks` of COT reports for each of the 8 currencies.
    Returns dict[ccy] -> list of report dicts (newest first), each shaped:
      {
        "date": "YYYY-MM-DD",
        "long_contracts": int, "short_contracts": int,
        "long_change": int, "short_change": int,
        "long_pct": float, "short_pct": float,
        "long_pct_change": float,        # current week's Long% - prev week's
        "net_position": int,
        "open_interest": int, "open_interest_change": int,
      }

    Used by the COT Detail dashboard. Hits the CFTC Socrata API fresh on
    every call, no caching (data is small, ~50KB per call for 8 ccys).
    """
    out: dict[str, list[dict]] = {}
    for ccy, market in CFTC_NAMES.items():
        try:
            rows = _fetch_market(market, as_of_date, limit=weeks)
        except Exception as e:
            print(f"[cot-history] {ccy} ({market}) fetch failed: {e}")
            continue
        if not rows:
            continue

        # Compute per-row Long% and weekly Δ Long% so the frontend can plot
        # them directly without recomputing.
        parsed: list[dict] = []
        for i, x in enumerate(rows):
            L = _to_int(x.get("noncomm_positions_long_all"))
            S = _to_int(x.get("noncomm_positions_short_all"))
            total = L + S
            long_pct = (100.0 * L / total) if total else 50.0
            short_pct = 100.0 - long_pct
            # Prev week (older record) is rows[i+1]
            if i + 1 < len(rows):
                pL = _to_int(rows[i + 1].get("noncomm_positions_long_all"))
                pS = _to_int(rows[i + 1].get("noncomm_positions_short_all"))
                pt = pL + pS
                prev_long_pct = (100.0 * pL / pt) if pt else 50.0
            else:
                prev_long_pct = long_pct
            parsed.append({
                "date": (x.get("report_date_as_yyyy_mm_dd") or "")[:10],
                "long_contracts": L,
                "short_contracts": S,
                "long_change": _to_int(x.get("change_in_noncomm_long_all")),
                "short_change": _to_int(x.get("change_in_noncomm_short_all")),
                "long_pct": round(long_pct, 2),
                "short_pct": round(short_pct, 2),
                "long_pct_change": round(long_pct - prev_long_pct, 2),
                "net_position": L - S,
                "open_interest": _to_int(x.get("open_interest_all")),
                "open_interest_change": _to_int(x.get("change_in_open_interest_all")),
            })
        out[ccy] = parsed
    return out


# Disaggregated Futures Only report (commodities: Managed Money category).
CFTC_DISAGG_API = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"


def cot_release_date(report_date: str, lag_days: int = 3) -> str:
    """Tuesday positions -> Friday release. In a week with a US federal
    holiday between Wednesday and Friday, CFTC publishes the following Monday."""
    from datetime import timedelta
    from src.fetchers.eia import _holidays
    rd = datetime.strptime(report_date, "%Y-%m-%d").date()
    rel = rd + timedelta(days=lag_days)
    if any((rd + timedelta(days=k)) in _holidays() for k in range(1, lag_days + 1)):
        rel = rd + timedelta(days=lag_days + 3)
    return rel.isoformat()


def fetch_disaggregated(symbol: str, contract_code: str, as_of_date: str | None = None,
                        release_lag_days: int = 3) -> CotReading | None:
    """
    Latest Managed Money reading for one contract from the Disaggregated report,
    as a CotReading (long/short = managed money) so the FX COT scorer applies
    unchanged. Queried by contract code, not market name (CFTC renames markets:
    067651 is now "WTI-PHYSICAL", formerly "CRUDE OIL, LIGHT SWEET").

    Backtest-safe: only reports whose RELEASE date (Friday after the Tuesday
    report date) is on/before as_of_date are used. Live result is cached to
    data/cache/cot_disagg_<code>.json as a fallback for API outages.
    """
    cache = CACHE_DIR / f"cot_disagg_{contract_code}.json"
    where = [f"cftc_contract_market_code = '{contract_code}'"]
    if as_of_date:
        where.append(f"report_date_as_yyyy_mm_dd <= '{as_of_date}T23:59:59.999'")
    params = {
        "$where": " AND ".join(where),
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": "6",
    }
    try:
        r = requests.get(f"{CFTC_DISAGG_API}?{urllib.parse.urlencode(params)}", timeout=20)
        r.raise_for_status()
        rows = r.json()
        if not as_of_date and rows:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    except Exception as e:
        print(f"[cot-disagg] {symbol} ({contract_code}) fetch failed: {e}")
        if as_of_date or not cache.exists():
            return None
        rows = json.loads(cache.read_text(encoding="utf-8"))

    ref = as_of_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return disagg_reading(symbol, rows, ref, release_lag_days)


def disagg_reading(symbol: str, rows: list[dict], ref: str,
                   release_lag_days: int = 3) -> CotReading | None:
    """Managed-money CotReading as of `ref` from disaggregated rows (any order,
    any span): keeps only reports released on/before ref, uses the latest two.
    Shared by the live fetch and the long-history study (scripts/study_oil.py)."""
    rows = sorted((x for x in rows
                   if cot_release_date((x.get("report_date_as_yyyy_mm_dd") or "")[:10], release_lag_days) <= ref),
                  key=lambda x: x["report_date_as_yyyy_mm_dd"], reverse=True)
    if not rows:
        return None
    latest, prev = rows[0], (rows[1] if len(rows) > 1 else None)

    def _long_pct(x):
        L = _to_int(x.get("m_money_positions_long_all"))
        S = _to_int(x.get("m_money_positions_short_all"))
        return L, S, (100 * L / (L + S) if L + S else 50.0)

    L, S, long_pct = _long_pct(latest)
    prev_long_pct = _long_pct(prev)[2] if prev else long_pct
    report_date = (latest.get("report_date_as_yyyy_mm_dd") or "")[:10]
    days_old = (datetime.strptime(ref, "%Y-%m-%d") - datetime.strptime(report_date, "%Y-%m-%d")).days
    return CotReading(
        currency=symbol,
        report_date=report_date,
        long_contracts=L,
        short_contracts=S,
        long_change=_to_int(latest.get("change_in_m_money_long_all")),
        short_change=_to_int(latest.get("change_in_m_money_short_all")),
        net_position=L - S,
        long_pct=long_pct,
        short_pct=100 - long_pct,
        weekly_change_pct=0.0,
        long_pct_change=long_pct - prev_long_pct,
        open_interest=_to_int(latest.get("open_interest_all")),
        open_interest_change=_to_int(latest.get("change_in_open_interest_all")),
        is_stale=days_old > MAX_STALE_DAYS,
        days_old=days_old,
    )


if __name__ == "__main__":
    cot = fetch_cot()
    for ccy, r in cot.items():
        print(f"{ccy}: long%={r.long_pct:.1f}  Δlong%={r.long_pct_change:+.2f}pp  net={r.net_position:+d}  ({r.report_date})")
