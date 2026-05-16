"""
Forexbenchmark retail positioning fetcher.

Pulls the per-pair long% table from forexbenchmark.com/quant/retail_positions/.
The page has two tables; we want Table 1 (per-pair) which has columns:
  Symbol | Currency difference | Percentage long | Percentage / max | ...

We extract Symbol -> long% (which implies short% = 100 - long%).

Strategy:
  1. Fetch fresh on every call (data should always be up to date).
  2. If fetch succeeds, write to cache (committed to repo so CI has a fallback).
  3. If fetch fails, fall back to the most recent cached snapshot regardless of age.
  4. If both fail, return empty dict and the caller defaults to neutral 50/50.

Output shape: {symbol_upper: {"long": float, "short": float}}
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except ImportError:
    import requests as cffi_requests  # type: ignore
    HAS_CFFI = False

from bs4 import BeautifulSoup

URL = "https://forexbenchmark.com/quant/retail_positions/"
CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"
CACHE_FILE = CACHE_DIR / "forexbenchmark_outlook.json"


def _get(url: str, timeout: int = 20):
    if HAS_CFFI:
        return cffi_requests.get(url, impersonate="chrome120", timeout=timeout)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    return cffi_requests.get(url, headers=headers, timeout=timeout)


def _parse_pair_table(html: str) -> dict[str, dict] | None:
    """
    Parse Table 1 (the per-pair table) from the page HTML.
    Returns {symbol: {"long": float, "short": float}} or None on failure.
    """
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    if len(tables) < 2:
        return None
    pair_table = tables[1]
    headers = [th.get_text(strip=True) for th in pair_table.find_all("th")]
    try:
        sym_idx = headers.index("Symbol")
        long_idx = headers.index("Percentage long")
    except ValueError:
        return None

    body = pair_table.find("tbody") or pair_table
    rows = body.find_all("tr")
    out: dict[str, dict] = {}
    for row in rows:
        cells = row.find_all("td")
        if not cells or len(cells) <= max(sym_idx, long_idx):
            continue
        sym = cells[sym_idx].get_text(strip=True).upper().replace("/", "")
        long_text = cells[long_idx].get_text(strip=True)
        try:
            longp = float(long_text)
        except ValueError:
            continue
        if not sym or not (0 <= longp <= 100):
            continue
        out[sym] = {"long": longp, "short": round(100.0 - longp, 2)}
    return out if out else None


def _save_cache(data: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(data, f)


def _load_cache() -> dict | None:
    if not CACHE_FILE.exists():
        return None
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
        return data if data else None
    except Exception:
        return None


def fetch_forexbenchmark() -> dict[str, dict]:
    """
    Always attempts a fresh fetch. Falls back to cached snapshot only on
    network/parse failure. Returns {symbol: {"long": float, "short": float}}.
    """
    try:
        r = _get(URL)
        if r.status_code == 200:
            parsed = _parse_pair_table(r.text)
            if parsed:
                _save_cache(parsed)
                print(f"[forexbenchmark] fresh fetch OK; {len(parsed)} pairs")
                return parsed
            else:
                print("[forexbenchmark] fetch succeeded but parse returned no pairs")
        else:
            print(f"[forexbenchmark] HTTP {r.status_code}")
    except Exception as e:
        print(f"[forexbenchmark] fetch failed: {e}")

    # Fallback: stale cache.
    stale = _load_cache()
    if stale:
        print(f"[forexbenchmark] using stale cache; {len(stale)} pairs")
        return stale
    print("[forexbenchmark] no data available")
    return {}


if __name__ == "__main__":
    data = fetch_forexbenchmark()
    for sym in sorted(data.keys()):
        r = data[sym]
        print(f"  {sym}: long={r['long']:.1f}%  short={r['short']:.1f}%")
