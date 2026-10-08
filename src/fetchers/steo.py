"""
EIA Short-Term Energy Outlook (STEO) vintages, for the USOIL regime rows.

Why spreadsheets and not the API: the EIA API v2 `steo` route only serves the
CURRENT forecast vintage, and a forecast revision needs the previous month's
vintage too. EIA keeps every monthly release as a workbook at
  https://www.eia.gov/outlooks/steo/archives/<mon><yy>_base.xlsx
(.xlsx from 2015; older ones are .xls and are skipped). Each workbook states its
own release date (row 4 of every table, e.g. "Thursday, October 1, 2026"), so
backtests only see a vintage on/after the day it was published.

Series codes in the workbooks are lowercase (verified 2026-10-07):
  cops_opec      OPEC total spare crude production capacity, mb/d (table 3c/3d)
  papr_nonopec   total non-OPEC liquids production, mb/d (table 3a)
NB OPEC membership changes (e.g. Angola 2024) shift both series between
vintages, so revisions only ever compare CONSECUTIVE vintages.

Cache: data/cache/steo_vintages.json  {"YYYY-MM": {"released": "YYYY-MM-DD",
"series": {code: {"YYYY-MM": value}}}} (committed).
"""
from __future__ import annotations

import io
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ARCHIVE_URL = "https://www.eia.gov/outlooks/steo/archives/{mon}{yy:02d}_base.xlsx"
CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"
CACHE_FILE = CACHE_DIR / "steo_vintages.json"
SERIES = ("cops_opec", "papr_nonopec")
RETRY_HOURS = 6
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def _get(url: str):
    try:
        from curl_cffi import requests as creq
        return creq.get(url, impersonate="chrome", timeout=90)
    except ImportError:
        import requests
        return requests.get(url, timeout=90, headers={"User-Agent": "Mozilla/5.0"})


def _parse_release(cell) -> str | None:
    if isinstance(cell, datetime):
        return cell.date().isoformat()
    if isinstance(cell, str):
        # Month matched on its first 3 letters: EIA's own cells have typos
        # (aug23_base.xlsx says "Augutst 3, 2023").
        m = re.search(r"([A-Z][a-z]+) (\d{1,2}), (\d{4})", cell)
        if m and m.group(1)[:3].lower() in MONTHS:
            return date(int(m.group(3)), MONTHS.index(m.group(1)[:3].lower()) + 1,
                        int(m.group(2))).isoformat()
    return None


def parse_workbook(content: bytes, vintage: str) -> dict:
    """{"released", "series": {code: {"YYYY-MM": value}}} from one STEO workbook."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    out: dict = {"released": None, "series": {}}
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(max_row=250, values_only=True))
        if len(rows) < 6:
            continue
        hits = [r for r in rows if r and isinstance(r[0], str) and r[0].strip().lower() in SERIES]
        if not hits:
            continue
        out["released"] = out["released"] or _parse_release(rows[3][0])
        years, months = rows[2], rows[3]
        for r in hits:
            code = r[0].strip().lower()
            if code in out["series"]:
                continue
            yr, vals = None, {}
            for y, mo, v in zip(years[2:], months[2:], r[2:]):
                if isinstance(y, (int, float)):
                    yr = int(y)
                if yr is None or not isinstance(mo, str) or not isinstance(v, (int, float)):
                    continue
                m_idx = MONTHS.index(mo.strip()[:3].lower()) + 1
                vals[f"{yr}-{m_idx:02d}"] = round(float(v), 4)
            out["series"][code] = vals
        if len(out["series"]) == len(SERIES) and out["released"]:
            break
    if not out["released"]:
        # Layout without a date cell (seen in some old vintages): assume the 15th,
        # later than any real STEO release day, so a backtest never sees it early.
        out["released"] = f"{vintage}-15"
        out["released_approx"] = True
    return out


def _load() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save(cache: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(dict(sorted(cache.items())), separators=(",", ":")), encoding="utf-8")


def _fetch_vintage(y: int, m: int) -> dict | None:
    vintage = f"{y}-{m:02d}"
    r = _get(ARCHIVE_URL.format(mon=MONTHS[m - 1], yy=y % 100))
    ctype = r.headers.get("content-type", "")
    if r.status_code != 200 or "spreadsheet" not in ctype:
        return None  # not published yet (404 page) or missing
    v = parse_workbook(r.content, vintage)
    if set(v["series"]) != set(SERIES):
        print(f"[steo] {vintage}: series missing in workbook ({sorted(v['series'])})")
        return None
    return v


def refresh(backfill_from: str | None = None) -> dict:
    """Make sure the current and previous month's vintages are cached (the
    current one only once EIA publishes it). backfill_from="2015-01" also
    fills every missing month since then (one-off, ~1MB per vintage)."""
    cache = _load()
    meta = cache.pop("_meta", {})
    today = datetime.now(timezone.utc).date()
    wanted = []
    y, m = (int(x) for x in (backfill_from or "").split("-")) if backfill_from else (None, None)
    if y:
        while (y, m) <= (today.year, today.month):
            wanted.append((y, m))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    else:
        prev = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        wanted = [prev, (today.year, today.month)]
    changed = False
    for y, m in wanted:
        key = f"{y}-{m:02d}"
        if key in cache:
            continue
        last = meta.get("tries", {}).get(key)
        if not backfill_from and last and \
                datetime.now(timezone.utc) - datetime.fromisoformat(last) < timedelta(hours=RETRY_HOURS):
            continue
        try:
            v = _fetch_vintage(y, m)
        except Exception as e:
            print(f"[steo] {key} fetch failed: {e}")
            v = None
        meta.setdefault("tries", {})[key] = datetime.now(timezone.utc).isoformat()
        changed = True
        if v:
            cache[key] = v
            print(f"[steo] vintage {key} released {v['released']}"
                  f"{' (approx)' if v.get('released_approx') else ''}")
    if changed:
        cache["_meta"] = meta
        _save(cache)
    else:
        cache["_meta"] = meta
    return cache


def vintages_as_of(as_of_date: str | None = None) -> list[tuple[str, dict]]:
    """[(vintage, data)] released on/before as_of (today when None), oldest first."""
    cutoff = as_of_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cache = _load()
    return sorted((k, v) for k, v in cache.items()
                  if not k.startswith("_") and v.get("released", "9999") <= cutoff)


def window_avg(series: dict, start: str, months: int = 12) -> float | None:
    """Average of `months` monthly values starting at YYYY-MM `start`."""
    y, m = (int(x) for x in start.split("-"))
    vals = []
    for _ in range(months):
        v = series.get(f"{y}-{m:02d}")
        if v is None:
            return None
        vals.append(v)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return sum(vals) / len(vals)


if __name__ == "__main__":
    import sys
    refresh(backfill_from=sys.argv[1] if len(sys.argv) > 1 else None)
    for k, v in vintages_as_of()[-3:]:
        s = v["series"]["cops_opec"]
        print(k, v["released"], "spare", s.get(k), "| 12m avg", round(window_avg(s, k) or 0, 2))
