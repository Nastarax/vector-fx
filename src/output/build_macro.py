"""
Macro Calendar page (data/macro.html).

A deliberately simple month-grid calendar: the current month, one cell per day,
with a small chip for every economic release and rate decision, colour-coded by
currency. Three event sources are merged at build time:

  1. Released prints (econ_data): the latest actual release per indicator per
     currency, with actual vs forecast and Vector's impact in the tooltip.
  2. Estimated upcoming releases (data/cache/release_calendar.json): each cell's
     next expected release date (dashed chip = estimated, not yet published).
  3. Rate decisions (MEET below): exact central-bank decision dates for the 8
     currencies (star chip).

The month grid itself is rendered client-side from the browser's date, so the
"today" highlight and default month stay correct between builds; prev/next
buttons walk other months. Events are passed as JSON and filtered per month in
the browser.
"""
from __future__ import annotations

import json
from datetime import datetime, date, timezone
from pathlib import Path

from src.output.timefmt import updated_at_str
from src.fetchers.release_calendar import load_calendar

OUTPUT_DIR = Path(__file__).resolve().parents[2] / "data"

CCY_ORDER = ("USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD")

# ---- Static: 2026 central-bank rate-decision calendar ------------------------
# [ccy, bank, short, ISO date, type(full|plain), tag]
# type "full" = projections / forecast / report meeting (bigger surprise potential)
MEET = [
    # USD - Federal Reserve (FOMC)
    ["USD", "Federal Reserve", "Fed", "2026-01-28", "plain", ""],
    ["USD", "Federal Reserve", "Fed", "2026-03-18", "full", "Projections"],
    ["USD", "Federal Reserve", "Fed", "2026-04-29", "plain", ""],
    ["USD", "Federal Reserve", "Fed", "2026-06-17", "full", "Dot plot"],
    ["USD", "Federal Reserve", "Fed", "2026-07-29", "plain", ""],
    ["USD", "Federal Reserve", "Fed", "2026-09-16", "full", "Dot plot"],
    ["USD", "Federal Reserve", "Fed", "2026-10-28", "plain", ""],
    ["USD", "Federal Reserve", "Fed", "2026-12-09", "full", "Dot plot"],
    # EUR - ECB
    ["EUR", "ECB", "ECB", "2026-03-19", "full", "Projections"],
    ["EUR", "ECB", "ECB", "2026-04-30", "plain", ""],
    ["EUR", "ECB", "ECB", "2026-06-11", "full", "Projections"],
    ["EUR", "ECB", "ECB", "2026-07-23", "plain", ""],
    ["EUR", "ECB", "ECB", "2026-09-10", "full", "Projections"],
    ["EUR", "ECB", "ECB", "2026-10-29", "plain", ""],
    ["EUR", "ECB", "ECB", "2026-12-17", "full", "Projections"],
    # GBP - Bank of England
    ["GBP", "Bank of England", "BoE", "2026-02-05", "full", "MPR"],
    ["GBP", "Bank of England", "BoE", "2026-03-19", "plain", ""],
    ["GBP", "Bank of England", "BoE", "2026-04-30", "full", "MPR"],
    ["GBP", "Bank of England", "BoE", "2026-06-18", "plain", ""],
    ["GBP", "Bank of England", "BoE", "2026-07-30", "full", "MPR"],
    ["GBP", "Bank of England", "BoE", "2026-09-17", "plain", ""],
    ["GBP", "Bank of England", "BoE", "2026-11-05", "full", "MPR"],
    ["GBP", "Bank of England", "BoE", "2026-12-17", "plain", ""],
    # JPY - Bank of Japan
    ["JPY", "Bank of Japan", "BoJ", "2026-01-23", "full", "Outlook"],
    ["JPY", "Bank of Japan", "BoJ", "2026-03-19", "plain", ""],
    ["JPY", "Bank of Japan", "BoJ", "2026-04-28", "full", "Outlook"],
    ["JPY", "Bank of Japan", "BoJ", "2026-06-16", "plain", ""],
    ["JPY", "Bank of Japan", "BoJ", "2026-07-31", "full", "Outlook"],
    ["JPY", "Bank of Japan", "BoJ", "2026-09-18", "plain", ""],
    ["JPY", "Bank of Japan", "BoJ", "2026-10-30", "full", "Outlook"],
    ["JPY", "Bank of Japan", "BoJ", "2026-12-18", "plain", ""],
    # CHF - SNB (quarterly)
    ["CHF", "SNB", "SNB", "2026-03-19", "full", "Forecast"],
    ["CHF", "SNB", "SNB", "2026-06-18", "full", "Forecast"],
    ["CHF", "SNB", "SNB", "2026-09-24", "full", "Forecast"],
    ["CHF", "SNB", "SNB", "2026-12-11", "full", "Forecast"],
    # CAD - Bank of Canada
    ["CAD", "Bank of Canada", "BoC", "2026-01-28", "full", "MPR"],
    ["CAD", "Bank of Canada", "BoC", "2026-03-18", "plain", ""],
    ["CAD", "Bank of Canada", "BoC", "2026-04-29", "full", "MPR"],
    ["CAD", "Bank of Canada", "BoC", "2026-06-10", "plain", ""],
    ["CAD", "Bank of Canada", "BoC", "2026-07-15", "full", "MPR"],
    ["CAD", "Bank of Canada", "BoC", "2026-09-02", "plain", ""],
    ["CAD", "Bank of Canada", "BoC", "2026-10-28", "full", "MPR"],
    ["CAD", "Bank of Canada", "BoC", "2026-12-09", "plain", ""],
    # AUD - RBA
    ["AUD", "RBA", "RBA", "2026-02-03", "full", "SoMP"],
    ["AUD", "RBA", "RBA", "2026-03-17", "plain", ""],
    ["AUD", "RBA", "RBA", "2026-05-05", "full", "SoMP"],
    ["AUD", "RBA", "RBA", "2026-06-16", "plain", ""],
    ["AUD", "RBA", "RBA", "2026-08-11", "full", "SoMP"],
    ["AUD", "RBA", "RBA", "2026-09-29", "plain", ""],
    ["AUD", "RBA", "RBA", "2026-11-03", "full", "SoMP"],
    ["AUD", "RBA", "RBA", "2026-12-08", "plain", ""],
    # NZD - RBNZ
    ["NZD", "RBNZ", "RBNZ", "2026-04-08", "plain", ""],
    ["NZD", "RBNZ", "RBNZ", "2026-05-27", "full", "MPS"],
    ["NZD", "RBNZ", "RBNZ", "2026-07-08", "plain", ""],
    ["NZD", "RBNZ", "RBNZ", "2026-09-02", "full", "MPS"],
    ["NZD", "RBNZ", "RBNZ", "2026-10-28", "plain", ""],
    ["NZD", "RBNZ", "RBNZ", "2026-12-09", "full", "MPS"],
]

