"""
Investing.com China manufacturing PMI (USOIL regime row: China demand).

Two pages, verified 2026-10-07:
  NBS   official NBS Manufacturing PMI, event id 594 (scored: the headline,
        large/state firms, released the last day of the month)
  CAIXIN Caixin/RatingDog Manufacturing PMI, event id 753 (shown alongside:
        private/export firms, released the first business day)
Same fetch/parse as the other Investing fetchers (investing._fetch_with_retries,
which routes through the scraping API when SCRAPER_API_KEY is set, and
investing.parse_latest_release). Cloudflare blocks GitHub Actions, so it is
refreshed from the laptop by scripts/refresh_investing.py (target china_pmi;
--due fetches it once the cached print is a month old).

Cache: data/cache/investing_china_pmi.json  {key: {date, actual, forecast,
previous}, "_fetched_at": iso}. `date` is the release date.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src.fetchers import investing

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"
CACHE_FILE = CACHE_DIR / "investing_china_pmi.json"

CHINA_PMI_URLS: dict[str, str] = {
    "NBS": "https://www.investing.com/economic-calendar/chinese-manufacturing-pmi-594",
    "CAIXIN": "https://www.investing.com/economic-calendar/chinese-caixin-manufacturing-pmi-753",
}
DUE_AFTER_DAYS = 27     # monthly print: due again ~4 weeks after the last one
RETRY_HOURS = 8         # don't re-hit the page more often than this while due

_LAST_FRESH: set[str] = set()


def load_cached() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def fetch_china_pmi(sleep_between: float = 6.0) -> dict:
    """Fetch both pages; a failed page keeps its cached value."""
    global _LAST_FRESH
    _LAST_FRESH = set()
    cache = load_cached()
    for i, (key, url) in enumerate(CHINA_PMI_URLS.items()):
        if i:
            time.sleep(sleep_between)
        status, html = investing._fetch_with_retries(url)
        rel = investing.parse_latest_release(html) if html else None
        if rel and rel.get("actual") is not None:
            cache[key] = rel
            _LAST_FRESH.add(key)
            print(f"[china-pmi] {key} {rel}")
        else:
            print(f"[china-pmi] {key} fetch/parse failed (HTTP {status}); "
                  f"{'keeping cache' if key in cache else 'no cache'}")
    cache["_fetched_at"] = datetime.now(timezone.utc).isoformat()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    return cache


def is_due(today: date | None = None) -> bool:
    """Due when the cached NBS print is >= DUE_AFTER_DAYS old (or missing) and
    the page wasn't tried in the last RETRY_HOURS."""
    today = today or datetime.now(timezone.utc).date()
    cache = load_cached()
    nbs = cache.get("NBS") or {}
    if nbs.get("date") and (today - date.fromisoformat(nbs["date"])).days < DUE_AFTER_DAYS:
        return False
    last = cache.get("_fetched_at")
    if last and datetime.now(timezone.utc) - datetime.fromisoformat(last) < timedelta(hours=RETRY_HOURS):
        return False
    return True


if __name__ == "__main__":
    fetch_china_pmi()
