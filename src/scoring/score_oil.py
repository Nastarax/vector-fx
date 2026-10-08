"""
USOIL (WTI crude) scoring. Standalone instrument, NOT base-minus-quote:
composite = weighted mean of the `signal` rows' -2..+2 scores, rounded half
away from zero, clamped to -2..+2. Signals with no data are left out of the
mean (shown as n/a) instead of scoring 0. Config: config/oil.yaml.

Each signal type maps to a scorer in SCORERS; later phases (rig count, STEO,
China mPMI, OPEC+ flag) slot in as a new yaml entry + a scorer here.
"""
from __future__ import annotations

import bisect
import math
from datetime import date, timedelta
from pathlib import Path

import yaml

from src.fetchers import baker_hughes, eia, investing_china_pmi, steo
from src.fetchers.cot import cot_release_date
from src.scoring.score_pair import _setup_state
from src.scoring.score_sentiment import cot_score
from src.scoring.score_technical import range_position, trend_score

CONFIG = Path(__file__).resolve().parents[2] / "config" / "oil.yaml"


def load_cfg() -> dict:
    with open(CONFIG, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _round_half_away(x: float) -> int:
    return int(math.copysign(math.floor(abs(x) + 0.5), x))


def _fmt_mbbl(kbbl: float, signed: bool = False) -> str:
    return f"{kbbl / 1000:{'+' if signed else ''},.1f}M"


# ---------- EIA helpers ----------

def _seasonal_indices(periods: list[date], idx: int, years: int) -> list[int]:
    """Indices of the observation nearest the same calendar week in each of
    the previous `years` years (within +-3 days)."""
    out = []
    for y in range(1, years + 1):
        target = periods[idx] - timedelta(days=round(365.2425 * y))
        j = bisect.bisect_left(periods, target)
        best = min((k for k in (j - 1, j) if 0 <= k < len(periods)),
                   key=lambda k: abs((periods[k] - target).days), default=None)
        if best is not None and abs((periods[best] - target).days) <= 3:
            out.append(best)
    return out


def _eia_context(series: str, as_of_date: str | None, cfg: dict) -> dict | None:
    lag = cfg["eia"]["release_lag_days"]
    years = cfg["eia"]["seasonal_years"]
    pts = eia.load_series(series, as_of_date, lag)
    if len(pts) < 5:
        return None
    periods = [date.fromisoformat(p) for p, _ in pts]
    vals = [v for _, v in pts]
    i = len(pts) - 1
    seas = [k for k in _seasonal_indices(periods, i, years) if k >= 4]
    if len(seas) < years:
        return None
    return {
        "period": pts[i][0],
        "released": eia.release_date(pts[i][0], lag),
        "value": vals[i],
        "chg4": vals[i] - vals[i - 4],
        "avg4": sum(vals[i - 3:i + 1]) / 4,
        "seas_level": sum(vals[k] for k in seas) / len(seas),
        "seas_chg4": sum(vals[k] - vals[k - 4] for k in seas) / len(seas),
        "seas_avg4": sum(sum(vals[k - 3:k + 1]) / 4 for k in seas) / len(seas),
    }


# ---------- scorers: (sig_cfg, ctx) -> (score | None, reading, date) ----------

def _score_eia_stocks(sig: dict, ctx: dict):
    c = _eia_context(sig["series"], ctx["as_of_date"], ctx["cfg"])
    if c is None:
        return None, "no EIA data", None
    lvl_dev = (c["value"] - c["seas_level"]) / c["seas_level"] * 100
    chg = c["chg4"] - (c["seas_chg4"] if sig.get("change_vs_seasonal", True) else 0.0)
    chg_dev = chg / c["seas_level"] * 100
    # Inventory is inverse to price: below average / drawing = bullish.
    lvl_s = 1 if lvl_dev <= -sig["level_pct"] else (-1 if lvl_dev >= sig["level_pct"] else 0)
    chg_s = 1 if chg_dev <= -sig["change_pct"] else (-1 if chg_dev >= sig["change_pct"] else 0)
    seas_txt = f" (5y {_fmt_mbbl(c['seas_chg4'], True)})" if sig.get("change_vs_seasonal", True) else ""
    reading = (f"{_fmt_mbbl(c['value'])} bbl · {lvl_dev:+.1f}% vs 5y avg · "
               f"4w {_fmt_mbbl(c['chg4'], True)}{seas_txt}")
    return lvl_s + chg_s, reading, f"wk {c['period']} · rel {c['released']}"


def _score_curve(sig: dict, ctx: dict):
    cv = ctx["curve"]
    if cv is None:
        return None, "no curve data for this date", None
    sp = cv["spread_pct"]
    a = abs(sp)
    mag = 2 if a >= sig["strong_pct"] else (1 if a >= sig["weak_pct"] else 0)
    score = mag if sp > 0 else -mag
    shape = "backwardation" if sp > 0 else ("contango" if sp < 0 else "flat")
    reading = (f"{cv['front']} {cv['front_px']:.2f} vs {cv['m12']} {cv['m12_px']:.2f} · "
               f"{sp:+.1f}% {shape}")
    src = " (archive)" if cv.get("source") == "archive" else ""
    return score, reading, f"{cv['date']}{src}"


def _score_cot(sig: dict, ctx: dict):
    r = ctx["cot"]
    if r is None:
        return None, "no COT data", None
    score = cot_score(r, neutral_threshold=sig.get("neutral_threshold", 0.0))
    reading = (f"MM long {r.long_pct:.1f}% ({r.long_pct_change:+.2f}pp w/w) · "
               f"net {r.net_position:+,}")
    rel = cot_release_date(r.report_date, ctx["cfg"]["cot"]["release_lag_days"])
    stale = " · STALE" if r.is_stale else ""
    return score, reading, f"pos {r.report_date} · rel {rel}{stale}"


def _score_trend(sig: dict, ctx: dict):
    df = ctx["df"]
    if df is None or df.empty or len(df["Close"].dropna()) < 15:
        return None, "no price data", None
    closes = df["Close"].dropna()
    sma3 = closes.rolling(3).mean().iloc[-1]
    sma14 = closes.rolling(14).mean().iloc[-1]
    reading = f"CL=F {closes.iloc[-1]:.2f} · SMA3 {sma3:.2f} vs SMA14 {sma14:.2f}"
    return trend_score(df, None, equity_index=False), reading, closes.index[-1].date().isoformat()


def _score_rig_count(sig: dict, ctx: dict):
    field = sig.get("field", "oil")
    pts = baker_hughes.load_series(field, ctx["as_of_date"])
    sw, lw = sig["short_weeks"], sig["long_weeks"]
    if len(pts) <= lw:
        return None, "no rig count data", None
    d, now = pts[-1]
    short_chg = (now / pts[-1 - sw][1] - 1) * 100
    long_chg = (now / pts[-1 - lw][1] - 1) * 100
    sign = -1 if sig.get("direction", "down_is_bullish") == "down_is_bullish" else 1
    s = 0
    if abs(short_chg) >= sig["short_pct"]:
        s += sign * (1 if short_chg > 0 else -1)
    if abs(long_chg) >= sig["long_pct"]:
        s += sign * (1 if long_chg > 0 else -1)
    reading = (f"{now} US {field} rigs · {sw}w {now - pts[-1 - sw][1]:+d} ({short_chg:+.1f}%) · "
               f"{lw}w {now - pts[-1 - lw][1]:+d} ({long_chg:+.1f}%)")
    return s, reading, f"rel {d}"


def _steo_pair(sig: dict, ctx: dict):
    """(vintage, released, this series, previous consecutive vintage's series)."""
    vs = steo.vintages_as_of(ctx["as_of_date"])
    if not vs:
        return None
    k, cur = vs[-1]
    y, m = (int(x) for x in k.split("-"))
    pk = f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"
    prev = dict(vs).get(pk)
    return (k, cur["released"], cur["series"].get(sig["series"], {}),
            prev["series"].get(sig["series"], {}) if prev else None, pk)


def _vintage_label(k: str) -> str:
    y, m = k.split("-")
    return f"{steo.MONTHS[int(m) - 1].title()} {y}"


def _score_steo_spare(sig: dict, ctx: dict):
    t = _steo_pair(sig, ctx)
    if t is None:
        return None, "no STEO vintage released yet", None
    k, rel, cur, prev, pk = t
    now, avg = cur.get(k), steo.window_avg(cur, k)
    if now is None or avg is None:
        return None, "STEO series missing", None
    s = 1 if now <= sig["tight_mbd"] else (-1 if now >= sig["loose_mbd"] else 0)
    rev_txt = ""
    p_avg = steo.window_avg(prev, k) if prev else None
    if p_avg is not None:
        rev = avg - p_avg
        s += 1 if rev <= -sig["rev_mbd"] else (-1 if rev >= sig["rev_mbd"] else 0)
        rev_txt = f" (rev {rev:+.2f} vs {_vintage_label(pk)})"
    reading = f"{now:.2f} mb/d now · next 12m avg {avg:.2f}{rev_txt}"
    return s, reading, f"STEO {_vintage_label(k)} · rel {rel}"


def _score_steo_nonopec(sig: dict, ctx: dict):
    t = _steo_pair(sig, ctx)
    if t is None:
        return None, "no STEO vintage released yet", None
    k, rel, cur, prev, pk = t
    avg = steo.window_avg(cur, k)
    p_avg = steo.window_avg(prev, k) if prev else None
    if avg is None:
        return None, "STEO series missing", None
    if p_avg is None:
        return None, f"next 12m avg {avg:.2f} mb/d · no previous STEO to compare", f"STEO {_vintage_label(k)}"
    rev = avg - p_avg
    a = abs(rev)
    mag = 2 if a >= sig["strong_mbd"] else (1 if a >= sig["weak_mbd"] else 0)
    score = -mag if rev > 0 else mag   # more non-OPEC supply = bearish
    reading = f"next 12m avg {avg:.2f} mb/d · rev {rev:+.2f} vs {_vintage_label(pk)}"
    return score, reading, f"STEO {_vintage_label(k)} · rel {rel}"


def _score_china_pmi(sig: dict, ctx: dict):
    cache = investing_china_pmi.load_cached()
    cutoff = ctx["as_of_date"]
    nbs = cache.get("NBS")
    if not nbs or (cutoff and nbs.get("date", "9999") > cutoff):
        return None, "no China PMI print for this date", None
    bench = nbs.get("forecast") if nbs.get("forecast") is not None else nbs.get("previous")
    a = nbs["actual"]
    score = 0 if bench is None or a == bench else (1 if a > bench else -1)
    vs = "fcst" if nbs.get("forecast") is not None else "prev"
    reading = f"NBS {a:g} vs {bench:g} {vs} (prev {nbs.get('previous')})" if bench is not None else f"NBS {a:g}"
    cx = cache.get("CAIXIN")
    if cx and cx.get("actual") is not None and not (cutoff and cx.get("date", "9999") > cutoff):
        reading += f" · Caixin {cx['actual']:g}" + (f" vs {cx['forecast']:g}" if cx.get("forecast") is not None else "")
    return score, reading, f"rel {nbs['date']}"


_OPEC_SCORES = {"cut": 1, "hike": -1, "hold": 0}


def _score_opec_flag(sig: dict, ctx: dict):
    ref = ctx["as_of_date"] or date.today().isoformat()
    past = sorted((e for e in ctx["cfg"].get("opec_decisions") or [] if str(e.get("date")) <= ref),
                  key=lambda e: str(e["date"]))
    if not past:
        return None, "no decision logged (add one to opec_decisions in config/oil.yaml)", None
    e = past[-1]
    d = str(e["date"])
    dec = str(e.get("decision", "")).lower()
    age = (date.fromisoformat(ref) - date.fromisoformat(d)).days
    note = f" · {e['note']}" if e.get("note") else ""
    return _OPEC_SCORES.get(dec), f"{dec or '?'}{note} ({age}d ago)", d


def _display_eia(sig: dict, ctx: dict):
    c = _eia_context(sig["series"], ctx["as_of_date"], ctx["cfg"])
    if c is None:
        return None, "no EIA data", None
    dev = (c["avg4"] - c["seas_avg4"]) / c["seas_avg4"] * 100
    reading = (f"{c['value']:,.0f} kb/d · 4w avg {c['avg4']:,.0f} ({dev:+.1f}% vs 5y) · "
               f"4w chg {c['chg4']:+,.0f}")
    return None, reading, f"wk {c['period']} · rel {c['released']}"


SCORERS = {
    "eia_stocks": _score_eia_stocks,
    "curve": _score_curve,
    "cot": _score_cot,
    "trend": _score_trend,
    "rig_count": _score_rig_count,
    "steo_spare": _score_steo_spare,
    "steo_nonopec": _score_steo_nonopec,
    "china_pmi": _score_china_pmi,
    "opec_flag": _score_opec_flag,
    "eia_display": _display_eia,
}


def eia_series_ids(cfg: dict) -> list[str]:
    return [s["series"] for s in cfg["signals"] if s["type"].startswith("eia")]


def build_oil(df, cot_reading, curve, as_of_date: str | None = None, cfg: dict | None = None) -> dict:
    cfg = cfg or load_cfg()
    ctx = {"cfg": cfg, "as_of_date": as_of_date, "df": df, "cot": cot_reading, "curve": curve}
    rows = []
    wsum = wtot = 0.0
    for sig in cfg["signals"]:
        scorer = SCORERS.get(sig["type"])
        if scorer is None:
            score, reading, dt = None, f"no scorer for type '{sig['type']}'", None
        else:
            try:
                score, reading, dt = scorer(sig, ctx)
            except Exception as e:
                print(f"[oil] {sig['id']} scoring failed: {e}")
                score, reading, dt = None, "error", None
        role = sig.get("role", "signal")
        if score is not None:
            score = max(-2, min(2, int(score)))
        if role == "signal" and score is not None:
            w = float(sig.get("weight", 1.0))
            wsum += w * score
            wtot += w
        rows.append({
            "id": sig["id"], "label": sig["label"], "role": role,
            # signal rows are summed; regime rows keep their lean for display
            # only; display rows carry no score at all.
            "score": score if role in ("signal", "regime") else None,
            "weight": sig.get("weight") if role == "signal" else None,
            "reading": reading, "date": dt,
        })

    inst = cfg["instrument"]
    # The mean of -2..+2 cells is itself always within -2..+2 (the clamp is only
    # a safety net). Shown to 1 decimal; the bias label uses the nearest whole
    # number, rounding halves away from zero (-0.5 -> -1 -> Bearish).
    if wtot > 0:
        mean = wsum / wtot
        rounded = max(-2, min(2, _round_half_away(mean)))
        score = round(max(-2.0, min(2.0, mean)), 1)
        bias = cfg["bias_labels"][str(rounded)]
    else:
        score, rounded, bias, mean = None, None, "Neutral", None
    loc_pct = range_position(df) if df is not None else None
    return {
        "symbol": inst["symbol"],
        "display_name": inst.get("display_name", inst["symbol"]),
        "score": score,                 # mean, 1 decimal (what the page shows)
        "score_rounded": rounded,       # nearest whole number (drives bias + colour)
        "mean": mean,
        "sum": round(wsum, 2) if wtot > 0 else None,
        "bias": bias,
        "loc_pct": loc_pct,
        "setup": _setup_state(bias, loc_pct),
        "rows": rows,
        "n_signals": sum(1 for r in rows if r["role"] == "signal"),
        "n_scored": sum(1 for r in rows if r["role"] == "signal" and r["score"] is not None),
    }
