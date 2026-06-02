"""
Renders interactive seasonality pages for all FX pairs.

Two pages, one per chart type, each with a pair-selector dropdown:
  data/seasonality_yearly.html   - Full Year cumulative perf, dual Y-axes
  data/seasonality_monthly.html  - Avg monthly returns + this year overlay

Each page embeds the seasonality data for ALL pairs as JSON so switching
pairs in the dropdown is instant (no page reload, no data fetch).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

DISPLAY_NAMES = {"XAUUSD": "Gold"}

import pandas as pd

OUTPUT_DIR = Path(__file__).resolve().parents[2] / "data"


def _weekly_cum_returns_by_year(df: pd.DataFrame) -> pd.DataFrame:
    """
    For each calendar year present in df, return a Series indexed by ISO week
    number (1..53) holding the cumulative % return from Jan 1 close of that
    year to the close of the latest day in that week.

    Result: DataFrame, columns = years, index = week numbers (1..53).
    """
    closes = df["Close"].copy()
    closes.index = pd.to_datetime(closes.index)
    out = {}
    for year, slc in closes.groupby(closes.index.year):
        if slc.empty:
            continue
        first_close = float(slc.iloc[0])
        if first_close == 0:
            continue
        cum_pct = (slc / first_close - 1.0) * 100.0
        wk = cum_pct.copy()
        wk.index = pd.to_datetime(wk.index)
        weekly = wk.groupby(wk.index.isocalendar().week).last()
        out[year] = weekly
    return pd.DataFrame(out)


def _monthly_returns_by_year(df: pd.DataFrame) -> pd.DataFrame:
    """
    Per-month % return for each calendar year present in df.
    Result: DataFrame, columns = years, index = month (1..12).
    """
    closes = df["Close"].copy()
    closes.index = pd.to_datetime(closes.index)
    m = closes.resample("ME").last()
    monthly_pct = m.pct_change() * 100.0
    out = {}
    for year, slc in monthly_pct.groupby(monthly_pct.index.year):
        if slc.empty:
            continue
        s = pd.Series({d.month: float(v) for d, v in slc.items() if pd.notna(v)})
        out[year] = s
    return pd.DataFrame(out).reindex(range(1, 13))


def compute_seasonality(df: pd.DataFrame, lookback_years: int = 10) -> dict:
    """
    Returns the per-pair seasonality dict consumed by the templates.
    All return values are in percent (e.g. 1.41 = +1.41%).
    """
    df = df.copy()
    df.index = pd.to_datetime(df.index)
    current_year = df.index.max().year

    weekly_by_year = _weekly_cum_returns_by_year(df)
    past_years = [y for y in weekly_by_year.columns if y < current_year]
    past_years = sorted(past_years)[-lookback_years:]
    avg_weekly = weekly_by_year[past_years].mean(axis=1) if past_years else None
    ytd_weekly = weekly_by_year[current_year] if current_year in weekly_by_year.columns else None

    monthly_by_year = _monthly_returns_by_year(df)
    past_years_m = [y for y in monthly_by_year.columns if y < current_year]
    past_years_m = sorted(past_years_m)[-lookback_years:]
    avg_monthly = monthly_by_year[past_years_m].mean(axis=1) if past_years_m else None
    this_year_monthly = monthly_by_year[current_year] if current_year in monthly_by_year.columns else None

    avg_weekly_list = []
    if avg_weekly is not None:
        for wk in range(1, 54):
            if wk in avg_weekly.index and pd.notna(avg_weekly.loc[wk]):
                avg_weekly_list.append({"week": int(wk), "value": round(float(avg_weekly.loc[wk]), 4)})

    ytd_weekly_list = []
    if ytd_weekly is not None:
        for wk in range(1, 54):
            if wk in ytd_weekly.index and pd.notna(ytd_weekly.loc[wk]):
                ytd_weekly_list.append({"week": int(wk), "value": round(float(ytd_weekly.loc[wk]), 4)})

    ytd_price_list = []
    closes = df["Close"].copy()
    closes.index = pd.to_datetime(closes.index)
    cy_closes = closes[closes.index.year == current_year]
    if not cy_closes.empty:
        weekly_close = cy_closes.groupby(cy_closes.index.isocalendar().week).last()
        for wk in range(1, 54):
            if wk in weekly_close.index and pd.notna(weekly_close.loc[wk]):
                ytd_price_list.append({"week": int(wk), "price": round(float(weekly_close.loc[wk]), 5)})

    avg_monthly_list = []
    if avg_monthly is not None:
        for m in range(1, 13):
            if m in avg_monthly.index and pd.notna(avg_monthly.loc[m]):
                avg_monthly_list.append({"month": int(m), "return": round(float(avg_monthly.loc[m]), 2)})

    this_year_monthly_list = []
    if this_year_monthly is not None:
        for m in range(1, 13):
            if m in this_year_monthly.index and pd.notna(this_year_monthly.loc[m]):
                this_year_monthly_list.append({"month": int(m), "return": round(float(this_year_monthly.loc[m]), 2)})

    return {
        "current_year": int(current_year),
        "lookback_years": list(map(int, past_years)),
        "avg_weekly": avg_weekly_list,
        "ytd_weekly": ytd_weekly_list,
        "ytd_price": ytd_price_list,
        "avg_monthly": avg_monthly_list,
        "this_year_monthly": this_year_monthly_list,
    }


# =================== Page templates ===================
# Both pages share the same brand header and styling, differ only in chart
# canvas + Chart.js config. Embed all-pair data as JSON so the dropdown
# switches the chart instantly without a page reload.

_COMMON_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Vector | {page_title}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'><stop offset='0' stop-color='%233d77e8'/><stop offset='1' stop-color='%231e4fd1'/></linearGradient></defs><rect width='32' height='32' rx='7' fill='url(%23g)'/><text x='16' y='23' font-family='Arial' font-size='18' font-weight='bold' fill='white' text-anchor='middle'>V</text></svg>">
<link rel="stylesheet" href="vector.css">
<style>
  .toolbar{{display:flex;align-items:center;gap:14px;padding:14px 24px;background:var(--panel);border-bottom:1px solid var(--border);font-size:13px}}
  .toolbar label{{color:var(--muted);margin-right:6px}}
  select{{background:var(--row);color:var(--text);border:1px solid var(--border);padding:6px 12px;border-radius:4px;font-size:13px;cursor:pointer}}
  select:hover{{border-color:#3a4078}}
  .nav-tabs{{display:flex;gap:6px;margin-left:auto}}
  .nav-tabs a{{padding:6px 14px;border-radius:4px;color:#aac4ff;text-decoration:none;font-size:13px;border:1px solid var(--border);background:var(--row)}}
  .nav-tabs a.active{{background:#3974e6;color:#fff;border-color:#3974e6}}
  .nav-tabs a:hover:not(.active){{background:var(--rowAlt)}}
  .charts{{padding:24px;display:flex;flex-direction:column;gap:24px;max-width:1400px;margin:0 auto}}
  .card{{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:18px}}
  .card h2{{margin:0 0 12px 0;font-size:16px;font-weight:600}}
  .card .subtitle{{color:var(--muted);font-size:11px;margin-bottom:14px}}
  .chart-wrap{{position:relative;height:440px}}
  footer{{padding:16px 24px;color:var(--muted);font-size:11px;border-top:1px solid var(--border);text-align:center}}
</style>
</head>
<body>
<header>
  <div class="brand">
    <div class="brand-mark">V</div>
    <div>
      <div class="brand-name">VECTOR</div>
      <h1>{header_subtitle}</h1>
    </div>
  </div>
  <div class="meta">Updated <b>{updated_at}</b></div>
</header>

<nav class="topnav">
  <a class="navlink" href="index.html">Top Setups</a>
  <a class="navlink" href="cot.html">COT Detail</a>
  <a class="navlink" href="economic_heatmap.html">Economic Heatmap</a>
  <a class="navlink" href="scorecard.html">Asset Scorecard</a>
  <a class="navlink" href="inflation.html">Inflation</a>
  <a class="navlink" href="macro.html">Macro Calendar</a>
  <a class="navlink active" href="seasonality_yearly.html">Seasonality</a>
  <span class="nav-spacer"></span>
</nav>

<div class="toolbar">
  <div><label>Pair</label><select id="pairSelect"></select></div>
  <div class="nav-tabs">
    <a href="seasonality_yearly.html" {yearly_active}>Yearly</a>
    <a href="seasonality_monthly.html" {monthly_active}>Monthly</a>
  </div>
</div>
"""

