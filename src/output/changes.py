"""
"What changed" layer for the main heatmap: this week's releases, score change
per row, and cells that flipped since the comparison date.

- recent_releases(): every release in the window from the Economic Heatmap
  rows (econ_data), newest first. Only dates on/before as_of (rate rows carry
  the NEXT meeting date, which is skipped).
- cell_history.json: one snapshot per day of every heatmap row's cell scores
  (pairs, currency rows, USOIL sub-rows), written by live runs only. The latest
  run of a day overwrites that day, so a past entry is end-of-day. Going
  forward only (no backfill: past caches aren't point-in-time).
- annotate(): adds `delta` (total vs the comparison date) and `changed`
  ({indicator id: old score}) to each row, and the same for USOIL. Pair deltas
  fall back to score_history.json's pair totals (recorded since 2026-06-10)
  until cell_history has a week of data. Currency rows don't fall back: the
  history's currency score is the Asset Scorecard total, which is computed
  differently from the heatmap currency row.

Backtests (--date) get the release list only; deltas and markers need live
history from that period, which doesn't exist.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"
CELL_HISTORY = CACHE_DIR / "cell_history.json"
MAX_DAYS = 90
FIAT = ("USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def recent_releases(econ_data: dict | None, as_of: str | None, days: int) -> list[dict]:
    if not econ_data:
        return []
    end = as_of or _today()
    start = (date.fromisoformat(end) - timedelta(days=days)).isoformat()
    out = []
    for ccy in FIAT:
        for r in econ_data.get(ccy) or []:
            d = r.get("date")
            if not d or r.get("actual") is None or not (start < d[:10] <= end):
                continue
            out.append({**r, "ccy": ccy, "date": d[:10]})
    out.sort(key=lambda r: (r["date"], r["ccy"]), reverse=True)
    return out


def _load() -> dict:
    if not CELL_HISTORY.exists():
        return {}
    try:
        return json.loads(CELL_HISTORY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _snapshot(rows: list[dict], oil: dict | None) -> dict:
    snap = {r["symbol"]: {"_total": r["total"], **{k: v for k, v in r["scores"].items() if v is not None}}
            for r in rows}
    if oil and oil.get("score") is not None:
        snap[oil["symbol"]] = {"_total": oil["score"],
                               **{s["id"]: s["score"] for s in oil["rows"] if s["score"] is not None}}
    return snap


def save(rows: list[dict], oil: dict | None) -> None:
    """Live runs only. Overwrites today's entry (latest run of the day wins)."""
    hist = _load()
    hist[_today()] = _snapshot(rows, oil)
    keep = sorted(hist)[-MAX_DAYS:]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CELL_HISTORY.write_text(json.dumps({k: hist[k] for k in keep}, separators=(",", ":")),
                            encoding="utf-8")


def _baseline(hist: dict, days: int) -> tuple[str | None, dict]:
    cutoff = (date.fromisoformat(_today()) - timedelta(days=days)).isoformat()
    past = [d for d in hist if d <= cutoff]
    return (max(past), hist[max(past)]) if past else (None, {})


def _pair_total_fallback(days: int) -> dict[str, tuple[str, int]]:
    from src.scoring.score_history import load_history
    cutoff = (date.fromisoformat(_today()) - timedelta(days=days)).isoformat()
    out = {}
    for sym, entries in load_history().items():
        past = [e for e in entries if e["date"] <= cutoff]
        if past:
            e = max(past, key=lambda x: x["date"])
            out[sym] = (e["date"], e["score"])
    return out


def annotate(heatmap: dict, days: int) -> None:
    """Adds delta / changed / delta_date to rows and to heatmap['oil'] in place."""
    if heatmap.get("as_of_date"):
        return
    base_date, base = _baseline(_load(), days)
    fallback = _pair_total_fallback(days)
    for r in heatmap["rows"]:
        prev = base.get(r["symbol"])
        if prev is not None:
            r["delta"] = r["total"] - prev["_total"]
            r["delta_date"] = base_date
            r["changed"] = {k: prev[k] for k, v in r["scores"].items()
                            if v is not None and k in prev and prev[k] != v}
        elif not r.get("is_currency") and r["symbol"] in fallback:
            d, score = fallback[r["symbol"]]
            r["delta"], r["delta_date"] = r["total"] - score, d
    oil = heatmap.get("oil")
    if oil and oil.get("score") is not None and oil["symbol"] in base:
        prev = base[oil["symbol"]]
        oil["delta"] = oil["score"] - prev["_total"]
        oil["delta_date"] = base_date
        for s in oil["rows"]:
            if s["score"] is not None and s["id"] in prev and prev[s["id"]] != s["score"]:
                s["was"] = prev[s["id"]]
