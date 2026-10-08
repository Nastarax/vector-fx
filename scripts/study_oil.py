"""
Forward-return study of the USOIL row, per signal and composite. Read-only.

Question: does each USOIL signal, and the composite, predict WTI's next move?

Scores
  Every Friday from 2010-07 the row is scored by the LIVE code
  (score_oil.build_oil) using only data released on/before that Friday: EIA
  by release date, COT by Friday release (disaggregated history pulled once,
  same disagg_reading as live), rigs by publish date, curve from
  oil_curve_history.json (Databento backfill + live yfinance), trend and
  location on CL=F closes up to that day.

Returns (no lookahead, no roll artefacts)
  Entry is the first trading close AFTER the scoring Friday (normally Monday),
  so Friday-afternoon releases (rigs 1pm, COT 3:30pm ET) can't leak into the
  fill. Returns come from HOLDING actual contracts (Databento daily closes),
  front month rolled ROLL_BDAYS business days before its last trade date. Not
  CL=F: its stitched series jumps at every roll by the M1-M2 spread, which is
  mechanically tied to the curve signal and would contaminate it.

Stats
  One asset, so IC is time-series: Spearman(score, forward return) across
  dates. Weekly scores with H-week forward windows overlap H-fold, so t uses
  n_eff = n / H. Also: mean return traded by sign, hit rate, and the 2010-17 vs
  2018-now halves (an edge in one half only is not an edge). `cheat` = sign of
  the 1-week forward return: a lookahead control that must light up.

Usage
  python scripts/study_oil.py            # horizons 1 2 4 8 (weeks)
  python scripts/study_oil.py 4 13
Needs data/cache/study/databento_cl_ohlcv1d.csv (scripts/backfill_oil_curve.py).
"""
from __future__ import annotations

import functools
import json
import math
import os
import sys
import urllib.parse
from datetime import date, timedelta

import numpy as np
import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from src.fetchers import baker_hughes, cot, eia, oil_curve, prices  # noqa: E402
from src.scoring import score_oil  # noqa: E402

RAW = os.path.join(ROOT, "data", "cache", "study", "databento_cl_ohlcv1d.csv")
OUT = os.path.join(ROOT, "data", "cache", "study", "oil_signals.csv")
START = date(2010, 7, 2)
SPLIT = pd.Timestamp("2018-01-01")
ROLL_BDAYS = 5


# ---------- returns: rolled front-month contract index ----------

def _contract_closes() -> dict[str, pd.Series]:
    df = pd.read_csv(RAW)
    df["d"] = pd.to_datetime(pd.to_datetime(df["ts_event"], utc=True).dt.date)
    df = df[df["d"].dt.dayofweek < 5]
    df["tk"] = [oil_curve.ticker(d.year + ((int(s[3]) - d.year) % 10),
                                 oil_curve.MONTH_CODES.index(s[2]) + 1)
                for s, d in zip(df["symbol"], df["d"])]
    return {tk: g.groupby("d")["close"].last() for tk, g in df.groupby("tk")}


def _held(d: date) -> tuple[int, int]:
    """Front contract, rolled ROLL_BDAYS business days before its expiry."""
    y, m = oil_curve.front_contract(d)
    ltd = oil_curve.last_trade_date(y, m)
    bd = 0
    x = d
    while x < ltd:
        x += timedelta(days=1)
        if oil_curve._is_bday(x):
            bd += 1
    return oil_curve._add_months(y, m, 1) if bd < ROLL_BDAYS else (y, m)


def _rolled_index(closes: dict[str, pd.Series]) -> pd.Series:
    days = sorted(set().union(*[s.index for s in closes.values()]))
    level, out, held = 1.0, {}, None
    for t0, t1 in zip(days[:-1], days[1:]):
        if held is None:
            out[t0] = level
        tk = oil_curve.ticker(*_held(t0.date()))  # position chosen at t0's close
        s = closes.get(tk)
        if s is not None and t0 in s.index and t1 in s.index and s[t0] > 0 and s[t1] > 0:
            level *= s[t1] / s[t0]
        held = tk
        out[t1] = level
    return pd.Series(out)


# ---------- scores: live scorer on each Friday ----------

def _memoize_caches() -> None:
    eia_cache = eia._load_cache()
    eia._load_cache = lambda: eia_cache
    eia.release_date = functools.lru_cache(maxsize=None)(eia.release_date)
    bh_cache = baker_hughes._load_cache()
    baker_hughes._load_cache = lambda: bh_cache


def _cot_history(code: str) -> list[dict]:
    params = {"$where": f"cftc_contract_market_code = '{code}'",
              "$order": "report_date_as_yyyy_mm_dd DESC", "$limit": "5000"}
    r = requests.get(f"{cot.CFTC_DISAGG_API}?{urllib.parse.urlencode(params)}", timeout=60)
    r.raise_for_status()
    return r.json()


