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

from src.output.timefmt import updated_at_str

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

# Metals score US macro releases, not their own (they have no domestic data).
# Each metal reuses the USD economic rows and re-signs the currency-impact chip
# per its EdgeFinder convention (see score_pair.build_currency_scores):
#   Gold / Silver  - safe-haven: invert ALL US macro (strong economy = bearish).
#   Platinum       - industrial / pro-cyclical: growth + jobs mirror USD,
#                    inflation + rates inverted.
# "_default" covers any indicator not listed explicitly.
METALS = ("XAU", "XPT", "XAG")
METAL_IMPACT = {
    "XAU": {"_default": "invert"},
    "XAG": {"_default": "invert"},
    "XPT": {
        "gdp": "mirror", "mpmi": "mirror", "spmi": "mirror",
        "retail_sales": "mirror", "consumer_conf": "mirror",
        "nfp": "mirror", "adp": "mirror", "unemployment_rate": "mirror",
        "jobless_claims": "mirror", "jolts": "mirror",
        "cpi": "invert", "ppi": "invert", "pce": "invert", "rates": "invert",
        "_default": "invert",
    },
}


# Stock indices score US macro releases too, but with the EdgeFinder "stocks"
# direction (growth + jobs up_is_bullish, inflation + rates down_is_bullish), and
# their Interest Rate cell comes from the US 2Y yield, not the rate outlook. The
# index "currency impact" chip IS the stocks impact already computed per row.
INDICES = ("NDX", "UKX")
# Each index reuses its source currency's economic rows, re-signed to the stocks
# direction. NDX scores US macro (USD rows); UKX (FTSE 100) scores UK macro (GBP
# rows). US-only labour rows (NFP/ADP/JOLTS/Claims/PCE) only exist on the USD
# row, so UKX naturally carries no labour rows beyond Unemployment - matching its
# heatmap cells (those stay blank).
INDEX_SOURCE = {"NDX": "USD", "UKX": "GBP"}


def _flip(label: str) -> str:
    """Flip a Bullish/Bearish impact chip; Neutral / n/a pass through."""
    if label == "Bullish":
        return "Bearish"
    if label == "Bearish":
        return "Bullish"
    return label


def _metal_row(usd_row: dict, ind_id: str, metal: str) -> dict:
    """Re-sign a USD economic row for a metal. Data values (US release) are
    shown unchanged; only the currency-impact chip flips when inverted."""
    mode = METAL_IMPACT[metal].get(ind_id, METAL_IMPACT[metal]["_default"])
    row = dict(usd_row)
    if mode == "invert":
        row["currency_impact"] = _flip(usd_row["currency_impact"])
    return row


def _metal_rates_row(treasury_2y, metal: str) -> dict:
    """Interest Rate row for a metal, off the US 2Y Treasury yield vs its 8-day
    SMA (rising yield = hawkish = bearish metal), matching score_pair. The yield
    momentum is read up_is_bullish (rising = strong USD) then re-signed per the
    metal's rates convention (all three invert)."""
    actual = previous = None
    date = ""
    base = "n/a"
    obs = list(treasury_2y or [])
    if len(obs) >= 8:
        yields_asc = [o.value for o in reversed(obs)]
        sma8 = sum(yields_asc[-8:]) / 8
        latest = yields_asc[-1]
        actual = round(latest, 2)
        previous = round(sma8, 2)
        date = getattr(obs[0], "date", "") or ""
        if latest > sma8:
            base = "Bullish"
        elif latest < sma8:
            base = "Bearish"
        else:
            base = "Neutral"
    mode = METAL_IMPACT[metal].get("rates", METAL_IMPACT[metal]["_default"])
    impact = _flip(base) if mode == "invert" else base
    return {
        "indicator": "Interest Rate",
        "date": date,
        "surprise": _surprise_pct(actual, previous),
        "actual": actual,
        "forecast": None,
        "previous": previous,
        "currency_impact": impact,
        "stocks_impact": "n/a",
    }


