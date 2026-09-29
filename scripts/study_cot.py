"""
Isolated long-history study of the COT signal. Read-only.

Question: taken ON ITS OWN, does the CFTC Non-Commercial positioning signal
predict the forward move of that currency, over ~20 years, per market?

Data
  * CFTC Legacy Futures-Only reports, pulled by CONTRACT CODE (not market name:
    USD/GBP/AUD/NZD were renamed several times, so a name query only returns
    ~4.6 years). Cached in data/cache/study/cot_full.json, refreshed weekly.
  * Daily closes from the 28 fiat px_*.pkl caches (yfinance, from 2003-12).
    Each currency's "market" is an equal-weighted basket vs the other 7
    (+ when base, - when quote), same construction as backtest_ic.py.

Timing (no lookahead)
  Positions are as of Tuesday, published Friday 3:30pm ET. Entry is the close
  of the first trading day on or after report_date + 6 (the Monday), then the
  forward return runs H trading days from there.

Signals tested (per currency, weekly)
  live      sign(Long% - prev Long%), strict: exactly what cot_score does today
  live_2pp  same, but |change| <= 2pp counts as 0 (deadband)
  mom4      sign of the 4-week change in Long% (slower momentum)
  level     contrarian extremes: Long% in the top 10% of its trailing 3y range
            -> -1, bottom 10% -> +1, else 0
  The pair cell (base - quote, clamped -2..+2) is tested for `live` too, since
  that is what the heatmap actually shows.

Stats
  Weekly observations with an H-day forward window overlap when H > 5, so
  t-stats use n_eff = n * 5 / H. Everything is split into two halves
  (2004-2014, 2015-now) as a stability check: an edge that exists in one
  half only is not an edge.

Usage
  python scripts/study_cot.py              # horizons 5 10 20 trading days
  python scripts/study_cot.py 5 20 60
  python scripts/study_cot.py --refetch    # force a fresh CFTC pull
"""
from __future__ import annotations

import glob
import json
import math
import os
import sys
import time

import numpy as np
import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CACHE = os.path.join(ROOT, "data", "cache")
STUDY = os.path.join(CACHE, "study")
COT_FILE = os.path.join(STUDY, "cot_full.json")

API = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
FIAT = ["USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD"]
# Stable across exchange renames (verified 2026-09-29).
CODES = {
    "USD": "098662", "EUR": "099741", "GBP": "096742", "JPY": "097741",
    "CHF": "092741", "AUD": "232741", "CAD": "090741", "NZD": "112741",
}
SIGNALS = ["live", "live_2pp", "mom4", "level"]
SPLIT = "2015-01-01"
BAD_TICK = 0.08  # a daily |log return| above 8% on a major FX cross is a yfinance glitch


# ---------------------------------------------------------------- data

def _fetch_cot(refetch: bool) -> dict[str, list[dict]]:
    if (not refetch and os.path.exists(COT_FILE)
            and time.time() - os.path.getmtime(COT_FILE) < 7 * 86400):
        with open(COT_FILE, encoding="utf-8") as f:
            return json.load(f)
    os.makedirs(STUDY, exist_ok=True)
    out = {}
    for ccy, code in CODES.items():
        params = {
            "$where": f"cftc_contract_market_code='{code}'",
            "$select": "report_date_as_yyyy_mm_dd,noncomm_positions_long_all,"
                       "noncomm_positions_short_all",
            "$order": "report_date_as_yyyy_mm_dd ASC",
            "$limit": "10000",
        }
        rows = requests.get(API, params=params, timeout=60).json()
        out[ccy] = [{"date": r["report_date_as_yyyy_mm_dd"][:10],
                     "L": int(float(r["noncomm_positions_long_all"])),
                     "S": int(float(r["noncomm_positions_short_all"]))}
                    for r in rows]
        print(f"[cot] {ccy}: {len(rows)} reports {out[ccy][0]['date']} .. {out[ccy][-1]['date']}")
    with open(COT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f)
    return out


