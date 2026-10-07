"""
WTI futures curve: front month (M1) vs ~12th month (M12), for the USOIL row.

Source: yfinance individual NYMEX contracts, e.g. CLX26.NYM (Nov 2026). Verified
2026-10-06: live contracts return daily history back to ~2018, but EXPIRED
contracts 404 (CLJ25.NYM, CLH26.NYM). There is no free historical curve source
(EIA's RCLC1-4 stopped 2024-04-05; Stooq and Nasdaq CHRIS are bot-walled), so:
  - live runs compute the spread and append it to data/cache/oil_curve_history.json
    (committed) so backtest coverage grows going forward;
  - a backtest date uses yfinance when its M12 contract is still listed, else
    the archive, else returns None (cell shows "-", left out of the composite).

Rolls: the front contract is derived from CME's CL expiry rule (trading ends 3
business days before the 25th calendar day of the month before delivery; if
the 25th is not a business day, 3 business days before the last business day
preceding the 25th). M12 = front + 11 months.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from src.fetchers import prices
from src.fetchers.eia import _holidays

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"
ARCHIVE = CACHE_DIR / "oil_curve_history.json"
MONTH_CODES = "FGHJKMNQUVXZ"


def _is_bday(d: date) -> bool:
    return d.weekday() < 5 and d not in _holidays()


def last_trade_date(year: int, month: int) -> date:
    """Last trading day of the CL contract for delivery month (year, month)."""
    py, pm = (year - 1, 12) if month == 1 else (year, month - 1)
    d = date(py, pm, 25)
    while not _is_bday(d):
        d -= timedelta(days=1)
    n = 0
    while n < 3:
        d -= timedelta(days=1)
        if _is_bday(d):
            n += 1
    return d


def _add_months(year: int, month: int, k: int) -> tuple[int, int]:
    m = month - 1 + k
    return year + m // 12, m % 12 + 1


def front_contract(on: date) -> tuple[int, int]:
    """Delivery (year, month) of the front contract trading on `on`."""
    y, m = _add_months(on.year, on.month, 1)
    while last_trade_date(y, m) < on:
        y, m = _add_months(y, m, 1)
    return y, m


def ticker(year: int, month: int) -> str:
    return f"CL{MONTH_CODES[month - 1]}{year % 100:02d}.NYM"


def _close_on(df: pd.DataFrame, on: date) -> tuple[str, float] | None:
    if df is None or df.empty:
        return None
    s = df["Close"].dropna()
    s = s[s.index.date <= on]
    if s.empty or (on - s.index[-1].date()).days > 5:
        return None
    return s.index[-1].date().isoformat(), float(s.iloc[-1])


def _contract_px(y: int, m: int, on: date) -> pd.DataFrame | None:
    # Expired contracts 404 on yfinance; skip the retry loop entirely.
    if last_trade_date(y, m) < datetime.now(timezone.utc).date():
        return None
    t = ticker(y, m)
    df = prices.fetch_instrument(t, t, period="max")
    return df if not df.empty else None


def _load_archive() -> dict:
    if not ARCHIVE.exists():
        return {}
    try:
        return json.loads(ARCHIVE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _archive(entry: dict) -> None:
    arch = _load_archive()
    arch[entry["date"]] = entry
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE.write_text(json.dumps(dict(sorted(arch.items())), indent=1), encoding="utf-8")


def fetch_curve(front_df: pd.DataFrame, as_of_date: str | None = None) -> dict | None:
    """
    Returns {"date", "front", "m12", "front_px", "m12_px", "spread_pct", "source"}
    or None. spread_pct = (M1 - M12) / M1 * 100; positive = backwardation.
    `front_df` is the CL=F daily frame (already trimmed to as_of_date), used as
    the M1 price when the front contract itself is unavailable (expired).
    """
    on = (datetime.strptime(as_of_date, "%Y-%m-%d").date() if as_of_date
          else datetime.now(timezone.utc).date())
    fy, fm = front_contract(on)
    my, mm = _add_months(fy, fm, 11)

    m12 = _close_on(_contract_px(my, mm, on), on)
    if m12 is not None:
        m1 = _close_on(_contract_px(fy, fm, on), on) or _close_on(front_df, on)
        if m1 is not None:
            entry = {
                "date": m12[0],
                "front": ticker(fy, fm), "m12": ticker(my, mm),
                "front_px": round(m1[1], 2), "m12_px": round(m12[1], 2),
                "spread_pct": round((m1[1] - m12[1]) / m1[1] * 100, 3),
                "source": "yfinance",
            }
            if not as_of_date:
                _archive(entry)
            return entry

    # Fallback: archived snapshot within 5 days of the requested date.
    arch = _load_archive()
    past = [k for k in arch if k <= on.isoformat()]
    if past:
        k = max(past)
        if (on - date.fromisoformat(k)).days <= 5:
            return {**arch[k], "source": "archive"}
    print(f"[oil-curve] no curve data for {on} (M12 {ticker(my, mm)} expired, no archive)")
    return None


if __name__ == "__main__":
    print(fetch_curve(prices.fetch_instrument("USOIL", "CL=F")))