def _scores(fridays: list[date]) -> pd.DataFrame:
    cfg = score_oil.load_cfg()
    _memoize_caches()
    cl = prices.fetch_instrument(cfg["instrument"]["symbol"], cfg["instrument"]["yf_ticker"])
    cot_rows = _cot_history(cfg["cot"]["contract_code"])
    lag = cfg["cot"]["release_lag_days"]
    arch = oil_curve._load_archive()
    keys = sorted(arch)
    sig_ids = [s["id"] for s in cfg["signals"] if s.get("role", "signal") == "signal"]
    recs = []
    for f in fridays:
        ds = f.isoformat()
        i = np.searchsorted(keys, ds, side="right") - 1
        curve = arch[keys[i]] if i >= 0 and (f - date.fromisoformat(keys[i])).days <= 5 else None
        df = cl.loc[cl.index <= pd.Timestamp(ds, tz=cl.index.tz)]
        o = score_oil.build_oil(df, cot.disagg_reading("USOIL", cot_rows, ds, lag), curve,
                                as_of_date=ds, cfg=cfg)
        rec = {"date": pd.Timestamp(f), "composite": o["score_rounded"], "mean": o["mean"]}
        rec.update({r["id"]: r["score"] for r in o["rows"] if r["id"] in sig_ids})
        recs.append(rec)
    return pd.DataFrame(recs).set_index("date"), sig_ids


# ---------- stats ----------

def _spear(x: pd.Series, y: pd.Series) -> float:
    """Spearman = Pearson on ranks (avoids a scipy dependency)."""
    return x.rank().corr(y.rank())


def _t(ic: float, n_eff: float) -> float:
    if n_eff <= 3 or abs(ic) >= 1:
        return float("nan")
    return ic * math.sqrt((n_eff - 2) / (1 - ic * ic))


def _line(name: str, x: pd.Series, y: pd.Series, H: int) -> str:
    m = x.notna() & y.notna()
    x, y = x[m].astype(float), y[m]
    n = len(x)
    if n < 20 or x.nunique() < 2:
        return f"  {name:16s} n={n:4d}  (too little data)"
    ic = _spear(x, y)
    halves = []
    for part in (x.index < SPLIT, x.index >= SPLIT):
        xs, ys = x[part], y[part]
        halves.append(_spear(xs, ys) if len(xs) >= 20 and xs.nunique() > 1 else float("nan"))
    sgn = np.sign(x)
    traded = (sgn * y)[sgn != 0]
    hit = (traded > 0).mean() if len(traded) else float("nan")
    tr_t = traded.mean() / traded.std() * math.sqrt(len(traded) / H) if len(traded) > 3 else float("nan")
    return (f"  {name:16s} n={n:4d}  IC {ic:+.3f} t_adj {_t(ic, n / H):+5.2f}  "
            f"| 2010-17 {halves[0]:+.3f}  2018+ {halves[1]:+.3f}  "
            f"| by sign {traded.mean() * 100:+6.2f}%/trade t {tr_t:+5.2f} hit {hit * 100:4.0f}% (n={len(traded)})")


def run(horizons: list[int]) -> None:
    if not os.path.exists(RAW):
        sys.exit("missing Databento raw file: run scripts/backfill_oil_curve.py first")
    idx = _rolled_index(_contract_closes())
    last = idx.index[-1].date()
    fridays = []
    f = START
    while f <= last - timedelta(weeks=max(horizons)) - timedelta(days=7):
        fridays.append(f)
        f += timedelta(days=7)
    print(f"[oil-study] scoring {len(fridays)} Fridays {fridays[0]}..{fridays[-1]} with the live scorer...")
    sc, sig_ids = _scores(fridays)

    tdays = idx.index
    for H in horizons:
        fwd = {}
        for d in sc.index:
            k = tdays.searchsorted(d, side="right")  # first close strictly after Friday
            if k + 5 * H < len(tdays):
                fwd[d] = idx.iloc[k + 5 * H] / idx.iloc[k] - 1
        sc[f"fwd{H}"] = pd.Series(fwd)
    sc["cheat"] = np.sign(sc[f"fwd{horizons[0]}"])
    sc.to_csv(OUT)

    for H in horizons:
        y = sc[f"fwd{H}"]
        print(f"\n=== H = {H} week(s)  (entry next close after Friday; t uses n_eff = n/{H}) ===")
        for name in sig_ids + ["composite", "mean"] + (["cheat"] if H == horizons[0] else []):
            print(_line(name, sc[name], y, H))

    H = 4 if 4 in horizons else horizons[0]
    y = sc[f"fwd{H}"]
    print(f"\nMean {H}-week forward return by score (n):")
    print("  " + " " * 16 + "".join(f"{v:>15d}" for v in (-2, -1, 0, 1, 2)))
    for name in sig_ids + ["composite"]:
        cells = []
        for v in (-2, -1, 0, 1, 2):
            r = y[sc[name] == v].dropna()
            cells.append(f"{r.mean() * 100:+6.2f}% ({len(r):3d})" if len(r) else f"{'-':>14s}")
        print(f"  {name:16s}" + "".join(f"{c:>15s}" for c in cells))
    print(f"\nBaseline: mean {H}-week return over all dates {y.mean() * 100:+.2f}% (n={y.notna().sum()})")
    print(f"Per-date scores + returns saved to {OUT}")


if __name__ == "__main__":
    hs = [int(a) for a in sys.argv[1:]] or [1, 2, 4, 8]
    run(hs)
