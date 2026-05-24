"""
FRED API fetcher.
Pulls macro time series for each currency. Caches raw JSON to disk so reruns
don't burn through rate limits and so we can run offline.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests
import yaml

FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"
CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


@dataclass
class FredObservation:
    date: str
    value: float


def load_series_map() -> dict:
    """Load the FRED series ID mapping from config."""
    with open(CONFIG_DIR / "fred_series.yaml") as f:
        return yaml.safe_load(f)


def _cache_path(series_id: str, suffix: str = "") -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tag = f"_{suffix}" if suffix else ""
    return CACHE_DIR / f"fred_{series_id}{tag}.json"


def _is_fresh(path: Path, max_age_hours: int = 6) -> bool:
    """Macro data publishes at most a few times a month; 6h cache is plenty."""
    if not path.exists():
        return False
    age = time.time() - path.stat().st_mtime
    return age < max_age_hours * 3600


def fetch_series(series_id: str, api_key: str, force: bool = False,
                 limit: int = 60, cache_suffix: str = "") -> list[FredObservation]:
    """
    Fetch one FRED series, with disk cache.
    Retries transient 500/503 errors with exponential backoff (1s, 2s, 4s).

    limit: number of most-recent observations to pull. Default 60 (enough for
    scoring momentum/z-score). The inflation history chart uses a larger limit.
    cache_suffix: keeps high-limit history caches separate from the default
    60-obs scoring caches so the two don't overwrite each other.
    """
    cache = _cache_path(series_id, cache_suffix)
    if not force and _is_fresh(cache):
        with open(cache) as f:
            data = json.load(f)
    else:
        params = {
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": limit,
        }
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                r = requests.get(FRED_BASE, params=params, timeout=20)
                # Retry on transient server errors; raise on client errors (400 etc.)
                if r.status_code in (500, 502, 503, 504):
                    raise requests.HTTPError(f"transient {r.status_code}")
                r.raise_for_status()
                data = r.json()
                with open(cache, "w") as f:
                    json.dump(data, f)
                break
            except requests.HTTPError as e:
                last_err = e
                # Don't retry permanent (4xx) errors
                if "transient" not in str(e):
                    raise
                if attempt < 2:
                    time.sleep(2 ** attempt)  # 1s, 2s
                    continue
                raise
            except requests.RequestException as e:
                last_err = e
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise
        else:
            if last_err:
                raise last_err

    obs = []
    for o in data.get("observations", []):
        try:
            obs.append(FredObservation(date=o["date"], value=float(o["value"])))
        except (ValueError, KeyError):
            continue  # skip "." (missing) values
    return obs


def fetch_all_macro(api_key: Optional[str] = None, as_of_date: Optional[str] = None) -> dict:
    """
    Returns nested dict:
      result[currency][indicator] = list[FredObservation] (newest first)

    If as_of_date is provided (YYYY-MM-DD), filters each series to only include
    observations dated on or before that date. Used for historical backtesting.
    """
    api_key = api_key or os.getenv("FRED_API_KEY")
    if not api_key:
        raise RuntimeError("FRED_API_KEY missing. Add it to your .env file.")

    series_map = load_series_map()
    out: dict = {}
    for ccy, indicators in series_map.items():
        out[ccy] = {}
        for ind, series_id in indicators.items():
            if not series_id:
                out[ccy][ind] = []
                continue
            try:
                # When backtesting, force a fresh fetch (skip cache) to get the
                # full history; filter afterwards.
                obs = fetch_series(series_id, api_key, force=bool(as_of_date))
                if as_of_date:
                    obs = [o for o in obs if o.date <= as_of_date]
                out[ccy][ind] = obs
            except Exception as e:
                print(f"[fred] {ccy}/{ind} ({series_id}) failed: {e}")
                out[ccy][ind] = []
            time.sleep(0.05)
    return out


def fetch_treasury_2y(api_key: Optional[str] = None, as_of_date: Optional[str] = None) -> list[FredObservation]:
    """
    Fetch recent 2-year Treasury yield (FRED DGS2) for gold interest-rate scoring.
    Returns list of FredObservation (newest first), at least 20 days to allow
    an 8-day SMA computation with margin.
    """
    api_key = api_key or os.getenv("FRED_API_KEY")
    if not api_key:
        raise RuntimeError("FRED_API_KEY missing. Add it to your .env file.")
    try:
        obs = fetch_series("DGS2", api_key, limit=30, cache_suffix="2y")
        if as_of_date:
            obs = [o for o in obs if o.date <= as_of_date]
        return obs
    except Exception as e:
        print(f"[fred] DGS2 (2Y Treasury) failed: {e}")
        return []


def fetch_cpi_history(api_key: Optional[str] = None, force: bool = False,
                      limit: int = 240, as_of_date: Optional[str] = None) -> dict:
    """
    Fetch a long CPI *index* history for each currency, for the inflation page
    line chart. Returns {ccy: list[FredObservation]} (newest first).

    Uses a separate "hist" cache so it doesn't clobber the 60-obs scoring cache.
    limit=240 gives 20 years monthly / 60 years quarterly (FRED returns
    whatever it actually has).
    """
    api_key = api_key or os.getenv("FRED_API_KEY")
    if not api_key:
        raise RuntimeError("FRED_API_KEY missing. Add it to your .env file.")

    series_map = load_series_map()
    out: dict = {}
    for ccy, indicators in series_map.items():
        series_id = indicators.get("cpi")
        if not series_id:
            out[ccy] = []
            continue
        try:
            obs = fetch_series(series_id, api_key, force=bool(as_of_date) or force,
                               limit=limit, cache_suffix="hist")
            if as_of_date:
                obs = [o for o in obs if o.date <= as_of_date]
            out[ccy] = obs
        except Exception as e:
            print(f"[fred-hist] {ccy}/cpi ({series_id}) failed: {e}")
            out[ccy] = []
        time.sleep(0.05)
    return out


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    data = fetch_all_macro()
    for ccy, inds in data.items():
        print(f"{ccy}:")
        for ind, obs in inds.items():
            latest = obs[0].value if obs else "n/a"
            print(f"  {ind}: {len(obs)} obs, latest={latest}")
