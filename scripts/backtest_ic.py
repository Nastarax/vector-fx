"""
Currency-level Information Coefficient (IC) backtest for Vector.

Read-only. Touches nothing in the live pipeline: it reads the committed
score history (data/cache/score_history.json) and the price caches
(data/cache/px_*.pkl) and prints a report.

Question it answers: does the per-currency score actually predict the
currency's forward return? We work at the currency level (8 fiat ccys),
not the 28 pairs, because that is where the signal lives and it avoids
double-counting the cross.

Per-currency return = equal-weighted basket of that currency against every
other currency it has a pair for. If the currency is the base of the pair we
take +log-return, if it is the quote we take -log-return, then average. That
isolates the single-currency factor from any one cross.

Metrics:
  * IC per snapshot date = Spearman rank correlation between the 8 scores and
    their forward basket returns. Reported as mean IC, its t-stat, and the
    hit rate (share of dates with IC > 0). Rule of thumb: a stable mean IC of
    ~0.05+ with t > 2 is a real edge; near zero is noise.
  * Return-by-score buckets: pooled forward return grouped by score bucket,
    to see whether high scores actually out-return low scores (monotonicity).
  * Long-top / short-bottom: each date go long the highest-scored ccy and
    short the lowest, average the spread. A crude tradability proxy.

Usage:
  python scripts/backtest_ic.py            # default horizons 1,3,5
  python scripts/backtest_ic.py 1 5 10     # custom forward horizons (snapshots)

NOTE: horizons are measured in SCORE SNAPSHOTS (roughly trading days), not
calendar days, because that is the grid the score history lives on.
"""
from __future__ import annotations

import glob
import json
import math
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CACHE = os.path.join(ROOT, "data", "cache")

FIAT = ["USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD"]


def _load_scores() -> dict[str, dict[str, int]]:
    """Return {ccy: {date_str: score}} for the 8 fiat currencies."""
    with open(os.path.join(CACHE, "score_history.json"), encoding="utf-8") as f:
        raw = json.load(f)
    out: dict[str, dict[str, int]] = {}
    for ccy in FIAT:
        hist = raw.get(ccy, [])
        out[ccy] = {row["date"]: row["score"] for row in hist if row.get("score") is not None}
    return out


def _load_pair_closes() -> dict[tuple[str, str], pd.Series]:
    """Return {(base, quote): Series indexed by date-string -> Close} for every
    fiat-vs-fiat pair cache. Index is the plain YYYY-MM-DD date string so it
    lines up with the score snapshots."""
    pairs: dict[tuple[str, str], pd.Series] = {}
    for path in glob.glob(os.path.join(CACHE, "px_*.pkl")):
        name = os.path.basename(path)[3:-4]  # strip "px_" and ".pkl"
        if name.endswith("_4h") or len(name) != 6:
            continue
        base, quote = name[:3], name[3:]
        if base not in FIAT or quote not in FIAT:
            continue
        df = pd.read_pickle(path)
        s = df["Close"].dropna()
        # collapse tz-aware datetime index to date string
        s.index = [d.strftime("%Y-%m-%d") for d in pd.to_datetime(s.index)]
        s = s[~s.index.duplicated(keep="last")]
        pairs[(base, quote)] = s
    return pairs


def _close_asof(series: pd.Series, date: str) -> float | None:
    """Last available close on or before `date`."""
    sub = series[series.index <= date]
    return float(sub.iloc[-1]) if len(sub) else None