def _index_row(usd_row: dict, ind_id: str) -> dict:
    """Re-cast a USD economic row for a stock index: the index's currency-impact
    chip is the USD row's stocks impact (growth/jobs un-inverted, inflation
    inverted). Data values (the US release) are shown unchanged."""
    row = dict(usd_row)
    row["currency_impact"] = usd_row.get("stocks_impact", "n/a")
    row["ind_id"] = ind_id
    return row


def _index_rates_row(treasury_2y) -> dict:
    """Interest Rate row for a stock index: the US 2Y Treasury yield vs its
    21-day SMA. Rising yield = hawkish = bearish equities (inverted), matching
    EdgeFinder's "2 Yr Yield (21 day SMA)" index cell."""
    actual = previous = None
    date = ""
    impact = "n/a"
    obs = list(treasury_2y or [])
    if len(obs) >= 21:
        yields_asc = [o.value for o in reversed(obs)]
        sma21 = sum(yields_asc[-21:]) / 21
        latest = yields_asc[-1]
        actual = round(latest, 2)
        previous = round(sma21, 2)
        date = getattr(obs[0], "date", "") or ""
        if latest > sma21:
            impact = "Bearish"
        elif latest < sma21:
            impact = "Bullish"
        else:
            impact = "Neutral"
    return {
        "indicator": "2 Yr Yield (21 day SMA)",
        "date": date,
        "surprise": None,
        "actual": actual,
        "forecast": None,
        "previous": previous,
        "currency_impact": impact,
        "stocks_impact": impact,
        "ind_id": "rates",
    }


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
               investing_pce=None, myfxbook_ppi=None,
               investing_retail_sales=None):
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

    # PPI: Myfxbook for CHF/AUD, Investing for NZD/GBP, TE for the rest
    elif ind_id == "ppi":
        if ccy in ("CHF", "AUD") and (myfxbook_ppi or {}).get(ccy):
            rel = myfxbook_ppi[ccy]
            actual = rel.get("actual")
            forecast = rel.get("consensus")
            previous = rel.get("previous")
            date = rel.get("date") or ""
        elif ccy in ("NZD", "GBP") and (investing_ppi or {}).get(ccy):
            rel = investing_ppi[ccy]
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
    # - Other 7: TE Actual vs Forecast (Consensus, TEForecast fallback)
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
            forecast = rel.get("consensus") or rel.get("forecast")
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

    # PCE: Investing Core PCE Price Index YoY for USD (Actual vs Forecast)
    elif ind_id == "pce" and ccy == "USD" and (investing_pce or {}).get("USD"):
        rel = investing_pce["USD"]
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

    # Benchmark for surprise / impact: Actual vs Forecast, mirroring the Top
    # Setups scorer (EdgeFinder scores every release, PMI included, vs Forecast).
    # EdgeFinder is surprise-only: there is NO Previous fallback. A release with
    # no published forecast (e.g. CAD mPMI, AUD retail) has no surprise to
    # measure, so the chip is Neutral and the surprise reads n/a (matches the
    # scorer's _dir_fcst, which scores those 0). Consumer-conf for non-USD is the
    # one exception that arrives here with forecast set to its Previous value.
    benchmark = forecast
    surprise = _surprise_pct(actual, benchmark)
    if benchmark is None and actual is not None:
        currency_impact = stocks_impact = "Neutral"
    else:
        currency_impact = _impact_label(actual, benchmark, direction)
        stocks_impact = _impact_label(actual, benchmark, stocks_dir)
    return {
        "indicator": ind["label"],
        "date": date,
        "surprise": surprise,
        "actual": actual,
        "forecast": forecast,
        "previous": previous,
        "currency_impact": currency_impact,
        "stocks_impact": stocks_impact,
    }