def _cot_signals(raw: dict[str, list[dict]]) -> dict[str, pd.DataFrame]:
    """Per currency: weekly frame indexed by report date with the raw inputs
    and every signal. Duplicate report dates (renames overlap) keep the last."""
    out = {}
    for ccy, rows in raw.items():
        df = pd.DataFrame(rows).drop_duplicates("date", keep="last")
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        tot = df["L"] + df["S"]
        df["long_pct"] = np.where(tot > 0, 100 * df["L"] / tot.where(tot > 0, 1), 50.0)
        d1 = df["long_pct"].diff()
        d4 = df["long_pct"].diff(4)
        df["d1"] = d1
        df["live"] = np.sign(d1)
        df["live_2pp"] = np.where(d1 > 2, 1, np.where(d1 < -2, -1, 0))
        df["mom4"] = np.sign(d4)
        # percentile of the current Long% within the trailing 156 reports
        pct = df["long_pct"].rolling(156, min_periods=104).apply(
            lambda w: (w[:-1] < w[-1]).mean(), raw=True)
        df["level_pct"] = pct
        df["level"] = np.where(pct >= 0.9, -1, np.where(pct <= 0.1, 1, 0))
        df.loc[pct.isna(), "level"] = np.nan
        out[ccy] = df.dropna(subset=["d1"])
    return out


def _pair_closes() -> dict[tuple[str, str], pd.Series]:
    pairs = {}
    for path in glob.glob(os.path.join(CACHE, "px_*.pkl")):
        name = os.path.basename(path)[3:-4]
        if name.endswith("_4h") or len(name) != 6:
            continue
        b, q = name[:3], name[3:]
        if b in FIAT and q in FIAT:
            s = pd.read_pickle(path)["Close"].dropna()
            s.index = pd.to_datetime([d.strftime("%Y-%m-%d") for d in pd.to_datetime(s.index)])
            pairs[(b, q)] = s[~s.index.duplicated(keep="last")]
    return pairs


def _clean_logret(s: pd.Series) -> pd.Series:
    r = np.log(s).diff()
    return r.where(r.abs() < BAD_TICK, 0.0)


def _baskets(pairs) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (ccy basket cumulative log-price, pair cumulative log-price),
    both on the common trading-day grid (dates where >= half the pairs trade)."""
    rets = pd.DataFrame({f"{b}{q}": _clean_logret(s) for (b, q), s in pairs.items()})
    rets = rets[rets.notna().sum(axis=1) >= len(pairs) // 2].fillna(0.0)
    bask = {}
    for c in FIAT:
        cols = []
        for (b, q) in pairs:
            if c == b:
                cols.append(rets[f"{b}{q}"])
            elif c == q:
                cols.append(-rets[f"{b}{q}"])
        bask[c] = pd.concat(cols, axis=1).mean(axis=1)
    return pd.DataFrame(bask).cumsum(), rets.cumsum()


def _fwd(level: pd.DataFrame, report_dates: pd.DatetimeIndex, H: int) -> pd.DataFrame:
    """Forward H-day log return from the entry day of each report (report+6d,
    rolled forward to the next trading day). Rows with no full window -> NaN."""
    grid = level.index
    idx = grid.searchsorted(report_dates + pd.Timedelta(days=6))
    out = np.full((len(report_dates), level.shape[1]), np.nan)
    vals = level.to_numpy()
    for k, i in enumerate(idx):
        if i + H < len(grid):
            out[k] = vals[i + H] - vals[i]
    return pd.DataFrame(out, index=report_dates, columns=level.columns)


# ---------------------------------------------------------------- stats

def _tstat(x: np.ndarray, H: int) -> float:
    n = len(x)
    if n < 10 or x.std(ddof=1) == 0:
        return float("nan")
    n_eff = n * 5 / max(H, 5)
    return x.mean() / (x.std(ddof=1) / math.sqrt(n_eff))


def _ts_stats(sig: pd.Series, ret: pd.Series, H: int) -> dict:
    """Time-series edge: trade the signal's sign, measure the forward return."""
    df = pd.concat([sig, ret], axis=1, keys=["s", "r"]).dropna()
    df = df[df["s"] != 0]
    if len(df) < 10:
        return {}
    pnl = (df["s"] * df["r"]).to_numpy() * 1e4  # bps
    up = df.loc[df["s"] > 0, "r"].mean() * 1e4
    dn = df.loc[df["s"] < 0, "r"].mean() * 1e4
    return {"n": len(df), "bps": pnl.mean(), "t": _tstat(pnl, H),
            "hit": (pnl > 0).mean(), "up": up, "dn": dn}


