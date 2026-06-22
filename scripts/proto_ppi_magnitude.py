"""
Prototype: magnitude-scaled PPI scoring vs the current sign-only +-1.

Read-only. Loads the same PPI sources build_currency_scores uses (Myfxbook for
CHF/AUD, Investing for NZD/GBP, TE for the other 4), extracts each currency's
(actual, benchmark) exactly as the live scorer does, then scores PPI two ways:

  current : _dir -> sign of surprise, +1 / 0 / -1 per currency
  magnitude: surprise binned by size into +-2 / +-1 / 0 per currency

Pair cell = clamp(base - quote, -2, 2) under both, so we can see which of the
28 pairs would move and by how much. Nothing is written; this only prints.

Usage:
  python scripts/proto_ppi_magnitude.py                # default thresholds
  python scripts/proto_ppi_magnitude.py 0.15 0.5       # t0 t1 in pp (abs surprise)
  python scripts/proto_ppi_magnitude.py --rel 0.05 0.20  # relative to |benchmark|
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

from src.fetchers import investing_ppi, myfxbook_ppi, tradingeconomics

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
CCYS = ["USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD"]

# EdgeFinder PPI pair cells we have screenshots for. NZDCHF's first reading
# (-2) is kept here but flagged: it contradicts EF's own NZD/CHF charts
# (NZD beat + CHF miss -> positive) and its USD-scorecard up_is_bullish
# convention, so it is treated as the suspected stale/misread outlier.
KNOWN_EF = {
    "EURUSD": -2,
    "USDJPY": -2,
    "USDCAD": 0,
    "CADCHF": 0,
    "EURCHF": -2,   # scorecard shows PPI vs forecast = Bearish
    "NZDCHF": -2,   # suspected anomaly
}
SUSPECT = {"NZDCHF"}


def load_pairs():
    with open(CONFIG_DIR / "pairs.yaml") as f:
        return yaml.safe_load(f)["pairs"]


def ppi_inputs(te_forecast: bool = False) -> dict[str, dict]:
    """Return {ccy: {'actual':, 'benchmark':, 'src':}} using the live source
    precedence in build_currency_scores for ind_id == 'ppi'.

    te_forecast=True flips the TE-sourced currencies (USD/EUR/JPY/CAD) to score
    against TEForecast (analyst forecast) instead of consensus, to test whether
    EF uses the analyst forecast (the EUR PPI forecast-source hypothesis)."""
    inv = investing_ppi.load_cached() or {}
    mfx = myfxbook_ppi.load_cached() or {}
    te = tradingeconomics.load_history() or {}
    out: dict[str, dict] = {}
    for ccy in CCYS:
        actual = benchmark = None
        src = None
        if ccy in ("CHF", "AUD") and mfx.get(ccy):
            rel = mfx[ccy]
            actual = rel.get("actual")
            benchmark = rel.get("consensus")
            if benchmark is None:
                benchmark = rel.get("previous")
            src = "myfxbook"
        elif ccy in ("NZD", "GBP") and inv.get(ccy):
            rel = inv[ccy]
            actual = rel.get("actual")
            benchmark = rel.get("forecast")
            if benchmark is None:
                benchmark = rel.get("previous")
            src = "investing"
        else:
            rels = te.get(f"{ccy}|ppi", [])
            if rels:
                latest = sorted(rels, key=lambda x: x.get("date", ""), reverse=True)[0]
                actual = latest.get("actual")
                if te_forecast:
                    benchmark = latest.get("forecast") or latest.get("consensus")
                else:
                    benchmark = latest.get("consensus") or latest.get("forecast")
                src = "te" + ("/fc" if te_forecast else "")
        out[ccy] = {"actual": actual, "benchmark": benchmark, "src": src}
    return out


def score_sign(actual, benchmark):
    if actual is None or benchmark is None:
        return None
    d = actual - benchmark
    return 1 if d > 0 else (-1 if d < 0 else 0)


def score_mag(actual, benchmark, t0, t1, rel):
    """Magnitude-binned surprise. rel=True -> thresholds are fraction of
    |benchmark|; rel=False -> absolute percentage points."""
    if actual is None or benchmark is None:
        return None
    d = actual - benchmark
    mag = abs(d)
    if rel:
        base = abs(benchmark) if benchmark else 1.0
        mag = mag / base
    sign = 1 if d > 0 else (-1 if d < 0 else 0)
    if mag <= t0:
        return 0
    if mag <= t1:
        return sign * 1
    return sign * 2


def clamp(x, lo=-2, hi=2):
    return max(lo, min(hi, x))


def main():
    args = sys.argv[1:]
    rel = False
    te_forecast = False
    if "--te-forecast" in args:
        te_forecast = True
        args = [a for a in args if a != "--te-forecast"]
    if args and args[0] == "--rel":
        rel = True
        args = args[1:]
    if rel:
        t0, t1 = (float(args[0]), float(args[1])) if len(args) >= 2 else (0.05, 0.20)
    else:
        t0, t1 = (float(args[0]), float(args[1])) if len(args) >= 2 else (0.15, 0.50)

    mode = f"relative (frac of |benchmark|)  t0={t0} t1={t1}" if rel \
        else f"absolute (percentage points)  t0={t0} t1={t1}"
    fc = "TEForecast" if te_forecast else "consensus"
    print(f"PPI magnitude prototype  |  binning: {mode}")
    print(f"  benchmark for TE ccys: {fc}")
    print(f"  0..t0 -> 0,  t0..t1 -> +-1,  >t1 -> +-2\n")

    data = ppi_inputs(te_forecast=te_forecast)
    print(f"{'ccy':<4} {'src':<10} {'actual':>8} {'bench':>8} {'surprise':>9} "
          f"{'sign':>5} {'mag':>4}")
    sign_s, mag_s = {}, {}
    for ccy in CCYS:
        d = data[ccy]
        a, b = d["actual"], d["benchmark"]
        s = score_sign(a, b)
        m = score_mag(a, b, t0, t1, rel)
        sign_s[ccy] = s
        mag_s[ccy] = m
        sur = (a - b) if (a is not None and b is not None) else None
        print(f"{ccy:<4} {str(d['src']):<10} "
              f"{('' if a is None else f'{a:.2f}'):>8} "
              f"{('' if b is None else f'{b:.2f}'):>8} "
              f"{('' if sur is None else f'{sur:+.2f}'):>9} "
              f"{('-' if s is None else f'{s:+d}'):>5} "
              f"{('-' if m is None else f'{m:+d}'):>4}")

    print("\nPair PPI cell  (base - quote, clamped -2..+2):\n")
    print(f"{'pair':<8} {'sign':>5} {'mag':>5}  {'delta':>5}  {'EF':>4}")
    changed = []
    pair_mag: dict[str, int] = {}
    for p in load_pairs():
        sym, base, quote = p["symbol"], p["base"], p["quote"]
        bs, qs = sign_s.get(base), sign_s.get(quote)
        bm, qm = mag_s.get(base), mag_s.get(quote)
        old = None if bs is None or qs is None else clamp(bs - qs)
        new = None if bm is None or qm is None else clamp(bm - qm)
        if new is not None:
            pair_mag[sym] = new
        delta = "" if old is None or new is None else f"{new - old:+d}"
        flag = "  <-- changed" if (old is not None and new is not None and new != old) else ""
        if flag:
            changed.append(sym)
        ef = KNOWN_EF.get(sym)
        ef_s = "" if ef is None else (f"{ef:+d}*" if sym in SUSPECT else f"{ef:+d}")
        print(f"{sym:<8} {('-' if old is None else f'{old:+d}'):>5} "
              f"{('-' if new is None else f'{new:+d}'):>5}  {delta:>5}  {ef_s:>4}{flag}")

    print(f"\n{len(changed)}/28 pairs change: {', '.join(changed) if changed else 'none'}")

    print("\nValidation vs EdgeFinder known cells (* = suspected anomaly):")
    hit = tot = 0
    for sym, ef in KNOWN_EF.items():
        mv = pair_mag.get(sym)
        ok = (mv == ef)
        tag = " (excluded from score)" if sym in SUSPECT else ""
        if sym not in SUSPECT:
            tot += 1
            hit += int(ok)
        mark = "OK " if ok else "XX "
        print(f"  {mark}{sym:<8} EF={ef:+d}  mag={'-' if mv is None else f'{mv:+d}'}{tag}")
    print(f"\n  matched {hit}/{tot} non-suspect EF cells")


if __name__ == "__main__":
    main()
