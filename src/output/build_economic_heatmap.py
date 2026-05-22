"""
Renders the Economic Heatmap dashboard.

For each of the 8 currencies, builds a table of latest macro releases with:
  indicator | date | surprise | actual | forecast | previous | currency impact | stocks impact

Currency impact uses the same direction logic as the pair-level heatmap:
  up_is_bullish indicators (GDP, PMI, NFP, etc): actual > forecast -> Bullish
  down_is_bullish indicators (Unemployment, Jobless Claims): actual > forecast -> Bearish

Stocks impact uses a separate mapping (some indicators that are bullish for a
currency are bearish for stocks, e.g. high CPI = Fed hike fear = bad for SPX).

Output: data/economic_heatmap.html, single page with currency dropdown.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

OUTPUT_DIR = Path(__file__).resolve().parents[2] / "data"

CURRENCIES = ("USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD")

# Indicators displayed per currency. Order matches what EdgeFinder shows.
# US-only indicators (NFP, ADP, JOLTS, PCE, Jobless Claims) appear only for USD.
INDICATORS = [
    {"id": "gdp",               "label": "GDP Growth",          "direction": "up_is_bullish",   "stocks": "up_is_bullish",   "ccys": "all"},
    {"id": "mpmi",              "label": "Manufacturing PMI",   "direction": "up_is_bullish",   "stocks": "up_is_bullish",   "ccys": "all"},
    {"id": "spmi",              "label": "Services PMI",        "direction": "up_is_bullish",   "stocks": "up_is_bullish",   "ccys": "all"},
    {"id": "retail_sales",      "label": "Retail Sales",        "direction": "up_is_bullish",   "stocks": "up_is_bullish",   "ccys": "all"},
    {"id": "consumer_conf",     "label": "Consumer Confidence", "direction": "up_is_bullish",   "stocks": "up_is_bullish",   "ccys": "all"},
    {"id": "cpi",               "label": "CPI YoY",             "direction": "up_is_bullish",   "stocks": "down_is_bullish", "ccys": "all"},
    {"id": "ppi",               "label": "PPI YoY",             "direction": "up_is_bullish",   "stocks": "down_is_bullish", "ccys": "all"},
    {"id": "pce",               "label": "PCE YoY",             "direction": "up_is_bullish",   "stocks": "down_is_bullish", "ccys": ["USD"]},
    {"id": "rates",             "label": "Interest Rate",       "direction": "up_is_bullish",   "stocks": "down_is_bullish", "ccys": "all"},
    {"id": "unemployment_rate", "label": "Unemployment Rate",   "direction": "down_is_bullish", "stocks": "down_is_bullish", "ccys": "all"},
    {"id": "jobless_claims",    "label": "Jobless Claims",      "direction": "down_is_bullish", "stocks": "down_is_bullish", "ccys": ["USD"]},
    {"id": "nfp",               "label": "Non-Farm Payrolls",   "direction": "up_is_bullish",   "stocks": "up_is_bullish",   "ccys": ["USD"]},
    {"id": "adp",               "label": "ADP Employment",      "direction": "up_is_bullish",   "stocks": "up_is_bullish",   "ccys": ["USD"]},
    {"id": "jolts",             "label": "JOLTS Job Openings",  "direction": "up_is_bullish",   "stocks": "up_is_bullish",   "ccys": ["USD"]},
]


def _impact_label(actual, benchmark, direction: str) -> str:
    """Bullish / Bearish / Neutral based on direction logic."""
    if actual is None or benchmark is None:
        return "n/a"
    if actual > benchmark:
        raw = "Bullish"
    elif actual < benchmark:
        raw = "Bearish"
    else:
        return "Neutral"
    # Flip if direction is down_is_bullish (higher actual = bearish for ccy)
    if direction == "down_is_bullish":
        raw = "Bearish" if raw == "Bullish" else "Bullish"
    return raw


def _surprise_pct(actual, benchmark) -> float | None:
    """(Actual - Forecast) / |Forecast| * 100. None if either is missing."""
    if actual is None or benchmark is None:
        return None
    denom = abs(benchmark) if abs(benchmark) > 1e-9 else max(abs(actual), 1.0)
    return round((actual - benchmark) / denom * 100.0, 2)


def _get_latest_te(te_history, ccy, ind_id):
    """Return latest TE release dict for (ccy, ind_id), or None."""
    rels = (te_history or {}).get(f"{ccy}|{ind_id}", [])
    if not rels:
        return None
    return sorted(rels, key=lambda x: x.get("date", ""), reverse=True)[0]


def _build_row(ccy, ind, te_history, investing_cpi, investing_ppi,
               investing_mpmi, investing_spmi, abs_au_mhsi, rates_outlook,
               investing_cc=None, investing_jolts=None, investing_adp=None,
               myfxbook_ppi=None, investing_retail_sales=None):
    """Return the row dict for one (currency, indicator) pair."""
    ind_id = ind["id"]
    direction = ind["direction"]
    stocks_dir = ind["stocks"]

    actual = forecast = previous = None
    date = ""

    # CPI: Investing.com cache, per-currency
    if ind_id == "cpi":
        rel = (investing_cpi or {}).get(ccy)
        if rel:
            actual = rel.get("actual")
            forecast = rel.get("forecast")
            previous = rel.get("previous")
            date = rel.get("date") or ""

    # PPI: Myfxbook for CHF (Actual vs Consensus), Investing for NZD, TE for the rest
    elif ind_id == "ppi":
        if ccy == "CHF" and (myfxbook_ppi or {}).get("CHF"):
            rel = myfxbook_ppi["CHF"]
            actual = rel.get("actual")
            forecast = rel.get("consensus")
            previous = rel.get("previous")
            date = rel.get("date") or ""
        elif ccy == "NZD":
            rel = (investing_ppi or {}).get("NZD")
            if rel:
                actual = rel.get("actual")
                forecast = rel.get("forecast")
                previous = rel.get("previous")
                date = rel.get("date") or ""
        else:
            rel = _get_latest_te(te_history, ccy, "ppi")
            if rel:
                actual = rel.get("actual")
                forecast = rel.get("consensus") or rel.get("forecast")
                previous = rel.get("previous")
                date = rel.get("date") or ""

    # PMI: Investing caches (fall back to TE history for completeness)
    elif ind_id == "mpmi":
        rel = (investing_mpmi or {}).get(ccy)
        if rel:
            actual = rel.get("actual")
            forecast = rel.get("forecast")
            previous = rel.get("previous")
            date = rel.get("date") or ""
    elif ind_id == "spmi":
        rel = (investing_spmi or {}).get(ccy)
        if rel:
            actual = rel.get("actual")
            forecast = rel.get("forecast")
            previous = rel.get("previous")
            date = rel.get("date") or ""

    # Retail Sales: Investing for CAD, ABS MHSI for AUD, TE for others
    elif ind_id == "retail_sales" and ccy == "CAD" and (investing_retail_sales or {}).get("CAD"):
        rel = investing_retail_sales["CAD"]
        actual = rel.get("actual")
        forecast = rel.get("forecast")
        previous = rel.get("previous")
        date = rel.get("date") or ""
    elif ind_id == "retail_sales" and ccy == "AUD":
        rel = abs_au_mhsi or {}
        actual = rel.get("current_mom")
        forecast = None  # ABS doesn't publish a forecast
        previous = rel.get("previous_mom")
        date = rel.get("current_month") or ""

    # Interest Rates: rates_outlook special case (compare next-meeting forecast to current rate)
    elif ind_id == "rates":
        outlook = (rates_outlook or {}).get(ccy)
        if outlook:
            actual = outlook.get("current")
            forecast = outlook.get("forecast")
            previous = outlook.get("current")  # no "previous" concept here
            date = outlook.get("date") or ""

    # Consumer Conf:
    # - USD: Investing CB Consumer Confidence, Actual vs Forecast (true surprise)
    # - Other 7: TE momentum scoring uses Previous instead of Forecast
    elif ind_id == "consumer_conf" and ccy == "USD" and (investing_cc or {}).get("USD"):
        rel = investing_cc["USD"]
        actual = rel.get("actual")
        forecast = rel.get("forecast")
        previous = rel.get("previous")
        date = rel.get("date") or ""
    elif ind_id == "consumer_conf":
        rel = _get_latest_te(te_history, ccy, "consumer_conf")
        if rel:
            actual = rel.get("actual")
            forecast = rel.get("previous")  # momentum comparison
            previous = rel.get("previous")
            date = rel.get("date") or ""

    # JOLTS: Investing JOLTS Job Openings for USD (Actual vs Forecast)
    elif ind_id == "jolts" and ccy == "USD" and (investing_jolts or {}).get("USD"):
        rel = investing_jolts["USD"]
        actual = rel.get("actual")
        forecast = rel.get("forecast")
        previous = rel.get("previous")
        date = rel.get("date") or ""

    # ADP: Investing ADP Employment Change for USD (Actual vs Forecast)
    elif ind_id == "adp" and ccy == "USD" and (investing_adp or {}).get("USD"):
        rel = investing_adp["USD"]
        actual = rel.get("actual")
        forecast = rel.get("forecast")
        previous = rel.get("previous")
        date = rel.get("date") or ""

    # Everything else: TE history with consensus -> forecast fallback
    else:
        rel = _get_latest_te(te_history, ccy, ind_id)
        if rel:
            actual = rel.get("actual")
            forecast = rel.get("consensus")
            if forecast is None:
                forecast = rel.get("forecast")
            previous = rel.get("previous")
            date = rel.get("date") or ""

    # Benchmark for surprise / impact. PMI is scored as momentum (Actual vs
    # Previous) when no forecast is published, so fall back to previous for
    # mpmi/spmi to keep the row from collapsing to n/a.
    benchmark = forecast
    if benchmark is None:
        benchmark = previous

    surprise = _surprise_pct(actual, benchmark)
    return {
        "indicator": ind["label"],
        "date": date,
        "surprise": surprise,
        "actual": actual,
        "forecast": forecast,
        "previous": previous,
        "currency_impact": _impact_label(actual, benchmark, direction),
        "stocks_impact": _impact_label(actual, benchmark, stocks_dir),
    }


def build_all(te_history=None, investing_cpi=None, investing_ppi=None,
              investing_mpmi=None, investing_spmi=None, abs_au_mhsi=None,
              rates_outlook=None, investing_cc=None, investing_jolts=None,
              investing_adp=None, myfxbook_ppi=None,
              investing_retail_sales=None) -> dict:
    """Return {ccy: [row dicts]} for all currencies."""
    out: dict[str, list[dict]] = {}
    for ccy in CURRENCIES:
        rows = []
        for ind in INDICATORS:
            ccys = ind["ccys"]
            if ccys != "all" and ccy not in ccys:
                continue
            rows.append(_build_row(ccy, ind, te_history, investing_cpi, investing_ppi,
                                   investing_mpmi, investing_spmi, abs_au_mhsi, rates_outlook,
                                   investing_cc=investing_cc, investing_jolts=investing_jolts,
                                   investing_adp=investing_adp, myfxbook_ppi=myfxbook_ppi,
                                   investing_retail_sales=investing_retail_sales))
        out[ccy] = rows
    return out


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Vector | Economic Heatmap</title>
<style>
  :root{
    --bg:#0d1430; --panel:#141a3a; --text:#e8ecff; --muted:#8893c0;
    --row:#1a2046; --rowAlt:#161c3d; --border:#2a3060;
    --bullish:#3974e6; --bearish:#dd5050;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;font-size:13px}
  header{display:flex;align-items:center;justify-content:space-between;padding:18px 24px;background:linear-gradient(135deg,#1a2148 0%,#0d1430 100%);border-bottom:1px solid var(--border)}
  .brand{display:flex;align-items:center;gap:12px}
  .brand-mark{font-size:24px;font-weight:800;color:#3974e6}
  .brand-name{font-size:20px;font-weight:800;letter-spacing:1.5px}
  h1{font-size:14px;color:var(--muted);font-weight:500;margin:0}
  a.btn{color:#aac4ff;text-decoration:none;font-size:13px;border:1px solid var(--border);padding:6px 12px;border-radius:4px;background:var(--row)}
  a.btn:hover{background:var(--rowAlt)}
  .meta{color:var(--muted);font-size:12px}
  .toolbar{display:flex;align-items:center;gap:14px;padding:14px 24px;background:var(--panel);border-bottom:1px solid var(--border);font-size:13px}
  .toolbar label{color:var(--muted);margin-right:6px}
  select{background:var(--row);color:var(--text);border:1px solid var(--border);padding:6px 12px;border-radius:4px;font-size:13px;cursor:pointer}
  select:hover{border-color:#3a4078}
  .content{padding:24px;max-width:1400px;margin:0 auto}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:18px}
  .card h2{margin:0 0 6px 0;font-size:16px;font-weight:600}
  .card .subtitle{color:var(--muted);font-size:11px;margin-bottom:14px}
  table.heat{width:100%;border-collapse:collapse;font-size:12.5px}
  table.heat th{background:var(--row);color:var(--text);font-weight:600;border-bottom:1px solid var(--border);padding:9px 10px;text-align:right;white-space:nowrap}
  table.heat th:first-child{text-align:left}
  table.heat td{padding:7px 10px;text-align:right;border-bottom:1px solid var(--border);white-space:nowrap}
  table.heat td:first-child{text-align:left;font-weight:600}
  table.heat tr:nth-child(odd) td{background:var(--row)}
  table.heat tr:nth-child(even) td{background:var(--rowAlt)}
  .chip{display:inline-block;padding:2px 8px;border-radius:3px;font-weight:600;min-width:64px;text-align:center}
  .chip.bull{background:rgba(57,116,230,0.85);color:#fff}
  .chip.bear{background:rgba(221,80,80,0.85);color:#fff}
  .chip.neut{background:rgba(120,120,140,0.4);color:#cfd6f5}
  .chip.na{background:transparent;color:var(--muted);font-weight:400}
  .surprise.pos{color:#aac4ff}
  .surprise.neg{color:#ffaab4}
  footer{padding:16px 24px;color:var(--muted);font-size:11px;border-top:1px solid var(--border);text-align:center}
</style>
</head>
<body>
<header>
  <div class="brand">
    <div class="brand-mark">V&rarr;</div>
    <div>
      <div class="brand-name">VECTOR</div>
      <h1>Economic Heatmap</h1>
    </div>
  </div>
  <div style="display:flex;align-items:center;gap:14px">
    <div class="meta">Updated: __UPDATED_AT__</div>
    <a class="btn" href="output.html">&larr; Heatmap</a>
  </div>
</header>

<div class="toolbar">
  <div><label>Currency</label><select id="ccySelect"></select></div>
</div>

<div class="content">
  <div class="card">
    <h2 id="heatTitle">USD Economic Heatmap</h2>
    <div class="subtitle">Latest release per indicator. Surprise = (Actual - Forecast) / |Forecast|; PMI rows without a forecast fall back to (Actual - Previous) / |Previous|. Currency impact and Stocks impact use indicator-specific direction logic.</div>
    <table class="heat" id="heatTable">
      <thead>
        <tr>
          <th id="th-ind">Economic Data</th>
          <th>Date</th>
          <th>Surprise</th>
          <th>Actual</th>
          <th>Forecast</th>
          <th>Previous</th>
          <th>Currency Impact</th>
          <th>Stocks Impact</th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>
  </div>
</div>

<footer>Vector &middot; Data: FRED, CFTC, TradingEconomics, Investing.com, ForexFactory &middot; For personal research only, not financial advice.</footer>

<script>
const ALL = __DATA_JSON__;
const CURRENCIES = __CURRENCIES_JSON__;

const sel = document.getElementById('ccySelect');
CURRENCIES.forEach(c => {
  const opt = document.createElement('option');
  opt.value = c; opt.textContent = c;
  if (c === 'USD') opt.selected = true;
  sel.appendChild(opt);
});

// Abbreviate large counts: >=1M -> "6.87M", >=1k -> "209K", else plain.
// Small values (rates, %, index levels, PMI) pass through unchanged.
function abbrevNum(v) {
  const n = Number(v);
  const a = Math.abs(n);
  if (a >= 1e6) return parseFloat((n / 1e6).toFixed(2)) + 'M';
  if (a >= 1e3) return parseFloat((n / 1e3).toFixed(2)) + 'K';
  return parseFloat(n.toFixed(2)).toString();
}

function fmt(v) {
  if (v === null || v === undefined) return '<span style="color:#666">n/a</span>';
  return abbrevNum(v);
}

function chip(label) {
  const cls = label === 'Bullish' ? 'bull' : label === 'Bearish' ? 'bear' : label === 'Neutral' ? 'neut' : 'na';
  return `<span class="chip ${cls}">${label}</span>`;
}

function render(ccy) {
  document.getElementById('heatTitle').textContent = ccy + ' Economic Heatmap';
  document.getElementById('th-ind').textContent = ccy + ' Economic Data';
  const tbody = document.querySelector('#heatTable tbody');
  tbody.innerHTML = '';
  const rows = ALL[ccy] || [];
  rows.forEach(r => {
    const surpCls = r.surprise === null ? '' : (r.surprise > 0 ? 'pos' : (r.surprise < 0 ? 'neg' : ''));
    const surpText = r.surprise === null ? 'n/a' : (r.surprise > 0 ? '+' : '') + r.surprise.toFixed(2) + '%';
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${r.indicator}</td>
      <td>${r.date || '<span style="color:#666">n/a</span>'}</td>
      <td><span class="surprise ${surpCls}">${surpText}</span></td>
      <td>${fmt(r.actual)}</td>
      <td>${fmt(r.forecast)}</td>
      <td>${fmt(r.previous)}</td>
      <td>${chip(r.currency_impact)}</td>
      <td>${chip(r.stocks_impact)}</td>
    `;
    tbody.appendChild(tr);
  });
}

sel.addEventListener('change', e => render(e.target.value));
render('USD');
</script>
</body>
</html>
"""


def render(all_data: dict, output_path: Path | None = None) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = output_path or (OUTPUT_DIR / "economic_heatmap.html")
    currencies = [c for c in CURRENCIES if c in all_data]
    # Plain string replacement avoids the {} escaping headaches that .format()
    # creates with embedded CSS/JS braces.
    html = (_HTML_TEMPLATE
            .replace("__UPDATED_AT__", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
            .replace("__DATA_JSON__", json.dumps(all_data))
            .replace("__CURRENCIES_JSON__", json.dumps(currencies)))
    output_path.write_text(html, encoding="utf-8")
    return output_path