# Long indicator label -> short chip text.
_SHORT = {
    "GDP Growth": "GDP",
    "Manufacturing PMI": "Mfg PMI",
    "Services PMI": "Svc PMI",
    "Retail Sales": "Retail",
    "Household Spending": "Household",
    "Consumer Confidence": "Conf",
    "CPI YoY": "CPI",
    "PPI YoY": "PPI",
    "PCE YoY": "PCE",
    "Unemployment Rate": "Unemp",
    "Jobless Claims": "Claims",
    "Non-Farm Payrolls": "NFP",
    "ADP Employment": "ADP",
    "JOLTS Job Openings": "JOLTS",
}


def _short(label: str) -> str:
    if not label:
        return ""
    if label in _SHORT:
        return _SHORT[label]
    # Fallback: strip common suffixes, cap length.
    s = label.replace(" YoY", "").replace(" Rate", "").replace(" Change", "")
    return s if len(s) <= 10 else s[:9] + "…"


def _calendar_events(econ_data) -> list[dict]:
    """Merge released prints + estimated upcoming + rate decisions into a flat
    list of dated events for the client-side month grid."""
    events: list[dict] = []
    today_iso = date.today().strftime("%Y-%m-%d")

    # 1. Released prints (actual, with values + impact). econ_data = {ccy: [rows]}.
    #    Only the 8 fiat currencies: indices/metals (XAU, NDX, UKX, ...) reuse a
    #    fiat's cells, so including them would duplicate every release.
    for ccy, rows in (econ_data or {}).items():
        if ccy not in CCY_ORDER:
            continue
        for r in rows:
            d = r.get("date")
            label = r.get("indicator")
            if not d or not label:
                continue
            if label == "Interest Rate":
                continue  # rate decisions come from MEET (exact dates)
            events.append({
                "ccy": ccy,
                "date": d,
                "label": label,
                "short": _short(label),
                "kind": "actual",
                "actual": r.get("actual"),
                "forecast": r.get("forecast"),
                "impact": r.get("currency_impact"),
            })

    # 2. Estimated upcoming releases (dashed chip). Future dates only; actuals
    #    already cover what has printed.
    seen = {(e["ccy"], e["date"], e["label"]) for e in events}
    cal = load_calendar() or {}
    for e in (cal.get("entries") or {}).values():
        if e.get("indicator") == "rates":
            continue
        nxt = e.get("next_release")
        ccy = e.get("currency")
        label = e.get("label")
        if not nxt or not ccy or nxt < today_iso:
            continue
        if (ccy, nxt, label) in seen:
            continue
        events.append({
            "ccy": ccy,
            "date": nxt,
            "label": label,
            "short": _short(label),
            "kind": "estimated",
        })

    # 3. Rate decisions (star chip, exact dates).
    for ccy, bank, short, d, typ, tag in MEET:
        events.append({
            "ccy": ccy,
            "date": d,
            "label": bank + " rate decision" + (" (" + tag + ")" if tag else ""),
            "short": short,
            "kind": "decision",
            "heavy": typ == "full",
        })

    return events


