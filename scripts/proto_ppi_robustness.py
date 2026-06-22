"""Threshold robustness check for magnitude PPI scoring.

Replays every PPI release in te_history through _dir_mag and asks: do the
0.45/0.70 thresholds produce stable cells, or do releases cluster near the bin
boundaries where small forecast revisions flip the score?

Live magnitude mode uses TEForecast as benchmark (te_use_forecast=true), so we
score surprise = actual - forecast. NB: only USD/EUR/JPY/CAD use TE as their
LIVE source; CHF/AUD/GBP use Myfxbook/Investing live (TE rows here are a
surprise-distribution proxy) and NZD has no TE history at all.

Read-only, prints only. No args.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scoring.score_pair import _dir_mag

TE = Path(__file__).resolve().parents[1] / "data" / "cache" / "te_history.json"
LIVE_TE = {"USD", "EUR", "JPY", "CAD"}          # use TE as their live PPI source
T0, T1 = 0.45, 0.70                              # current config thresholds
EDGE = 0.15                                      # "fragile" = within this pp of a boundary
SWEEP = [(0.30, 0.60), (0.45, 0.70), (0.50, 1.00)]


def bench(rel):
    """TEForecast-first benchmark, matching live te_use_forecast=true."""
    b = rel.get("forecast")
    if b is None:
        b = rel.get("consensus")
    if b is None:
        b = rel.get("previous")
    return b


def near_boundary(mag):
    return min(abs(mag - T0), abs(mag - T1)) <= EDGE


def main():
    hist = json.load(open(TE))
    ccys = sorted({k.split("|")[0] for k in hist if k.endswith("|ppi")})

    print(f"PPI threshold robustness  |  benchmark=TEForecast  bins {T0}/{T1}pp  "
          f"edge band +-{EDGE}pp\n")

    fragile = total = flips_real = flips_noise = 0
    for ccy in ccys:
        rels = sorted(hist.get(f"{ccy}|ppi", []), key=lambda x: x.get("date", ""))
        live = "live" if ccy in LIVE_TE else "te-proxy"
        print(f"{ccy}  ({live})")
        prev_score = None
        prev_sur = None
        for r in rels:
            a, b = r.get("actual"), bench(r)
            if a is None or b is None:
                continue
            sur = a - b
            score = _dir_mag(a, b, "up_is_bullish", T0, T1)
            total += 1
            flag = ""
            if near_boundary(abs(sur)):
                fragile += 1
                flag = "  <-- near boundary"
            # classify a score change as real (surprise moved a lot) vs noise
            # (score changed but surprise barely moved)
            if prev_score is not None and score != prev_score:
                if abs(sur - prev_sur) >= (T1 - T0):
                    flips_real += 1
                    flag += "  [flip:real]"
                else:
                    flips_noise += 1
                    flag += "  [flip:NOISE]"
            print(f"    {r.get('date')}  actual={a:>6}  fcst={b:>6}  "
                  f"surprise={sur:+.2f}  score={score:+d}{flag}")
            prev_score, prev_sur = score, sur
        print()

    print(f"summary: {total} releases, {fragile} near a bin boundary "
          f"({100*fragile/total:.0f}%)")
    print(f"  release-to-release score flips: {flips_real} real, "
          f"{flips_noise} noise (small surprise move crossed a boundary)\n")

    print("threshold sweep (score per currency's LATEST release, live-TE ccys):")
    print(f"  {'ccy':<5}" + "".join(f"{f'{a}/{b}':>12}" for a, b in SWEEP))
    for ccy in sorted(LIVE_TE):
        rels = sorted(hist.get(f"{ccy}|ppi", []), key=lambda x: x.get("date", ""))
        if not rels:
            continue
        r = rels[-1]
        a, b = r.get("actual"), bench(r)
        cells = []
        for t0, t1 in SWEEP:
            s = _dir_mag(a, b, "up_is_bullish", t0, t1)
            cells.append("-" if s is None else f"{s:+d}")
        print(f"  {ccy:<5}" + "".join(f"{c:>12}" for c in cells))


if __name__ == "__main__":
    main()