_FOOTER = """<footer>Vector &middot; Data: Yahoo Finance &middot; For personal research only, not financial advice.</footer>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
"""


_YEARLY_BODY = """
<div class="charts">
  <div class="card">
    <h2 id="chartTitle">Full Year Seasonality</h2>
    <div class="subtitle" id="chartSubtitle"></div>
    <div class="chart-wrap"><canvas id="annualChart"></canvas></div>
  </div>
</div>
""" + _FOOTER + """
<script>
const ALL_DATA = {data_json};
const PAIRS = {pairs_json};
const LABELS = {labels_json};
const DEFAULT_PAIR = "{default_pair}";
const MONTH_LABEL_BY_WEEK = {{1:'Jan',5:'Feb',9:'Mar',14:'Apr',18:'May',22:'Jun',27:'Jul',31:'Aug',35:'Sep',40:'Oct',44:'Nov',48:'Dec'}};

const sel = document.getElementById('pairSelect');
PAIRS.forEach(p => {{
  const opt = document.createElement('option');
  opt.value = p; opt.textContent = LABELS[p] || p;
  if (p === DEFAULT_PAIR) opt.selected = true;
  sel.appendChild(opt);
}});

let chart = null;
function render(pair) {{
  const d = ALL_DATA[pair];
  if (!d) return;
  document.getElementById('chartSubtitle').textContent =
    `YTD performance (${{d.current_year}}) vs ${{d.lookback_years.length}}-year avg performance, by week of year.`;

  const weeks = Array.from({{length: 53}}, (_, i) => i + 1);
  const avgMap   = new Map(d.avg_weekly.map(x => [x.week, x.value]));
  const priceMap = new Map(d.ytd_price.map(x => [x.week, x.price]));

  const config = {{
    type: 'line',
    data: {{
      labels: weeks,
      datasets: [
        {{
          label: '10-Year Avg Performance',
          data: weeks.map(w => avgMap.has(w) ? avgMap.get(w) : null),
          borderColor: 'rgba(200,200,210,0.85)',
          backgroundColor: 'transparent',
          borderDash: [6,4], borderWidth: 1.5, pointRadius: 0,
          tension: 0.2, spanGaps: true, yAxisID: 'yPct',
        }},
        {{
          label: 'YTD Price',
          data: weeks.map(w => priceMap.has(w) ? priceMap.get(w) : null),
          borderColor: '#dd5050',
          backgroundColor: 'transparent',
          borderWidth: 2.2, pointRadius: 0,
          tension: 0.2, spanGaps: true, yAxisID: 'yPrice',
        }},
      ]
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      interaction: {{mode:'index', intersect:false}},
      plugins: {{
        legend: {{labels: {{color:'#cfd6f5'}}}},
        tooltip: {{
          callbacks: {{
            title: items => 'Week ' + items[0].label,
            label: ctx => {{
              if (ctx.parsed.y === null) return ctx.dataset.label + ': n/a';
              if (ctx.dataset.yAxisID === 'yPrice') return ctx.dataset.label + ': ' + ctx.parsed.y.toFixed(5);
              return ctx.dataset.label + ': ' + ctx.parsed.y.toFixed(2) + '%';
            }}
          }}
        }}
      }},
      scales: {{
        x: {{
          grid: {{color: 'rgba(255,255,255,0.05)'}},
          ticks: {{color:'#8893c0', autoSkip: false,
            callback: function(val, idx) {{ const w = idx + 1; return MONTH_LABEL_BY_WEEK[w] || ''; }},
          }},
        }},
        yPrice: {{
          position: 'left',
          grid: {{color: 'rgba(255,255,255,0.05)'}},
          ticks: {{color:'#dd5050', callback: v => Number(v).toFixed(4)}},
          title: {{display:true, text:'YTD Price', color:'#dd5050'}},
        }},
        yPct: {{
          position: 'right',
          grid: {{drawOnChartArea: false}},
          ticks: {{color:'#cfd6f5', callback: v => v + '%'}},
          title: {{display:true, text:'10-Year Avg %', color:'#cfd6f5'}},
        }},
      }},
    }}
  }};

  if (chart) chart.destroy();
  chart = new Chart(document.getElementById('annualChart').getContext('2d'), config);
}}

sel.addEventListener('change', e => render(e.target.value));
render(DEFAULT_PAIR);
</script>
</body>
</html>
"""


