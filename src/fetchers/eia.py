"""
EIA API v2 fetcher for the Weekly Petroleum Status Report (USOIL row).

Endpoint: https://api.eia.gov/v2/seriesid/PET.<SERIES>.W?api_key=...
Key: EIA_API_KEY environment variable (.env locally, repo secret on Actions).
Free registration: https://www.eia.gov/opendata/register.php

Each weekly point is dated by its PERIOD (week ending Friday). The report is
released the following Wednesday 10:30 ET, or Thursday when a US federal
holiday falls Mon-Wed of release week. `release_date()` encodes that, and
`load_series(as_of_date=...)` drops every point not yet released on that
date, so backtests never see a print before it was published.

Cache: data/cache/eia_weekly.json (full history per series, committed so CI
and backtests have it). Freshness uses a fetched_at stamp inside the JSON,
not the file mtime (checkout resets mtimes on GitHub Actions).
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from pandas.tseries.holiday import USFederalHolidayCalendar

EIA_URL = "https://api.eia.gov/v2/seriesid/PET.{series}.W"
CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"
CACHE_FILE = CACHE_DIR / "eia_weekly.json"
MAX_AGE_HOURS = 6

_HOLIDAYS: set[date] | None = None


def _holidays() -> set[date]:
    global _HOLIDAYS
    if _HOLIDAYS is None:
        cal = USFederalHolidayCalendar()
        _HOLIDAYS = {d.date() for d in cal.holidays(start="1990-01-01", end="2040-12-31")}
    return _HOLIDAYS


def release_date(period: str, lag_days: int = 5) -> str:
    """Week-ending Friday -> report release date (Wednesday, +1 day when a
    federal holiday falls between the Monday and the release day)."""
    p = datetime.strptime(period, "%Y-%m-%d").date()
    rel = p + timedelta(days=lag_days)
    hol = _holidays()
    if any((p + timedelta(days=k)) in hol for k in range(3, lag_days + 1)):
        rel += timedelta(days=1)
    return rel.isoformat()


def _load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=1), encoding="utf-8")


def _fetch_remote(series: str, api_key: str) -> list[list]:
    """Full weekly history, oldest first, as [[period, value], ...]."""
    url = EIA_URL.format(series=series)
    last_err = None
    for attempt in range(3):
        try:
            r = requests.get(url, params={"api_key": api_key}, timeout=30)
            r.raise_for_status()
            rows = r.json()["response"]["data"]
            out = [[x["period"], float(x["value"])] for x in rows
                   if x.get("value") not in (None, "")]
            out.sort(key=lambda t: t[0])
            if not out:
                raise RuntimeError("empty series")
            return out
        except Exception as e:  # network, HTTP, schema
            last_err = e
            time.sleep(2 ** (attempt + 1))
    raise RuntimeError(f"EIA {series} fetch failed: {last_err}")


def refresh(series_ids: list[str], force: bool = False) -> dict:
    """Refresh stale series in the cache. No key -> cache untouched (warns)."""
    cache = _load_cache()
    api_key = os.getenv("EIA_API_KEY")
    now = datetime.now(timezone.utc)
    changed = False
    for s in series_ids:
        entry = cache.get(s)
        if entry and not force:
            try:
                age = now - datetime.fromisoformat(entry["fetched_at"])
                if age < timedelta(hours=MAX_AGE_HOURS):
                    continue
            except (KeyError, ValueError):
                pass
        if not api_key:
            print(f"[eia] EIA_API_KEY not set; {s} uses cache only"
                  f"{'' if entry else ' (no cache -> n/a)'}")
            continue
        try:
            data = _fetch_remote(s, api_key)
            cache[s] = {"fetched_at": now.isoformat(), "data": data}
            changed = True
            print(f"[eia] {s}: {len(data)} weeks, latest {data[-1][0]} = {data[-1][1]:,.0f}")
        except Exception as e:
            print(f"[eia] {e}; {'using stale cache' if entry else 'no cache -> n/a'}")
    if changed:
        _save_cache(cache)
    return cache


def load_series(series: str, as_of_date: str | None = None, lag_days: int = 5) -> list[tuple[str, float]]:
    """Cached series, oldest first, restricted to points released on/before
    as_of_date (today when None)."""
    entry = _load_cache().get(series)
    if not entry:
        return []
    cutoff = as_of_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return [(p, v) for p, v in entry["data"] if release_date(p, lag_days) <= cutoff]


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    ids = ["WCESTUS1", "W_EPC0_SAX_YCUOK_MBBL", "WCRFPUS2", "WRPUPUS2"]
    refresh(ids, force=True)
    for s in ids:
        pts = load_series(s)
        if pts:
            print(f"{s}: {pts[-1]} released {release_date(pts[-1][0])}")
