"""
Retail sentiment fetcher.
Uses curl_cffi (already installed via yfinance) to impersonate a real Chrome
browser at the TLS layer, which bypasses the Cloudflare protection that
blocks plain `requests`.

Order of attempts:
1. Myfxbook public JSON outlook endpoint (impersonating Chrome)
2. Myfxbook HTML outlook page (impersonating Chrome)
3. Neutral 50/50 fallback (so scoring still runs)
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
    # Fallback to plain requests with browser-ish headers
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


def fetch_retail(pairs: list[str]) -> dict[str, RetailReading]:
    cache = _cache_path()
    if _is_fresh(cache):
        with open(cache) as f:
            payload = json.load(f)
    else:
        payload = _try_json()
        source = "json"
        if not payload:
            payload = _try_html()
            source = "html"
        if payload:
            with open(cache, "w") as f:
                json.dump(payload, f)
            print(f"[retail] sourced from myfxbook ({source}); {len(payload)} pairs found")
        else:
            print("[retail] all sources failed; using neutral 50/50 fallback")
            payload = {}

    out: dict[str, RetailReading] = {}
    for sym in pairs:
        if sym in payload:
            out[sym] = RetailReading(sym, payload[sym]["long"], payload[sym]["short"])
        else:
            out[sym] = RetailReading(sym, 50.0, 50.0)
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