def _fmt(st: dict) -> str:
    if not st:
        return "    (too few obs)"
    flag = "  <--" if abs(st["t"]) > 2 else ""
    return (f"n={st['n']:5d}  {st['bps']:+6.1f}bp/trade  t={st['t']:+5.2f}  "
            f"hit={st['hit']*100:4.1f}%   (+1: {st['up']:+6.1f}  -1: {st['dn']:+6.1f}){flag}")


def _periods(df_index):
    split = pd.Timestamp(SPLIT)
    return {"all": slice(None), "2004-14": df_index < split, "2015-now": df_index >= split}


# ---------------------------------------------------------------- report

def run(horizons: list[int], refetch: bool) -> None:
    sigs = _cot_signals(_fetch_cot(refetch))
    pairs = _pair_closes()
    bask, pair_lvl = _baskets(pairs)
    start = bask.index[0]
    print("=" * 78)
    print(f"COT ISOLATED STUDY   prices {start.date()} .. {bask.index[-1].date()}   "
          f"{len(pairs)} pairs")
    print("Return = equal-weight basket of the currency vs the other 7. bps per")
    print("weekly trade in the signal's direction. t uses n_eff = n*5/H.")
    print("=" * 78)

    # stack every currency into one long frame per horizon
    for H in horizons:
        rows = []
        for c in FIAT:
            s = sigs[c][sigs[c].index >= start]
            f = _fwd(bask[[c]], s.index, H)[c]
            rows.append(s[SIGNALS + ["d1", "level_pct"]].assign(ret=f, ccy=c))
        long = pd.concat(rows).dropna(subset=["ret"])

        print(f"\n######## H = {H} trading days ########")
        for sig in SIGNALS:
            print(f"\n[{sig}]  pooled 8 ccys")
            for name, mask in _periods(long.index).items():
                sub = long if isinstance(mask, slice) else long[mask]
                print(f"  {name:9s} {_fmt(_ts_stats(sub[sig], sub['ret'], H))}")
            print("  per currency (all years):")
            for c in FIAT:
                sub = long[long["ccy"] == c]
                print(f"    {c}  {_fmt(_ts_stats(sub[sig], sub['ret'], H))}")

        # cross-sectional IC on the continuous inputs, one Spearman per week
        print("\n[cross-sectional IC] rank 8 ccys by the raw input each week")
        for col, label in (("d1", "d Long% (live input)"), ("level_pct", "Long% percentile (level)")):
            ics = []
            for d, g in long.groupby(level=0):
                g = g.dropna(subset=[col])
                if len(g) >= 6 and g[col].nunique() > 1:
                    ics.append(g[col].rank().corr(g["ret"].rank()))
            ics = np.array(ics)
            print(f"  {label:26s} mean IC {ics.mean():+.3f}   t={_tstat(ics, H):+.2f}   "
                  f"n={len(ics)} weeks   IC>0 {(ics > 0).mean()*100:.0f}%")

    # the heatmap cell: pair COT = clamp(base - quote)
    H = horizons[0]
    print(f"\n######## PAIR CELL (live rule, base - quote, clamped), H = {H} ########")
    recs = []
    for (b, q) in pairs:
        sb, sq = sigs[b]["live"], sigs[q]["live"]
        cell = (sb - sq).clip(-2, 2).dropna()
        cell = cell[cell.index >= start]
        f = _fwd(pair_lvl[[f"{b}{q}"]], cell.index, H)[f"{b}{q}"]
        recs.append(pd.DataFrame({"cell": cell, "ret": f}).dropna())
    pc = pd.concat(recs)
    print("  cell   n       mean fwd pair return")
    for v in (-2, -1, 0, 1, 2):
        g = pc[pc["cell"] == v]["ret"] * 1e4
        print(f"  {v:+d}   {len(g):6d}   {g.mean():+6.1f}bp")
    st = _ts_stats(np.sign(pc["cell"]), pc["ret"], H)
    print(f"  trade sign(cell): {_fmt(st)}")


if __name__ == "__main__":
    hs = [int(a) for a in sys.argv[1:] if not a.startswith("--")] or [5, 10, 20]
    run(hs, "--refetch" in sys.argv)
