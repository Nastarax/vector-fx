"""
Baker Hughes North America rotary rig count (USOIL rig-count row).

Source: https://rigcount.bakerhughes.com/na-rig-count. The page links the
current weekly workbook ("North America Rig Count Report - New Report", .xlsx,
~7MB, covers 2024-01 onward) and a frozen history file ("... New Report
(2013-Aug 2025)"). Download URLs are /static-files/<uuid> and the uuid changes,
so the link is scraped from the page by its text every time.

The "NAM Weekly" sheet is long-format (one row per country/basin/drill-for/
trajectory) with a US_PublishDate column. US_PublishDate IS the release date
(Friday 1pm ET; Wednesday/Thursday ahead of holidays), so backtests filter on
it directly with no lookahead. Verified 2026-10-07: US oil rigs 412 on
2025-08-29, 456 on 2026-10-02; both files agree on all 87 overlapping weeks.

Plain curl on Windows dies on a TLS renegotiation; curl_cffi (Chrome
impersonation, already a yfinance dependency) gets through.

Cache: data/cache/rig_count.json, {date: {oil, gas, total}} for the US, merged
and never dropped (committed so CI and backtests have the full history).
"""
from __future__ import annotations

import io
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

BASE = "https://rigcount.bakerhughes.com"
PAGE = BASE + "/na-rig-count"
CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"
CACHE_FILE = CACHE_DIR / "rig_count.json"
RETRY_HOURS = 6  # don't re-download the 7MB workbook more often than this


def _get(url: str, timeout: int = 90):
    try:
        from curl_cffi import requests as creq
        r = creq.get(url, impersonate="chrome", timeout=timeout)
    except ImportError:
        import requests
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r


def _links() -> dict[str, str]:
    """Link text -> absolute URL for every static file on the page."""
    html = _get(PAGE, timeout=40).text
    out = {}
    for href, text in re.findall(r'<a[^>]+href="([^"]*static-files/[^"]+)"[^>]*>(.*?)</a>', html, re.S):
        label = re.sub(r"<[^>]+>|\s+", " ", text).strip()
        out[label] = href if href.startswith("http") else BASE + href
    return out


def _parse_workbook(content: bytes) -> dict[str, dict]:
    """US weekly oil/gas/total rigs keyed by publish date (YYYY-MM-DD)."""
    raw = pd.read_excel(io.BytesIO(content), sheet_name="NAM Weekly", header=None, nrows=40)
    hdr = next(i for i, r in raw.iterrows() if (r.astype(str) == "Country").any())
    df = pd.read_excel(io.BytesIO(content), sheet_name="NAM Weekly", header=hdr)
    val = next(c for c in df.columns if str(c).lower().startswith("rig count"))
    us = df[df["Country"].astype(str).str.upper() == "UNITED STATES"].copy()
    us["d"] = pd.to_datetime(us["US_PublishDate"]).dt.strftime("%Y-%m-%d")
    piv = us.pivot_table(index="d", columns="DrillFor", values=val, aggfunc="sum", fill_value=0)
    out = {}
    for d, row in piv.iterrows():
        out[d] = {
            "oil": int(row.get("Oil", 0)),
            "gas": int(row.get("Gas", 0)),
            "total": int(row.sum()),
        }
    return out


def _load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def refresh(force: bool = False, seed_history: bool = False) -> dict:
    """Download the current workbook when a new weekly print is due and merge
    it into the cache. seed_history also merges the frozen 2013-2025 file
    (needed once; automatic when the cache is empty)."""
    cache = _load_cache()
    weeks = cache.get("weeks", {})
    today = datetime.now(timezone.utc).date()
    if not force and weeks:
        latest = date.fromisoformat(max(weeks))
        if (today - latest).days < 7:
            return cache  # this week's print already cached
        try:
            last_try = datetime.fromisoformat(cache["fetched_at"])
            if datetime.now(timezone.utc) - last_try < timedelta(hours=RETRY_HOURS):
                return cache
        except (KeyError, ValueError):
            pass
    seed_history = seed_history or not weeks
    try:
        links = _links()
        current = next(u for t, u in links.items()
                       if "North America Rig Count Report" in t and "(" not in t)
        # Frozen history first so the current workbook wins on overlapping weeks.
        targets = [u for t, u in links.items()
                   if seed_history and "North America Rig Count" in t and "2013" in t]
        targets.append(current)
        for url in targets:
            parsed = _parse_workbook(_get(url).content)
            weeks.update(parsed)
            print(f"[rigs] {url.rsplit('/', 1)[-1][:8]}: {len(parsed)} weeks, "
                  f"latest {max(parsed)} US oil {parsed[max(parsed)]['oil']}")
    except Exception as e:
        print(f"[rigs] refresh failed: {e}; {'using cache' if weeks else 'no cache -> n/a'}")
    cache = {"fetched_at": datetime.now(timezone.utc).isoformat(),
             "weeks": dict(sorted(weeks.items()))}
    if weeks:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(cache, indent=0), encoding="utf-8")
    return cache


def load_series(field: str = "oil", as_of_date: str | None = None) -> list[tuple[str, int]]:
    """(publish_date, rigs) oldest first, released on/before as_of_date."""
    cutoff = as_of_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    weeks = _load_cache().get("weeks", {})
    return [(d, v[field]) for d, v in sorted(weeks.items()) if d <= cutoff]


if __name__ == "__main__":
    import sys
    refresh(force=True, seed_history="--seed" in sys.argv)
    print(load_series()[-3:])
