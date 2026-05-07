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


def _cache_path(series_id: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"fred_{series_id}.json"


def _is_fresh(path: Path, max_age_hours: int = 6) -> bool:
    """Macro data publishes at most a few times a month; 6h cache is plenty."""
    if not path.exists():
        return False
    age = time.time() - path.stat().st_mtime
    return age < max_age_hours * 3600


def fetch_series(series_id: str, api_key: str, force: bool = False) -> list[FredObservation]:
    """
    Fetch one FRED series, with disk cache.
    Retries transient 500/503 errors with exponential backoff (1s, 2s, 4s).
    """
    cache = _cache_path(series_id)
    if not force and _is_fresh(cache):
        with open(cache) as f:
            data = json.load(f)
    else:
        params = {
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 60,
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


def fetch_all_macro(api_key: Optional[str] = None) -> dict:
    """
    Returns nested dict:
      result[currency][indicator] = list[FredObservation] (newest first)
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
                out[ccy][ind] = fetch_series(series_id, api_key)
            except Exception as e:
                print(f"[fred] {ccy}/{ind} ({series_id}) failed: {e}")
                out[ccy][ind] = []
            time.sleep(0.05)  # be polite
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
