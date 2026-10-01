"""
Isolated long-history study of the TREND cell. Read-only.

Question: taken ON ITS OWN, does Vector's trend cell predict the pair's next
move, over ~22 years, per pair?

Data
  Daily closes from the 28 fiat px_*.pkl caches (yfinance, from 2003-12).
  Daily |log return| above 8% is treated as a yfinance glitch and zeroed.
  4H history only goes back 90 days on yfinance, so everything here is daily.
  That matches the live FX rule, which ignores the 4H chart anyway.

Timing (no lookahead)
  Signal uses closes up to day t. Entry is the close of day t+1, so there is a
  full day between the signal and the fill. Forward return runs H trading days
  from the entry.

Signals (each a direction per pair per day)
  live      trend_score exactly: SMA3 vs SMA14 crossover (+-2), cut to +-1 when
            the SMA14 slope disagrees. Traded by sign; also bucketed by value.
  cross     sign(SMA3 - SMA14) alone
  slope     sign(SMA14 today - SMA14 yesterday) = sign(C_t - C_t-14), i.e.
            plain 14-day momentum
  align     the equity-index rule on the daily chart only: price vs SMA
            20/50/200, (above - below)/3 -> -2..+2. Slow trend for comparison.
  sma200    sign(price - SMA200), the classic slow trend filter

Stats
  The 28 pairs move together (they share 8 currencies), so pooling them as
  independent observations would inflate t. Instead each day's trades are
  averaged into one equal-weight portfolio return, and t is computed on that
  daily series with n_eff = days / H (overlapping windows). Results are split
  2004-14 / 2015-now; an edge in one half only is not an edge.

Usage
  python scripts/study_trend.py              # horizons 1 5 10 20
  python scripts/study_trend.py 5 20 60
"""
from __future__ import annotations

import glob
import math
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CACHE = os.path.join(ROOT, "data", "cache")

FIAT = ["USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD"]
SIGNALS = ["live", "cross", "slope", "align", "sma200"]
SPLIT = pd.Timestamp("2015-01-01")
BAD_TICK = 0.08


def _pairs() -> dict[str, pd.Series]:
    out = {}
    for path in sorted(glob.glob(os.path.join(CACHE, "px_*.pkl"))):
        name = os.path.basename(path)[3:-4]
        if name.endswith("_4h") or len(name) != 6:
            continue
        if name[:3] in FIAT and name[3:] in FIAT:
            s = pd.read_pickle(path)["Close"].dropna()
            s.index = pd.to_datetime([d.strftime("%Y-%m-%d") for d in pd.to_datetime(s.index)])
            out[name] = s[~s.index.duplicated(keep="last")]
    return out


def _signals(c: pd.Series) -> pd.DataFrame:
    """All signals as of each day's close. Mirrors trend_score line for line
    for `live` (score_technical.py)."""
    sma3 = c.rolling(3).mean()
    sma14 = c.rolling(14).mean()
    slope = np.where(sma14 > sma14.shift(1), 1, -1)
    cross = np.where(sma3 > sma14, 2, -2)
    live = np.where((cross > 0) & (slope < 0), cross - 1,
                    np.where((cross < 0) & (slope > 0), cross + 1, cross))
    votes = sum(np.sign(c - c.rolling(w).mean()) for w in (20, 50, 200))
    ratio = votes / 3
    align = np.select([ratio >= 0.5, ratio > 0, ratio <= -0.5, ratio < 0], [2, 1, -2, -1], 0)
    df = pd.DataFrame({
        "live": live, "cross": np.sign(cross), "slope": slope,
        "align": align, "sma200": np.sign(c - c.rolling(200).mean()),
    }, index=c.index)
    df.iloc[:200] = np.nan  # warm-up: every signal needs its full window
    return df


def _fwd(c: pd.Series, H: int) -> pd.Series:
    r = np.log(c).diff()
    lvl = r.where(r.abs() < BAD_TICK, 0.0).fillna(0.0).cumsum()
    return lvl.shift(-(1 + H)) - lvl.shift(-1)


def _tstat(x: np.ndarray, H: int) -> float:
    x = x[~np.isnan(x)]
    if len(x) < 20 or x.std(ddof=1) == 0:
        return float("nan")
    return x.mean() / (x.std(ddof=1) / math.sqrt(len(x) / H))


def _row(port: pd.Series, H: int) -> str:
    x = port.dropna().to_numpy() * 1e4
    if len(x) < 20:
        return "(too few obs)"
    t = _tstat(x, H)
    ann = x.mean() * 252 / H  # rough: non-overlapping H-day trades per year
    flag = "  <--" if abs(t) > 2 else ""
    return (f"{x.mean():+6.2f}bp/trade  t={t:+5.2f}  hit={(x > 0).mean()*100:4.1f}%  "
            f"~{ann:+5.0f}bp/yr  days={len(x)}{flag}")


def run(horizons: list[int]) -> None:
    pairs = _pairs()
    sig = {p: _signals(c) for p, c in pairs.items()}
    first = min(c.index[0] for c in pairs.values())
    last = max(c.index[-1] for c in pairs.values())
    print("=" * 80)
    print(f"TREND ISOLATED STUDY   {first.date()} .. {last.date()}   {len(pairs)} FX pairs")
    print("Trade each pair in the signal's direction, entry next day's close.")
    print("Per day, all pairs averaged into one equal-weight portfolio; t on that")
    print("daily series with n_eff = days/H. ~bp/yr assumes no costs.")
    print("=" * 80)

    for H in horizons:
        fwd = pd.DataFrame({p: _fwd(c, H) for p, c in pairs.items()})
        print(f"\n######## H = {H} trading day(s) ########")
        for name in SIGNALS:
            s = pd.DataFrame({p: np.sign(sig[p][name]) for p in pairs}).reindex(fwd.index)
            pnl = (s * fwd).where(s != 0)
            port = pnl.mean(axis=1)
            print(f"\n[{name}]")
            print(f"  all       {_row(port, H)}")
            print(f"  2004-14   {_row(port[port.index < SPLIT], H)}")
            print(f"  2015-now  {_row(port[port.index >= SPLIT], H)}")
            if name == "live":
                # per pair, all years: is the result broad or a few pairs?
                per = {p: _tstat(pnl[p].to_numpy() * 1e4, H) for p in pairs}
                pos = sum(1 for v in per.values() if v > 0)
                best = sorted(per.items(), key=lambda kv: kv[1])
                print(f"  per pair: {pos}/{len(per)} positive   "
                      f"worst {best[0][0]} t={best[0][1]:+.2f}   "
                      f"best {best[-1][0]} t={best[-1][1]:+.2f}")

        # live by score value: do +-2 cells beat +-1?
        lv = pd.DataFrame({p: sig[p]["live"] for p in pairs}).reindex(fwd.index)
        print("\n  live score buckets (pooled pair-days, mean fwd pair return)")
        for v in (-2, -1, 1, 2):
            vals = fwd.where(lv == v).stack().dropna().to_numpy() * 1e4
            print(f"    {v:+d}   n={len(vals):7d}   {vals.mean():+6.2f}bp")


if __name__ == "__main__":
    hs = [int(a) for a in sys.argv[1:] if not a.startswith("--")] or [1, 5, 10, 20]
    run(hs)
