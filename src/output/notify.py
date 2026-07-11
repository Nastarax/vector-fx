"""
Bias-change alerts.

After each scoring run, compare every pair's directional bias against the
previous run's bias, saved in data/cache/setup_state.json. When a pair newly
enters a directional bias (Bullish / Very Bullish / Bearish / Very Bearish)
from Neutral, or flips from the bullish side to the bearish side (or back),
send a push notification. A pair that stays on the same side between runs is
not re-alerted, so a persistently-bullish pair only pings once.

Channels, picked by which env vars are set (both optional, both can be on):
  Telegram: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
  Discord:  DISCORD_WEBHOOK_URL
With neither set, changes are only printed to the console. On GH Actions the
secrets are passed as env in .github/workflows/hourly.yml; the state file is
committed back by the workflow so the diff survives between hourly runs.

The first run (no state file yet) only records state and never alerts, so a
fresh checkout can't spam every currently-biased pair. A pair going back to
Neutral is recorded but not alerted.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import requests

STATE_FILE = Path(__file__).resolve().parents[2] / "data" / "cache" / "setup_state.json"

_BULLISH = ("Bullish", "Very Bullish")
_BEARISH = ("Bearish", "Very Bearish")


def _side(bias: str | None) -> str:
    """Collapse a bias label to its direction: 'bull', 'bear', or 'neutral'."""
    if bias in _BULLISH:
        return "bull"
    if bias in _BEARISH:
        return "bear"
    return "neutral"


def _load_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def _send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        ok = r.status_code == 200
        if not ok:
            print(f"[notify] telegram send failed: HTTP {r.status_code} {r.text[:200]}")
        return ok
    except Exception as e:
        print(f"[notify] telegram send failed: {e}")
        return False


def _send_discord(text: str) -> bool:
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        return False
    try:
        r = requests.post(url, json={"content": text}, timeout=10)
        ok = r.status_code in (200, 204)
        if not ok:
            print(f"[notify] discord send failed: HTTP {r.status_code} {r.text[:200]}")
        return ok
    except Exception as e:
        print(f"[notify] discord send failed: {e}")
        return False


def _change_line(sym: str, cur: dict) -> str:
    return f"{sym}: {cur['bias']}"


def check_and_notify(pair_rows: list[dict], state_path: Path | None = None) -> list[str]:
    """
    Diff current biases against the saved ones, alert when a pair newly enters
    a directional bias or flips sides, persist the new states. Returns the list
    of changed symbols (useful for tests; empty on bootstrap or when nothing
    changed direction).
    """
    path = state_path or STATE_FILE
    prev = _load_state(path)

    current: dict[str, dict] = {}
    for r in pair_rows:
        if r.get("is_currency"):
            continue
        current[r["symbol"]] = {
            "setup": r.get("setup"),
            "bias": r.get("bias"),
            "loc": r.get("loc_pct"),
        }

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=1)

    if prev is None:
        print("[notify] no previous bias state; recorded baseline, no alerts")
        return []

    changed = [
        sym for sym, cur in current.items()
        if _side(cur["bias"]) != "neutral"
        and _side(cur["bias"]) != _side((prev.get(sym) or {}).get("bias"))
    ]
    if not changed:
        print("[notify] no new directional biases")
        return []

    lines = [_change_line(sym, current[sym]) for sym in sorted(changed)]
    text = ("Vector: 1 pair changed bias\n" if len(changed) == 1
            else f"Vector: {len(changed)} pairs changed bias\n") + "\n".join(lines)
    print("[notify] " + text.replace("\n", " | "))

    sent_tg = _send_telegram(text)
    sent_dc = _send_discord(text)
    if not (sent_tg or sent_dc):
        print("[notify] no channel configured (set TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID "
              "or DISCORD_WEBHOOK_URL); alert printed only")
    return changed
