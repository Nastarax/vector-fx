"""
Daily score history for the Asset Scorecard's "Score over time" chart.

Saves one snapshot per calendar day. Each run of main.py calls save_snapshot()
which appends today's score if not already recorded. Keeps up to 90 days.

File: data/cache/score_history.json
Format: {symbol: [{date: "YYYY-MM-DD", score: int}, ...], ...}
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

        history[symbol].append({
            "date": today,
            "score": sc["total_score"],
        })
        history[symbol] = sorted(history[symbol], key=lambda x: x["date"])[-MAX_DAYS:]
        added += 1

    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f)

    print(f"[score-history] snapshot {today}: {len(scorecards)} symbols"
          + (f", {added} new" if added else ", already recorded"))