def _basket_return(pairs, ccy: str, d0: str, d1: str) -> float | None:
    """Equal-weighted log return of `ccy` vs all its fiat crosses, d0 -> d1."""
    legs = []
    for (base, quote), series in pairs.items():
        if ccy not in (base, quote):
            continue
        c0 = _close_asof(series, d0)
        c1 = _close_asof(series, d1)
        if not c0 or not c1 or c0 <= 0 or c1 <= 0:
            continue
        r = math.log(c1 / c0)
        legs.append(r if base == ccy else -r)
    if not legs:
        return None
    return sum(legs) / len(legs)


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman rank correlation = Pearson on ranks. None if degenerate."""
    n = len(xs)
    if n < 3:
        return None
    rx = pd.Series(xs).rank().tolist()
    ry = pd.Series(ys).rank().tolist()
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx <= 0 or vy <= 0:
        return None
    return cov / math.sqrt(vx * vy)


def run(horizons: list[int]) -> None:
    scores = _load_scores()
    pairs = _load_pair_closes()

    # common ordered snapshot dates (present for all 8 ccys)
    date_sets = [set(scores[c].keys()) for c in FIAT]
    dates = sorted(set.intersection(*date_sets))
    print(f"Fiat currencies: {', '.join(FIAT)}")
    print(f"Snapshot dates : {len(dates)}  ({dates[0]} .. {dates[-1]})")
    print(f"Pair caches    : {len(pairs)} fiat crosses")
    print("=" * 64)

    bucket_pool: dict[str, list[float]] = {}  # score-bucket -> fwd returns (h=first horizon)

    for hi, H in enumerate(horizons):
        ics, spreads = [], []
        for i in range(len(dates) - H):
            d0, d1 = dates[i], dates[i + H]
            sc, fr = [], []
            for c in FIAT:
                ret = _basket_return(pairs, c, d0, d1)
                if ret is None:
                    continue
                sc.append(scores[c][d0])
                fr.append(ret)
            ic = _spearman(sc, fr)
            if ic is not None:
                ics.append(ic)
            # long-top / short-bottom spread
            if len(sc) >= 2:
                hi_ret = fr[max(range(len(sc)), key=lambda k: sc[k])]
                lo_ret = fr[min(range(len(sc)), key=lambda k: sc[k])]
                spreads.append(hi_ret - lo_ret)
            # pool buckets only for the first (shortest) horizon
            if hi == 0:
                for s, r in zip(sc, fr):
                    b = _bucket(s)
                    bucket_pool.setdefault(b, []).append(r)

        _report_horizon(H, ics, spreads)

    _report_buckets(horizons[0], bucket_pool)


def _bucket(score: int) -> str:
    if score >= 5:
        return "  >= +5 (bullish)"
    if score >= 1:
        return "  +1..+4"
    if score <= -5:
        return "  <= -5 (bearish)"
    if score <= -1:
        return "  -1..-4"
    return "   0 (neutral)"


def _report_horizon(H: int, ics: list[float], spreads: list[float]) -> None:
    print(f"\nHorizon H={H} snapshot(s) forward")
    if not ics:
        print("  not enough data")
        return
    n = len(ics)
    mean = sum(ics) / n
    var = sum((x - mean) ** 2 for x in ics) / (n - 1) if n > 1 else 0.0
    sd = math.sqrt(var)
    t = mean / (sd / math.sqrt(n)) if sd > 0 else float("nan")
    hit = sum(1 for x in ics if x > 0) / n
    print(f"  IC mean      : {mean:+.3f}   (n={n} dates)")
    print(f"  IC std       : {sd:.3f}")
    print(f"  IC t-stat    : {t:+.2f}   {'<-- significant' if abs(t) > 2 else ''}")
    print(f"  IC hit rate  : {hit*100:.0f}% of dates IC>0")
    if spreads:
        sm = sum(spreads) / len(spreads)
        print(f"  top-bottom   : {sm*100:+.3f}% avg long-best / short-worst spread")


def _report_buckets(H: int, pool: dict[str, list[float]]) -> None:
    print("\n" + "=" * 64)
    print(f"Return-by-score buckets (H={H}, pooled across dates)")
    print("  score bucket        n    mean fwd ret")
    order = ["  >= +5 (bullish)", "  +1..+4", "   0 (neutral)", "  -1..-4", "  <= -5 (bearish)"]
    for b in order:
        rs = pool.get(b, [])
        if not rs:
            print(f"{b:<20} {0:>4}    --")
            continue
        m = sum(rs) / len(rs)
        print(f"{b:<20} {len(rs):>4}    {m*100:+.3f}%")
    print("\nMonotonic top>bottom return = signal has edge. Flat/inverted = "
          "rethink weights or thresholds. Sample is tiny for now; it compounds "
          "as score_history.json grows each run.")


if __name__ == "__main__":
    hs = [int(a) for a in sys.argv[1:]] or [1, 3, 5]
    run(hs)
