"""
Daily score history for the Asset Scorecard's "Score over time" chart.

Saves one snapshot per calendar day. Each run of main.py calls save_snapshot()
which appends today's score if not already recorded. Keeps up to 90 days.

File: data/cache/score_history.json
Format: {symbol: [{date: "YYYY-MM-DD", score: int, sub: {...}}, ...], ...}

`sub` holds the per-component sub-scores (technical, sentiment_cot,
fundamentals, growth, inflation, jobs) for IC attribution
(scripts/backtest_ic.py --subs). It is written going forward only; older
entries that predate this change carry just `score`, and the harness skips
them. We intentionally do NOT backfill sub-scores from current macro caches:
those are current-only, so a backfill would inject lookahead bias.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"
HISTORY_FILE = CACHE_DIR / "score_history.json"
MAX_DAYS = 90


def load_history() -> dict[str, list[dict]]:
    if not HISTORY_FILE.exists():
        return {}
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def save_snapshot(scorecards: dict, date_str: str | None = None):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    today = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    history = load_history()
    added = 0

    for symbol, sc in scorecards.items():
        if symbol not in history:
            history[symbol] = []

        dates = {entry["date"] for entry in history[symbol]}
        if today in dates:
            continue

        entry = {
            "date": today,
            "score": sc["total_score"],
        }
        # Per-component sub-scores for IC attribution (currencies carry these;
        # some symbols may not, so guard). Stored under "sub" going forward.
        sub = sc.get("sub_scores")
        if isinstance(sub, dict):
            entry["sub"] = {
                k: sub[k] for k in (
                    "technical", "sentiment_cot", "fundamentals",
                    "growth", "inflation", "jobs",
                    # cot/crowd are the two halves of sentiment_cot, recorded
                    # from 2026-07-21 so the harness can attribute the blend.
                    "cot", "crowd",
                ) if k in sub
            }
        history[symbol].append(entry)
        history[symbol] = sorted(history[symbol], key=lambda x: x["date"])[-MAX_DAYS:]
        added += 1

    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f)

    print(f"[score-history] snapshot {today}: {len(scorecards)} symbols"
          + (f", {added} new" if added else ", already recorded"))


def save_pair_snapshot(pair_rows: list[dict], date_str: str | None = None):
    """
    Record per-pair score + bias + range location + setup state, one snapshot
    per day, into the same history file. Pair symbols (EURUSD, NIKKEI, ...)
    live alongside the currency/asset entries (USD, NKY, ...) and are ignored
    by the currency-level IC harness, which only reads the 8 fiat keys.

    Purpose: validate the Location/WATCH entry filter once enough history
    accumulates (do bias+pullback entries outperform bias+extended?). Like
    sub-scores, loc/setup are recorded going forward only; they cannot be
    backfilled without lookahead bias.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    today = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    history = load_history()
    added = 0

    for r in pair_rows:
        if r.get("is_currency"):
            continue  # currency rows are recorded by save_snapshot
        symbol = r["symbol"]
        entries = history.setdefault(symbol, [])
        if any(e["date"] == today for e in entries):
            continue

        entry = {"date": today, "score": r["total"], "bias": r["bias"]}
        if r.get("loc_pct") is not None:
            entry["loc"] = r["loc_pct"]
        if r.get("setup"):
            entry["setup"] = r["setup"]
        entries.append(entry)
        history[symbol] = sorted(entries, key=lambda x: x["date"])[-MAX_DAYS:]
        added += 1

    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f)

    n_pairs = sum(1 for r in pair_rows if not r.get("is_currency"))
    print(f"[score-history] pair snapshot {today}: {n_pairs} pairs"
          + (f", {added} new" if added else ", already recorded"))