_MONTHLY_BODY = """
<div class="charts">
  <div class="card">
    <h2>Seasonality - Avg. returns by month</h2>
    <div class="subtitle" id="chartSubtitle"></div>
    <div class="chart-wrap"><canvas id="monthlyChart"></canvas></div>
  </div>
</div>
""" + _FOOTER + """
<script>
const ALL_DATA = {data_json};
const PAIRS = {pairs_json};
const LABELS = {labels_json};
const DEFAULT_PAIR = "{default_pair}";
const MONTH_LABELS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

const sel = document.getElementById('pairSelect');
PAIRS.forEach(p => {{
  const opt = document.createElement('option');
  opt.value = p; opt.textContent = LABELS[p] || p;
  if (p === DEFAULT_PAIR) opt.selected = true;
  sel.appendChild(opt);
}});

let chart = null;
function render(pair) {{
  const d = ALL_DATA[pair];
  if (!d) return;
  document.getElementById('chartSubtitle').textContent =
    `${{d.current_year}} monthly returns (white dots) vs ${{d.lookback_years.length}}-year average (blue=positive, red=negative).`;

  const avgMap = new Map(d.avg_monthly.map(x => [x.month, x.return]));
  const tyMap  = new Map(d.this_year_monthly.map(x => [x.month, x.return]));
  const avgArr = MONTH_LABELS.map((_, i) => avgMap.has(i+1) ? avgMap.get(i+1) : null);
  const tyArr  = MONTH_LABELS.map((_, i) => tyMap.has(i+1) ? tyMap.get(i+1) : null);

  const config = {{
    type: 'bar',
    data: {{
      labels: MONTH_LABELS,
      datasets: [
        {{
          label: '10-Year Avg',
          data: avgArr,
          backgroundColor: avgArr.map(v => v === null ? 'rgba(120,120,140,0.4)' : (v >= 0 ? 'rgba(57,116,230,0.85)' : 'rgba(221,80,80,0.85)')),
          borderColor:     avgArr.map(v => v === null ? 'rgba(120,120,140,1)'   : (v >= 0 ? 'rgba(57,116,230,1)'    : 'rgba(221,80,80,1)')),
          borderWidth: 0, borderRadius: 2, order: 2,
        }},
        {{
          label: 'This Year', data: tyArr, type: 'line',
          borderColor: 'rgba(255,255,255,0.85)', borderDash: [4,4],
          backgroundColor: '#fff', pointRadius: 5,
          pointBorderColor: '#fff', pointBackgroundColor: '#fff',
          tension: 0.2, spanGaps: true, order: 1,
        }},
      ]
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      interaction: {{mode:'index', intersect:false}},
      plugins: {{
        legend: {{labels: {{color:'#cfd6f5'}}}},
        tooltip: {{
          callbacks: {{
            label: ctx => ctx.dataset.label + ': ' + (ctx.parsed.y === null ? 'n/a' : ctx.parsed.y.toFixed(2) + '%'),
          }}
        }},
      }},
      scales: {{
        x: {{grid: {{color:'rgba(255,255,255,0.05)'}}, ticks: {{color:'#8893c0'}}}},
        y: {{grid: {{color:'rgba(255,255,255,0.05)'}}, ticks: {{color:'#8893c0', callback: v => v + '%'}}}},
      }},
    }}
  }};

  if (chart) chart.destroy();
  chart = new Chart(document.getElementById('monthlyChart').getContext('2d'), config);
}}

sel.addEventListener('change', e => render(e.target.value));
render(DEFAULT_PAIR);
</script>
</body>
</html>
"""