_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Vector | Macro Calendar</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'><stop offset='0' stop-color='%233d77e8'/><stop offset='1' stop-color='%231e4fd1'/></linearGradient></defs><rect width='32' height='32' rx='7' fill='url(%23g)'/><text x='16' y='23' font-family='Arial' font-size='18' font-weight='bold' fill='white' text-anchor='middle'>V</text></svg>">
<link rel="stylesheet" href="vector.css">
<style>
  :root{
    --accent2:#6f9bff;
    --usd:#3974e6; --eur:#5b8def; --gbp:#9a7bf0; --jpy:#dd5050;
    --chf:#e07a3f; --cad:#d6455f; --aud:#2fa86b; --nzd:#2f9aa8;
  }
  main{max-width:1180px;margin:0 auto;padding:22px 24px 60px}
  .lead{color:var(--muted);font-size:13px;margin:0 0 18px;max-width:820px}

  .calbar{display:flex;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap}
  .mtitle{font-size:19px;font-weight:800;min-width:172px}
  .navbtn{background:var(--panel);border:1px solid var(--border);color:var(--text);
    border-radius:8px;width:34px;height:32px;font-size:16px;cursor:pointer;line-height:1}
  .navbtn:hover{border-color:var(--accent)}
  .today-btn{background:var(--panel);border:1px solid var(--border);color:var(--text);
    border-radius:8px;height:32px;padding:0 12px;font-size:12px;font-weight:600;cursor:pointer}
  .today-btn:hover{border-color:var(--accent)}

  .filters{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 12px;align-items:center}
  .filters .flabel{color:var(--muted);font-size:11px;margin-right:2px}
  .fchip{cursor:pointer;user-select:none;padding:3px 10px;border-radius:20px;font-size:11px;
    font-weight:700;color:#fff;border:1px solid transparent}
  .fchip.off{opacity:.26}

  .legend{display:flex;gap:18px;flex-wrap:wrap;color:var(--muted);font-size:11px;margin-bottom:12px}
  .legend span{display:inline-flex;align-items:center;gap:6px}
  .lg-solid{width:20px;height:11px;border-radius:3px;background:var(--accent2)}
  .lg-dash{width:20px;height:11px;border-radius:3px;border:1px dashed var(--accent2)}
  .lg-star{color:#ffcf5c;font-size:12px}

  .grid7{display:grid;grid-template-columns:repeat(7,1fr);gap:6px}
  .dow{font-size:11px;font-weight:700;color:var(--muted);padding:2px 4px;
    text-transform:uppercase;letter-spacing:.4px}
  .cell{min-height:104px;background:var(--panel);border:1px solid var(--border);
    border-radius:8px;padding:6px 6px 8px;display:flex;flex-direction:column;gap:4px}
  .cell.blank{background:transparent;border:none}
  .cell.wknd{background:transparent;border-style:dashed;opacity:.65}
  .cell.today{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
  .cell.has{cursor:pointer}
  .cell.has:hover{border-color:var(--accent2)}
  .cell .dnum{font-size:12px;font-weight:700;color:var(--muted);display:flex;
    justify-content:space-between;align-items:center}
  .cell.today .dnum{color:var(--accent2)}
  .cell .dnum .tdtag{font-size:9px;font-weight:800;color:var(--accent2);letter-spacing:.4px}
  .chips{display:flex;flex-direction:column;gap:3px}
  .chip2{font-size:10.5px;font-weight:700;color:#fff;border-radius:4px;padding:2px 5px;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.3;cursor:default}
  .chip2 .cc{font-weight:800;opacity:.9;margin-right:4px}
  .chip2.est{background:transparent!important;border:1px dashed currentColor}
  .chip2.dec .st{color:#ffcf5c;margin-right:3px}
  .chip2.more{background:transparent;color:var(--muted);font-weight:600;padding:1px 5px}

  /* Day detail popup */
  .modal-back{position:fixed;inset:0;background:rgba(4,8,20,.62);display:none;
    align-items:flex-start;justify-content:center;z-index:50;padding:56px 16px;overflow-y:auto}
  .modal-back.open{display:flex}
  .modal{background:var(--panel);border:1px solid var(--border);border-radius:12px;
    max-width:480px;width:100%;box-shadow:0 18px 50px rgba(0,0,0,.5)}
  .modal .mh{display:flex;justify-content:space-between;align-items:center;
    padding:15px 18px;border-bottom:1px solid var(--border)}
  .modal .mh h3{margin:0;font-size:15px;font-weight:800}
  .modal .mh .x{background:none;border:none;color:var(--muted);font-size:21px;
    cursor:pointer;line-height:1}
  .modal .mh .x:hover{color:var(--text)}
  .modal .mb{padding:4px 18px 16px}
  .ev-item{padding:12px 0;border-top:1px solid var(--border);display:flex;gap:10px}
  .ev-item:first-child{border-top:none}
  .ev-item .cc2{display:inline-block;padding:2px 7px;border-radius:5px;font-size:11px;
    font-weight:800;color:#fff;margin-top:1px}
  .ev-item .body{flex:1}
  .ev-item .t{font-size:13px;font-weight:700;display:flex;align-items:center;gap:7px;flex-wrap:wrap}
  .ev-item .badge{font-size:9px;font-weight:800;text-transform:uppercase;letter-spacing:.4px;
    padding:1px 6px;border-radius:4px}
  .badge.rel{background:rgba(57,116,230,.22);color:#8fb4ff}
  .badge.est{background:transparent;border:1px dashed var(--muted);color:var(--muted)}
  .badge.dec{background:rgba(255,207,92,.16);color:#ffcf5c}
  .ev-item .vals{font-size:12px;color:var(--text);margin-top:3px}
  .ev-item .desc{font-size:12px;color:var(--muted);margin-top:4px;line-height:1.45}
  .modal .empty2{color:var(--muted);font-size:13px;padding:16px 0}

  footer{color:var(--muted);font-size:11px;text-align:center;margin-top:40px;
    border-top:1px solid var(--border);padding-top:16px}

  @media (max-width:760px){
    main{padding:16px 12px 48px}
    .cell{min-height:78px;padding:4px 4px 6px}
    .chip2{font-size:9.5px;padding:1px 4px}
    .dow{font-size:9.5px}
    .mtitle{font-size:16px;min-width:140px}
  }
</style>
<script defer src="analytics.js"></script>
</head>
<body>
<header>
  <div class="brand">
    <div class="brand-mark">V</div>
    <div>
      <div class="brand-name">VECTOR</div>
      <h1>Macro Calendar</h1>
    </div>
  </div>
  <div class="meta">Updated <b>__UPDATED_AT__</b></div>
</header>

<nav class="topnav">
  <a class="navlink" href="index.html">Top Setups</a>
  <a class="navlink" href="cot.html">COT Detail</a>
  <a class="navlink" href="economic_heatmap.html">Economic Heatmap</a>
  <a class="navlink" href="scorecard.html">Asset Scorecard</a>
  <a class="navlink" href="inflation.html">Inflation</a>
  <a class="navlink active" href="macro.html">Macro Calendar</a>
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

<main>
  <p class="lead">Every economic release and rate decision for your eight currencies, on one month.
    Solid chips are released prints, dashed chips are estimated upcoming releases, and a star marks a
    central-bank rate decision. Tap a currency to filter.</p>

  <div class="calbar">
    <button class="navbtn" id="prev" aria-label="Previous month">&#8249;</button>
    <div class="mtitle" id="mtitle"></div>
    <button class="navbtn" id="next" aria-label="Next month">&#8250;</button>
    <button class="today-btn" id="todayBtn">Today</button>
  </div>

  <div class="filters" id="filters"><span class="flabel">Show:</span></div>

  <div class="legend">
    <span><span class="lg-solid"></span> Released</span>
    <span><span class="lg-dash"></span> Estimated</span>
    <span><span class="lg-star">&#9733;</span> Rate decision</span>
  </div>

  <div class="grid7" id="dowRow"></div>
  <div class="grid7" id="grid" style="margin-top:6px"></div>

  <footer>
    Vector &middot; Macro Calendar &middot; released prints + estimated release dates regenerate every
    build; rate-decision dates verified against official central-bank calendars (May 2026). Estimated
    dates are cadence-based and can shift; confirm the exact time on your calendar of record.
  </footer>
</main>

<div class="modal-back" id="modalBack">
  <div class="modal" role="dialog" aria-modal="true">
    <div class="mh"><h3 id="modalTitle"></h3><button class="x" id="modalX" aria-label="Close">&times;</button></div>
    <div class="mb" id="modalBody"></div>
  </div>
</div>

<script>
const EVENTS = __EVENTS_JSON__;
const CCY_ORDER = ["USD","EUR","GBP","JPY","CHF","CAD","AUD","NZD"];
const CCY_COLOR = {USD:"var(--usd)",EUR:"var(--eur)",GBP:"var(--gbp)",JPY:"var(--jpy)",
  CHF:"var(--chf)",CAD:"var(--cad)",AUD:"var(--aud)",NZD:"var(--nzd)"};
const MON=["January","February","March","April","May","June","July","August","September","October","November","December"];
const DOW=["Mon","Tue","Wed","Thu","Fri","Sat","Sun"];
const WD=["Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"];
const MAX_CHIPS=4;

// One-line plain-English description per indicator (keyed by chip short code).
const DESC={
  "GDP":"Total economic output growth. The broadest read on the economy.",
  "Mfg PMI":"Factory-sector survey; above 50 signals expansion. Fast and forward-looking.",
  "Svc PMI":"Services-sector survey; above 50 signals expansion. Services lead most economies.",
  "Retail":"Consumer spending at retailers. A direct read on demand.",
  "Household":"Japanese household spending, a key BoJ demand gauge.",
  "Conf":"Consumer confidence survey. Optimism today leads spending tomorrow.",
  "CPI":"Headline consumer inflation. The central bank's primary target.",
  "PPI":"Producer prices at the factory gate. An early pipeline signal for CPI.",
  "PCE":"The Fed's preferred inflation gauge.",
  "Unemp":"Unemployment rate. Lower means a tighter labour market.",
  "Claims":"Weekly new jobless filings. A high-frequency labour pulse.",
  "NFP":"US monthly jobs added. The single biggest scheduled USD data release.",
  "ADP":"US private-payrolls estimate, released two days before NFP.",
  "JOLTS":"US job openings. A gauge of labour demand."
};

// Index events by date string.
const byDate={};
EVENTS.forEach(e=>{(byDate[e.date]=byDate[e.date]||[]).push(e);});

// State: which month is shown + which currencies are hidden.
const now=new Date();
let viewY=now.getFullYear(), viewM=now.getMonth();
const hidden=new Set();

function pad(n){return (n<10?"0":"")+n;}
function iso(y,m,d){return y+"-"+pad(m+1)+"-"+pad(d);}
function todayISO(){const t=new Date();return iso(t.getFullYear(),t.getMonth(),t.getDate());}

// Weekday header (Mon-first).
document.getElementById("dowRow").innerHTML = DOW.map(d=>'<div class="dow">'+d+'</div>').join("");

// Currency filter chips.
const filtersEl=document.getElementById("filters");
CCY_ORDER.forEach(c=>{
  const b=document.createElement("span");
  b.className="fchip"; b.textContent=c;
  b.style.background=CCY_COLOR[c];
  b.onclick=()=>{ if(hidden.has(c)){hidden.delete(c);b.classList.remove("off");}
    else{hidden.add(c);b.classList.add("off");} render(); };
  filtersEl.appendChild(b);
});

function chipHTML(e){
  const col=CCY_COLOR[e.ccy]||"var(--neutral)";
  if(e.kind==="decision"){
    let tip=e.ccy+" · "+e.label;
    return '<div class="chip2 dec" style="background:'+col+'" title="'+tip+'">'+
      '<span class="st">&#9733;</span><span class="cc">'+e.ccy+'</span>'+e.short+'</div>';
  }
  if(e.kind==="estimated"){
    let tip=e.ccy+" · "+e.label+" · estimated";
    return '<div class="chip2 est" style="color:'+col+'" title="'+tip+'">'+
      '<span class="cc">'+e.ccy+'</span>'+e.short+'</div>';
  }
  // actual
  const a=fmt(e.actual), f=fmt(e.forecast);
  let tip=e.ccy+" · "+e.label+"  (actual "+a+" vs forecast "+f+")"+
    (e.impact?"  → "+e.impact:"");
  return '<div class="chip2" style="background:'+col+'" title="'+tip+'">'+
    '<span class="cc">'+e.ccy+'</span>'+e.short+'</div>';
}
function fmt(v){ if(v===null||v===undefined) return "n/a";
  const n=Number(v),a=Math.abs(n);
  if(a>=1e6) return parseFloat((n/1e6).toFixed(2))+"M";
  if(a>=1e3) return parseFloat((n/1e3).toFixed(2))+"K";
  return parseFloat(n.toFixed(2)).toString(); }

function render(){
  document.getElementById("mtitle").textContent = MON[viewM]+" "+viewY;
  const first=new Date(viewY,viewM,1);
  const lead=(first.getDay()+6)%7;              // Mon-first offset
  const dim=new Date(viewY,viewM+1,0).getDate(); // days in month
  const tISO=todayISO();
  const order={decision:0,actual:1,estimated:2};
  let cells="";
  for(let i=0;i<lead;i++) cells+='<div class="cell blank"></div>';
  for(let d=1;d<=dim;d++){
    const ds=iso(viewY,viewM,d);
    const dow=new Date(viewY,viewM,d).getDay();
    const wknd=(dow===0||dow===6);
    let evs=(byDate[ds]||[]).filter(e=>!hidden.has(e.ccy));
    evs.sort((a,b)=>(order[a.kind]-order[b.kind])||CCY_ORDER.indexOf(a.ccy)-CCY_ORDER.indexOf(b.ccy));
    let chips="";
    if(evs.length){
      const show=evs.slice(0,MAX_CHIPS);
      chips=show.map(chipHTML).join("");
      if(evs.length>MAX_CHIPS){
        const rest=evs.slice(MAX_CHIPS).map(e=>e.ccy+" "+e.short).join(", ");
        chips+='<div class="chip2 more" title="'+rest+'">+'+(evs.length-MAX_CHIPS)+' more</div>';
      }
    }
    const cls="cell"+(ds===tISO?" today":"")+(wknd?" wknd":"")+(evs.length?" has":"");
    const tag=(ds===tISO?'<span class="tdtag">TODAY</span>':'');
    cells+='<div class="'+cls+'" data-date="'+ds+'"><div class="dnum"><span>'+d+'</span>'+tag+'</div>'+
      '<div class="chips">'+chips+'</div></div>';
  }
  document.getElementById("grid").innerHTML=cells;
}

// ---- Day detail popup -------------------------------------------------------
const order2={decision:0,actual:1,estimated:2};
function prettyDate(ds){const[y,m,d]=ds.split("-").map(Number);
  const dt=new Date(y,m-1,d);return WD[dt.getDay()]+", "+d+" "+MON[m-1]+" "+y;}
function evDesc(e){
  if(e.kind==="decision")
    return "Scheduled central-bank rate decision, the biggest driver for "+e.ccy+"."+
      (e.heavy?" New forecasts/projections are published at this meeting.":"");
  return DESC[e.short]||"";
}
function dayItems(ds){
  let evs=(byDate[ds]||[]).filter(e=>!hidden.has(e.ccy));
  if(!evs.length) return '<div class="empty2">No releases for your selected currencies on this day.</div>';
  evs.sort((a,b)=>(order2[a.kind]-order2[b.kind])||CCY_ORDER.indexOf(a.ccy)-CCY_ORDER.indexOf(b.ccy));
  return evs.map(e=>{
    const col=CCY_COLOR[e.ccy]||"var(--neutral)";
    const badge = e.kind==="decision"?'<span class="badge dec">Decision</span>'
      : e.kind==="estimated"?'<span class="badge est">Estimated</span>'
      : '<span class="badge rel">Released</span>';
    let vals="";
    if(e.kind==="actual")
      vals='<div class="vals">Actual '+fmt(e.actual)+' vs forecast '+fmt(e.forecast)+
        (e.impact?' &middot; '+e.impact+' for '+e.ccy:'')+'</div>';
    else if(e.kind==="estimated")
      vals='<div class="vals">Estimated date, not yet published.</div>';
    const desc=evDesc(e);
    return '<div class="ev-item"><span class="cc2" style="background:'+col+'">'+e.ccy+'</span>'+
      '<div class="body"><div class="t">'+e.label+' '+badge+'</div>'+vals+
      (desc?'<div class="desc">'+desc+'</div>':'')+'</div></div>';
  }).join("");
}
const back=document.getElementById("modalBack");
function openDay(ds){
  document.getElementById("modalTitle").textContent=prettyDate(ds);
  document.getElementById("modalBody").innerHTML=dayItems(ds);
  back.classList.add("open");
}
function closeModal(){back.classList.remove("open");}
document.getElementById("grid").addEventListener("click",ev=>{
  const cell=ev.target.closest(".cell.has"); if(!cell) return;
  openDay(cell.getAttribute("data-date"));
});
back.addEventListener("click",ev=>{ if(ev.target===back) closeModal(); });
document.getElementById("modalX").onclick=closeModal;
document.addEventListener("keydown",ev=>{ if(ev.key==="Escape") closeModal(); });

document.getElementById("prev").onclick=()=>{ if(--viewM<0){viewM=11;viewY--;} closeModal(); render(); };
document.getElementById("next").onclick=()=>{ if(++viewM>11){viewM=0;viewY++;} closeModal(); render(); };
document.getElementById("todayBtn").onclick=()=>{ const t=new Date();viewY=t.getFullYear();viewM=t.getMonth(); closeModal(); render(); };

render();
</script>
</body>
</html>
"""


def render(currency_rows=None, econ_data=None, output_path: Path | None = None) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = output_path or (OUTPUT_DIR / "macro.html")
    html = (_HTML
            .replace("__UPDATED_AT__", updated_at_str())
            .replace("__EVENTS_JSON__", json.dumps(_calendar_events(econ_data))))
    output_path.write_text(html, encoding="utf-8")
    return output_path
