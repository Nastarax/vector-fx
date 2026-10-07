"""
One-off backfill of the WTI M1-M12 curve into data/cache/oil_curve_history.json
from Databento CME Globex daily bars (GLBX.MDP3, ohlcv-1d, 2010-06 onward).

yfinance drops expired contracts, so the live curve fetcher can only build
history forward. This fills the past so backtests have the curve signal (e.g.
April 2020). Live runs keep using yfinance; this script is local-only.

Needs DATABENTO_API_KEY in .env and `pip install databento` (local-only, so
not in requirements.txt). Costs ~$1.10 of Databento credit for the full
outright history (checked 2026-10-07 via metadata.get_cost; the script prints
the cost and asks before downloading). The raw download is kept in
data/cache/study/ (gitignored) so re-running never re-bills.

Entries use the same shape and contract-selection rule as the live fetcher
(src/fetchers/oil_curve.py): front = first contract not yet expired per CME's
CL rule, M12 = front + 11 months. Existing yfinance entries are never
overwritten. Closes are the last trade of the UTC day (Databento ohlcv-1d),
not the settlement; fine for a % spread.

    python scripts/backfill_oil_curve.py            # prompt before paying
    python scripts/backfill_oil_curve.py --yes      # no prompt
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.fetchers import oil_curve  # noqa: E402

RAW = ROOT / "data" / "cache" / "study" / "databento_cl_ohlcv1d.csv"
START = "2010-06-06"


def _download(end: str, assume_yes: bool) -> pd.DataFrame:
    import databento as db
    client = db.Historical(os.environ["DATABENTO_API_KEY"])
    syms = [f"CL{m}{y}" for m in oil_curve.MONTH_CODES for y in range(10)]
    kw = dict(dataset="GLBX.MDP3", symbols=syms, stype_in="raw_symbol",
              schema="ohlcv-1d", start=START, end=end)
    cost = client.metadata.get_cost(**kw)
    print(f"[backfill] Databento cost for {START}..{end}: ${cost:.2f}")
    if not assume_yes and input("Download? [y/N] ").strip().lower() != "y":
        sys.exit("aborted")
    df = client.timeseries.get_range(**kw).to_df().reset_index()
    df = df[["ts_event", "symbol", "close", "volume"]]
    RAW.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(RAW, index=False)
    print(f"[backfill] saved {len(df):,} bars -> {RAW}")
    return df


def _full_ticker(raw: str, d: date) -> str:
    """CLZ5 on 2015-03-02 -> CLZ15.NYM. Delivery year is the first year >=
    the bar's year ending in that digit (CL lists < 10 years out)."""
    month = oil_curve.MONTH_CODES.index(raw[2]) + 1
    year = d.year + ((int(raw[3]) - d.year) % 10)
    return oil_curve.ticker(year, month)


def main() -> None:
    load_dotenv(ROOT / ".env")
    end = (pd.Timestamp.now(tz="UTC").normalize()).strftime("%Y-%m-%d")
    if RAW.exists():
        df = pd.read_csv(RAW)
        print(f"[backfill] using cached raw download ({len(df):,} bars)")
    else:
        df = _download(end, "--yes" in sys.argv)

    df["d"] = pd.to_datetime(df["ts_event"], utc=True).dt.date
    df = df[pd.to_datetime(df["d"]).dt.dayofweek < 5]  # drop Sunday-open stubs
    df["tk"] = [_full_ticker(s, d) for s, d in zip(df["symbol"], df["d"])]
    px = df.groupby(["d", "tk"])["close"].last()

    arch = oil_curve._load_archive()
    added = 0
    for d in sorted(set(px.index.get_level_values(0))):
        key = d.isoformat()
        if key in arch:
            continue  # never overwrite live yfinance entries
        fy, fm = oil_curve.front_contract(d)
        my, mm = oil_curve._add_months(fy, fm, 11)
        f_tk, m_tk = oil_curve.ticker(fy, fm), oil_curve.ticker(my, mm)
        try:
            f_px, m_px = float(px[(d, f_tk)]), float(px[(d, m_tk)])
        except KeyError:
            continue  # one leg didn't trade that day
        if f_px <= 0 or m_px <= 0:
            continue  # 2020-04-20: front settled negative; % spread undefined
        arch[key] = {
            "date": key, "front": f_tk, "m12": m_tk,
            "front_px": round(f_px, 2), "m12_px": round(m_px, 2),
            "spread_pct": round((f_px - m_px) / f_px * 100, 3),
            "source": "databento",
        }
        added += 1
    oil_curve.ARCHIVE.write_text(json.dumps(dict(sorted(arch.items())), indent=1), encoding="utf-8")
    print(f"[backfill] added {added:,} days; archive now {len(arch):,} days "
          f"({min(arch)} .. {max(arch)})")


if __name__ == "__main__":
    main()
