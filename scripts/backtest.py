"""
Systematic backtester with multi-horizon analysis.

Question this answers: when we get a Very Bullish reading, does the market
actually go up over the following days and weeks? Same for Very Bearish.

For each Monday in the last N weeks:
1. Generate the heatmap "as of" that date
2. Capture each pair's score
3. Compute forward returns at multiple horizons (1, 3, 5, 10, 20, 30, 60 days)

Output:
- Console table: hit rate and avg return at each horizon, by bucket
- backtest_results.csv: raw data
- backtest_report.html: visual report

Usage:
  python scripts/backtest.py
  python scripts/backtest.py --weeks 52
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.fetchers import cot, forexfactory, fred, prices, retail
from src.scoring.score_pair import build_heatmap as build_matrix, load_pairs_cfg

OUTPUT_DIR = ROOT / "data" / "backtest"
HORIZONS = [1, 3, 5, 10, 20, 30, 60]


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--weeks", type=int, default=26, help="Weeks back to test")
    return p.parse_args()


def generate_dates(weeks_back: int) -> list[str]:
    today = datetime.now()
    dates = []
    for w in range(1, weeks_back + 1):
        days_to_monday = today.weekday()
        this_monday = today - timedelta(days=days_to_monday)
        target = this_monday - timedelta(weeks=w)
        dates.append(target.strftime("%Y-%m-%d"))
    return dates


def filter_macro_to_date(macro_data: dict, as_of: str) -> dict:
    out = {}
    for ccy, indicators in macro_data.items():
        out[ccy] = {}
        for ind_id, obs in indicators.items():
            out[ccy][ind_id] = [o for o in obs if o.date <= as_of]
    return out


def filter_ff_to_date(ff_history: dict, as_of: str) -> dict:
    out = {}
    for key, releases in ff_history.items():
        filtered = [r for r in releases if r.get("date", "") <= as_of]
        if filtered:
            out[key] = filtered
    return out


def filter_prices_to_date(px: dict, as_of: str) -> dict:
    out = {}
    cutoff = pd.Timestamp(as_of)
    for sym, df in px.items():
        if df is None or df.empty:
            out[sym] = df
            continue
        try:
            cutoff_local = cutoff.tz_localize(df.index.tz) if df.index.tz else cutoff
            out[sym] = df.loc[df.index <= cutoff_local]
        except Exception:
            out[sym] = df
    return out


def forward_return(df: pd.DataFrame, start_date: str, hold_days: int) -> float | None:
    if df is None or df.empty:
        return None
    cutoff = pd.Timestamp(start_date)
    try:
        cutoff_local = cutoff.tz_localize(df.index.tz) if df.index.tz else cutoff
        future = df.loc[df.index >= cutoff_local]
        if len(future) <= hold_days:
            return None
        start_price = float(future.iloc[0]["Close"])
        end_price = float(future.iloc[hold_days]["Close"])
        if start_price <= 0:
            return None
        return (end_price - start_price) / start_price * 100
    except Exception:
        return None


def bucket_score(s: int) -> str:
    if s >= 9:
        return "Very Bullish"
    if s >= 4:
        return "Bullish"
    if s <= -9:
        return "Very Bearish"
    if s <= -4:
        return "Bearish"
    return "Neutral"


BUCKET_ORDER = ["Very Bullish", "Bullish", "Neutral", "Bearish", "Very Bearish"]


def main():
    args = parse_args()
    load_dotenv(ROOT / ".env")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Backtest: {args.weeks} weeks, horizons {HORIZONS} trading days")
    print("\nPre-fetching data...")
    macro_full = fred.fetch_all_macro()
    cot_data = {}  # skip COT in backtest to avoid look-ahead bias
    px_full = prices.fetch_prices()
    px_4h_full = prices.fetch_prices_4h()
    ff_history = forexfactory.load_history()
    pairs_cfg = load_pairs_cfg()
    pair_symbols = [p["symbol"] for p in pairs_cfg["pairs"]]
    print(f"  Pre-fetch done. Pairs: {len(pair_symbols)}, FF history: {sum(len(v) for v in ff_history.values())}")

    dates = generate_dates(args.weeks)
    print(f"\nDates: {dates[0]} ... {dates[-1]}")

    rows = []
    for i, date_str in enumerate(dates, 1):
        print(f"  [{i:>2}/{len(dates)}] {date_str}", end=" ", flush=True)
        macro_f = filter_macro_to_date(macro_full, date_str)
        ff_f = filter_ff_to_date(ff_history, date_str)
        px_f = filter_prices_to_date(px_full, date_str)
        px_4h_f = filter_prices_to_date(px_4h_full, date_str)
        rt = {sym: retail.RetailReading(sym, 50.0, 50.0) for sym in pair_symbols}

        try:
            heatmap = build_matrix(
                macro_f, cot_data, rt, px_f,
                prices_4h=px_4h_f, as_of_date=date_str, ff_history=ff_f,
            )
        except Exception as e:
            print(f"FAILED ({e})")
            continue

        for r in heatmap["rows"]:
            sym = r["symbol"]
            score = r["total"]
            row = {"date": date_str, "symbol": sym, "score": score, "bias": r["bias"]}
            for h in HORIZONS:
                row[f"ret_{h}d"] = forward_return(px_full.get(sym), date_str, h)
            rows.append(row)
        print(f"({len(heatmap['rows'])})")

    if not rows:
        print("No data.")
        return

    df = pd.DataFrame(rows)
    df["bucket"] = df["score"].apply(bucket_score)
    df.to_csv(OUTPUT_DIR / "backtest_results.csv", index=False)
    print(f"\nRaw data -> {OUTPUT_DIR / 'backtest_results.csv'}")

    # ============================================================
    # Multi-horizon analysis
    # ============================================================
    print(f"\n{'='*78}")
    print("DOES THE MARKET MOVE THE PREDICTED DIRECTION?")
    print(f"{'='*78}\n")

    for bucket in BUCKET_ORDER:
        sub = df[df["bucket"] == bucket]
        if len(sub) == 0:
            continue
        print(f"--- {bucket}  ({len(sub)} signals) ---")
        if "Bullish" in bucket:
            expected_dir = "UP"
            check = lambda r: r > 0
        elif "Bearish" in bucket:
            expected_dir = "DOWN"
            check = lambda r: r < 0
        else:
            expected_dir = None
            check = None

        if expected_dir:
            print(f"  Expected: pair goes {expected_dir}")
            print(f"  {'Horizon':>8}  {'Avg Return':>10}  {'Hit Rate':>9}  {'Median':>8}")
            for h in HORIZONS:
                col = f"ret_{h}d"
                values = sub[col].dropna()
                if len(values) == 0:
                    continue
                avg = values.mean()
                med = values.median()
                hits = values.apply(check).sum()
                hit_rate = hits / len(values) * 100
                # For bearish, "good" return is negative
                strat_ret = avg if "Bullish" in bucket else -avg
                print(f"  {str(h)+'d':>8}  {avg:>+9.2f}%  {hit_rate:>8.1f}%  {med:>+7.2f}%")
        else:
            print(f"  (Neutral signals, no directional expectation)")
        print()

    # ============================================================
    # KEY METRIC: extreme signal performance
    # ============================================================
    very_bull = df[df["score"] >= 9]
    very_bear = df[df["score"] <= -9]
    print(f"{'='*78}")
    print("KEY METRIC: Very Bullish & Very Bearish only")
    print(f"{'='*78}\n")
    print(f"Very Bullish (n={len(very_bull)}):")
    print(f"  {'Horizon':>8}  {'Avg %':>8}  {'% time UP':>10}")
    for h in HORIZONS:
        col = f"ret_{h}d"
        values = very_bull[col].dropna()
        if len(values) == 0:
            continue
        up_pct = (values > 0).sum() / len(values) * 100
        print(f"  {str(h)+'d':>8}  {values.mean():>+7.2f}  {up_pct:>9.1f}%")
    print(f"\nVery Bearish (n={len(very_bear)}):")
    print(f"  {'Horizon':>8}  {'Avg %':>8}  {'% time DOWN':>12}")
    for h in HORIZONS:
        col = f"ret_{h}d"
        values = very_bear[col].dropna()
        if len(values) == 0:
            continue
        down_pct = (values < 0).sum() / len(values) * 100
        print(f"  {str(h)+'d':>8}  {values.mean():>+7.2f}  {down_pct:>11.1f}%")

    render_report(df, args, OUTPUT_DIR / "backtest_report.html")
    print(f"\nReport -> {OUTPUT_DIR / 'backtest_report.html'}")


def render_report(df: pd.DataFrame, args, out_path: Path):
    """HTML report with multi-horizon analysis."""
    df = df.copy()
    df["bucket"] = df["score"].apply(bucket_score)

    # Build per-bucket per-horizon stats
    bucket_stats = {}
    for bucket in BUCKET_ORDER:
        sub = df[df["bucket"] == bucket]
        if len(sub) == 0:
            continue
        per_horizon = []
        for h in HORIZONS:
            col = f"ret_{h}d"
            vals = sub[col].dropna()
            if len(vals) == 0:
                continue
            avg = float(vals.mean())
            up_pct = float((vals > 0).sum() / len(vals) * 100)
            down_pct = float((vals < 0).sum() / len(vals) * 100)
            per_horizon.append({"h": h, "avg": avg, "up_pct": up_pct, "down_pct": down_pct, "n": len(vals)})
        bucket_stats[bucket] = per_horizon

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>EdgeFinder Backtest</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
body {{ background:#0d1430; color:#e8ecff; font-family:-apple-system,Segoe UI,sans-serif; padding:24px; font-size:13px; }}
h1 {{ font-size:22px; margin:0 0 4px; }}
h2 {{ font-size:16px; margin-top:32px; color:#aac4ff; }}
h3 {{ font-size:14px; margin-top:20px; color:#cdd9ff; }}
.meta {{ color:#8893c0; font-size:12px; margin-bottom:24px; }}
table {{ border-collapse:collapse; margin:8px 0; max-width:900px; }}
th, td {{ padding:6px 12px; border-bottom:1px solid #2a3060; text-align:right; }}
th:first-child, td:first-child {{ text-align:left; }}
.pos {{ color:#3974e6; font-weight:600; }}
.neg {{ color:#e07984; font-weight:600; }}
.bucket-vbull {{ background:#1f3a86; padding:3px 8px; border-radius:3px; }}
.bucket-bull {{ background:#274690; padding:3px 8px; border-radius:3px; }}
.bucket-bear {{ background:#7a2935; padding:3px 8px; border-radius:3px; }}
.bucket-vbear {{ background:#a01827; padding:3px 8px; border-radius:3px; }}
canvas {{ background:#141a3a; border-radius:6px; padding:12px; max-width:900px; margin-top:12px; }}
</style></head><body>

<h1>Backtest: Does the signal predict the move?</h1>
<div class="meta">{args.weeks} weeks of weekly snapshots · 13 FX pairs · forward returns at {", ".join([str(h)+"d" for h in HORIZONS])}</div>
"""

    for bucket in BUCKET_ORDER:
        if bucket not in bucket_stats:
            continue
        stats = bucket_stats[bucket]
        css_cls = {
            "Very Bullish": "bucket-vbull", "Bullish": "bucket-bull",
            "Bearish": "bucket-bear", "Very Bearish": "bucket-vbear",
        }.get(bucket, "")
        n = stats[0]["n"] if stats else 0
        html += f'<h3><span class="{css_cls}">{bucket}</span> &nbsp; ({n} signals)</h3>'
        if "Bullish" in bucket:
            html += '<p style="color:#8893c0">Expected: pair goes UP. <strong>"% Up"</strong> column should ideally be &gt; 50%.</p>'
        elif "Bearish" in bucket:
            html += '<p style="color:#8893c0">Expected: pair goes DOWN. <strong>"% Down"</strong> column should ideally be &gt; 50%.</p>'
        else:
            html += '<p style="color:#8893c0">No directional expectation.</p>'

        html += '<table><tr><th>Horizon</th><th>Avg Return</th><th>% Up</th><th>% Down</th></tr>'
        for s in stats:
            avg_cls = "pos" if s["avg"] > 0 else "neg"
            html += f'<tr><td>{s["h"]}d</td><td class="{avg_cls}">{s["avg"]:+.2f}%</td><td>{s["up_pct"]:.1f}%</td><td>{s["down_pct"]:.1f}%</td></tr>'
        html += '</table>'

    # Chart of avg return by horizon for each bucket
    chart_data = {}
    for bucket, stats in bucket_stats.items():
        chart_data[bucket] = {"h": [s["h"] for s in stats], "avg": [s["avg"] for s in stats]}

    html += f"""
<h2>Avg return curve over time, by signal strength</h2>
<canvas id="chart" height="320"></canvas>
<script>
const data = {json.dumps(chart_data)};
const colors = {{ 'Very Bullish':'#3974e6','Bullish':'#5d8ff0','Neutral':'#8893c0','Bearish':'#e07984','Very Bearish':'#d23b4a' }};
const datasets = Object.entries(data).map(([bucket, vals]) => ({{
  label: bucket,
  data: vals.h.map((h,i) => ({{ x:h, y:vals.avg[i] }})),
  borderColor: colors[bucket],
  backgroundColor: colors[bucket],
  showLine: true, fill: false, tension: 0.2, pointRadius: 5,
}}));
new Chart(document.getElementById('chart').getContext('2d'), {{
  type:'scatter', data: {{datasets}},
  options: {{
    plugins:{{ legend:{{ labels:{{ color:'#e8ecff' }} }} }},
    scales:{{
      x:{{ title:{{display:true, text:'Trading days forward', color:'#aac4ff'}}, ticks:{{color:'#aac4ff'}}, grid:{{color:'#2a3060'}} }},
      y:{{ title:{{display:true, text:'Average % return', color:'#aac4ff'}}, ticks:{{color:'#aac4ff'}}, grid:{{color:'#2a3060'}} }}
    }}
  }}
}});
</script>
</body></html>
"""
    out_path.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    main()