def _render_yearly(all_data: dict, default_pair: str) -> Path:
    pairs = sorted(all_data.keys())
    labels = {p: DISPLAY_NAMES.get(p, p) for p in pairs}
    head = _COMMON_HEAD.format(
        page_title="Yearly Seasonality",
        header_subtitle="Yearly Seasonality",
        updated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        yearly_active='class="active"',
        monthly_active='',
    )
    body = _YEARLY_BODY.format(
        data_json=json.dumps(all_data),
        pairs_json=json.dumps(pairs),
        labels_json=json.dumps(labels),
        default_pair=default_pair,
    )
    out_path = OUTPUT_DIR / "seasonality_yearly.html"
    out_path.write_text(head + body, encoding="utf-8")
    return out_path


def _render_monthly(all_data: dict, default_pair: str) -> Path:
    pairs = sorted(all_data.keys())
    labels = {p: DISPLAY_NAMES.get(p, p) for p in pairs}
    head = _COMMON_HEAD.format(
        page_title="Monthly Seasonality",
        header_subtitle="Monthly Seasonality",
        updated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        yearly_active='',
        monthly_active='class="active"',
    )
    body = _MONTHLY_BODY.format(
        data_json=json.dumps(all_data),
        pairs_json=json.dumps(pairs),
        labels_json=json.dumps(labels),
        default_pair=default_pair,
    )
    out_path = OUTPUT_DIR / "seasonality_monthly.html"
    out_path.write_text(head + body, encoding="utf-8")
    return out_path


def render_all(prices: dict, default_pair: str = "AUDUSD") -> tuple[Path, Path]:
    """
    Compute seasonality for every pair in `prices` and write two HTML pages
    (yearly + monthly) with all data embedded for dropdown switching.

    `prices`: {symbol: pd.DataFrame with 'Close' column}
    Returns: (yearly_path, monthly_path)
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    all_data: dict[str, dict] = {}
    for sym, df in prices.items():
        if df is None or df.empty:
            continue
        try:
            all_data[sym] = compute_seasonality(df)
        except Exception as e:
            print(f"[seasonality] {sym} compute failed: {e}")
    if not all_data:
        raise RuntimeError("No seasonality data computed for any pair")
    if default_pair not in all_data:
        default_pair = next(iter(sorted(all_data.keys())))
    print(f"[seasonality] computed {len(all_data)} pairs; default = {default_pair}")
    return _render_yearly(all_data, default_pair), _render_monthly(all_data, default_pair)


def render(symbol: str, df: pd.DataFrame, output_path: Path | None = None) -> Path:
    """Backward-compat single-pair render. Internally uses render_all."""
    yearly, _ = render_all({symbol: df}, default_pair=symbol)
    return yearly
