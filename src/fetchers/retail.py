"""
Retail sentiment fetcher.

Combines TWO sources of per-pair retail long%/short% positioning:
  1. Myfxbook Community Outlook (broker-aggregated retail accounts)
  2. Forexbenchmark Quant Retail Positions (separate broker aggregator)

For each pair, if both sources have data, the long% values are averaged.
If only one source has it, that one is used. If neither, defaults to 50/50.

Always fetches fresh on every call. Cache files are kept only as a fallback
for when the live fetch fails (e.g., GitHub Actions IP blocked by Cloudflare).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except ImportError:
    import requests as cffi_requests  # type: ignore
    HAS_CFFI = False

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"

MYFXBOOK_JSON = "https://www.myfxbook.com/api/get-community-outlook.json"
MYFXBOOK_HTML = "https://www.myfxbook.com/community/outlook"


@dataclass
class RetailReading:
    symbol: str
    long_pct: float
    short_pct: float


def _cache_path() -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / "myfxbook_outlook.json"


def _is_fresh(path: Path, max_age_hours: int = 1) -> bool:
    if not path.exists():
        return False
    return (time.time() - path.stat().st_mtime) < max_age_hours * 3600


def _get(url: str, timeout: int = 15):
    """GET with Chrome TLS impersonation if available."""
    if HAS_CFFI:
        return cffi_requests.get(url, impersonate="chrome120", timeout=timeout)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.myfxbook.com/",
    }
    return cffi_requests.get(url, headers=headers, timeout=timeout)


def _try_json() -> dict | None:
    try:
        r = _get(MYFXBOOK_JSON)
        if r.status_code != 200:
            return None
        data = r.json()
        symbols = data.get("symbols") or data.get("data", {}).get("symbols")
        if not symbols:
            return None
        out = {}
        for s in symbols:
            sym = (s.get("name") or "").replace("/", "").upper()
            longp = float(s.get("longPercentage", 50))
            shortp = float(s.get("shortPercentage", 50))
            if sym:
                out[sym] = {"long": longp, "short": shortp}
        return out if out else None
    except Exception:
        return None


def _try_html() -> dict | None:
    try:
        r = _get(MYFXBOOK_HTML)
        if r.status_code != 200:
            return None
        return _parse_html(r.text)
    except Exception:
        return None


def _load_stale_cache() -> dict | None:
    """Read cache file regardless of age. Used as last-resort fallback."""
    cache = _cache_path()
    if not cache.exists():
        return None
    try:
        with open(cache) as f:
            data = json.load(f)
        return data if data else None
    except Exception:
        return None


def _fetch_myfxbook() -> dict:
    """Always-fresh Myfxbook fetch; falls back to cache only on failure."""
    cache = _cache_path()
    payload = _try_json()
    source = "json"
    if not payload:
        payload = _try_html()
        source = "html"
    if payload:
        with open(cache, "w") as f:
            json.dump(payload, f)
        print(f"[retail] myfxbook fresh ({source}); {len(payload)} pairs")
        return payload
    stale = _load_stale_cache()
    if stale:
        age_h = (time.time() - cache.stat().st_mtime) / 3600
        print(f"[retail] myfxbook fetch failed; stale cache from {age_h:.1f}h ago ({len(stale)} pairs)")
        return stale
    print("[retail] myfxbook unavailable, no cache")
    return {}


def fetch_retail(pairs: list[str]) -> dict[str, RetailReading]:
    """
    Combine Myfxbook + Forexbenchmark per-pair long%/short%.
    For each pair, average long% across whichever sources have it.
    Defaults to neutral 50/50 if neither source covers the pair.
    """
    # Avoid circular import: keep this import local.
    from src.fetchers.forexbenchmark import fetch_forexbenchmark

    mfx = _fetch_myfxbook()
    fxb = fetch_forexbenchmark()

    out: dict[str, RetailReading] = {}
    for sym in pairs:
        longs: list[float] = []
        if sym in mfx:
            longs.append(float(mfx[sym]["long"]))
        if sym in fxb:
            longs.append(float(fxb[sym]["long"]))
        if longs:
            avg_long = sum(longs) / len(longs)
            out[sym] = RetailReading(sym, avg_long, 100.0 - avg_long)
        else:
            out[sym] = RetailReading(sym, 50.0, 50.0)
    print(f"[retail] combined: {sum(1 for s in pairs if s in mfx and s in fxb)} pairs with both sources, "
          f"{sum(1 for s in pairs if (s in mfx) != (s in fxb))} with one, "
          f"{sum(1 for s in pairs if s not in mfx and s not in fxb)} neutral fallback")
    return out


def _parse_html(html: str) -> dict[str, dict] | None:
    out: dict[str, dict] = {}
    rx = re.compile(
        r"([A-Z]{6}).{0,400}?(\d{1,3}(?:\.\d+)?)\s*%.{0,200}?(\d{1,3}(?:\.\d+)?)\s*%",
        re.S,
    )
    for m in rx.finditer(html):
        sym = m.group(1).upper()
        try:
            longp = float(m.group(2))
            shortp = float(m.group(3))
            if abs(longp + shortp - 100) < 5:
                out[sym] = {"long": longp, "short": shortp}
        except ValueError:
            continue
    return out if out else None


if __name__ == "__main__":
    pairs = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "NZDUSD", "USDCHF",
             "EURJPY", "GBPJPY", "AUDJPY", "NZDJPY", "CADJPY", "CHFJPY"]
    print(f"curl_cffi installed: {HAS_CFFI}")
    data = fetch_retail(pairs)
    for s, r in data.items():
        print(f"{s}: long={r.long_pct:.1f}%  short={r.short_pct:.1f}%")