def build_all(te_history=None, investing_cpi=None, investing_ppi=None,
              investing_mpmi=None, investing_spmi=None, abs_au_mhsi=None,
              rates_outlook=None, investing_cc=None, investing_jolts=None,
              investing_adp=None, investing_pce=None, myfxbook_ppi=None,
              investing_retail_sales=None, treasury_2y=None) -> dict:
    """Return {ccy: [row dicts]} for all currencies plus the metals (XAU/XPT/XAG).

    Metals have no domestic macro, so their rows are the USD release rows with
    the currency-impact chip re-signed per the metal's convention. The metals
    are not added to the economic-heatmap page dropdown (render() filters to the
    fiat CURRENCIES); they exist for the Asset Scorecard's fundamentals tables.
    """
    out: dict[str, list[dict]] = {}
    metals_acc: dict[str, list[dict]] = {m: [] for m in METALS}
    indices_acc: dict[str, list[dict]] = {idx: [] for idx in INDICES}
    for ccy in CURRENCIES:
        rows = []
        for ind in INDICATORS:
            ccys = ind["ccys"]
            if ccys != "all" and ccy not in ccys:
                continue
            row = _build_row(ccy, ind, te_history, investing_cpi, investing_ppi,
                             investing_mpmi, investing_spmi, abs_au_mhsi, rates_outlook,
                             investing_cc=investing_cc, investing_jolts=investing_jolts,
                             investing_adp=investing_adp, investing_pce=investing_pce,
                             myfxbook_ppi=myfxbook_ppi,
                             investing_retail_sales=investing_retail_sales)
            rows.append(row)
            # USD carries every indicator (incl. US-only labour rows), so derive
            # the metal rows from it. Rates is special: metals score it off the
            # 2Y Treasury yield, not the rate outlook.
            if ccy == "USD":
                for m in METALS:
                    if ind["id"] == "rates":
                        metals_acc[m].append(_metal_rates_row(treasury_2y, m))
                    else:
                        metals_acc[m].append(_metal_row(row, ind["id"], m))
            # Stock indices reuse their source currency's rows re-signed to the
            # stocks direction (NDX<-USD, UKX<-GBP); the rate cell comes from the
            # 2Y yield (21-day SMA), not the rate outlook.
            for idx in INDICES:
                if INDEX_SOURCE[idx] != ccy:
                    continue
                if ind["id"] == "rates":
                    indices_acc[idx].append(_index_rates_row(treasury_2y))
                else:
                    indices_acc[idx].append(_index_row(row, ind["id"]))
        out[ccy] = rows
    out.update(metals_acc)
    out.update(indices_acc)
    return out


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Vector | Economic Heatmap</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'><stop offset='0' stop-color='%233d77e8'/><stop offset='1' stop-color='%231e4fd1'/></linearGradient></defs><rect width='32' height='32' rx='7' fill='url(%23g)'/><text x='16' y='23' font-family='Arial' font-size='18' font-weight='bold' fill='white' text-anchor='middle'>V</text></svg>">
<link rel="stylesheet" href="vector.css">
<style>
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
    <div class="brand-mark">V</div>
    <div>
      <div class="brand-name">VECTOR</div>
      <h1>Economic Heatmap</h1>
    </div>
  </div>
  <div class="meta">Updated <b>__UPDATED_AT__</b></div>
</header>

<nav class="topnav">
  <a class="navlink" href="index.html">Top Setups</a>
  <a class="navlink" href="cot.html">COT Detail</a>
  <a class="navlink active" href="economic_heatmap.html">Economic Heatmap</a>
  <a class="navlink" href="scorecard.html">Asset Scorecard</a>
  <a class="navlink" href="inflation.html">Inflation</a>
  <a class="navlink" href="macro.html">Macro Calendar</a>
  <a class="navlink" href="retail.html">Retail Sentiment</a>
  <div class="dropdown">
    <a class="navlink dropbtn" href="#" onclick="event.preventDefault();this.parentElement.classList.toggle('open')">Seasonality &#9662;</a>
    <div class="dropdown-content">
      <a href="seasonality_yearly.html">Yearly Seasonality</a>
      <a href="seasonality_monthly.html">Monthly Seasonality</a>
    </div>
  </div>
  <span class="nav-spacer"></span>
</nav>

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
            .replace("__UPDATED_AT__", updated_at_str())
            .replace("__DATA_JSON__", json.dumps(all_data))
            .replace("__CURRENCIES_JSON__", json.dumps(currencies)))
    output_path.write_text(html, encoding="utf-8")
    return output_path
