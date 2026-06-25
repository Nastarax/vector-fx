"""
Retail Sentiment page (data/retail.html).

EdgeFinder-style contrarian retail-positioning view: one horizontal bar per
instrument, blue = crowd Long%, red = crowd Short%, sorted most-long at the top.
The left label chip is coloured by the CONTRARIAN signal (Vector's crowd score):
crowd heavily long (>=60%) -> bearish (orange), heavily short (<=40% long) ->
bullish (blue), mixed -> neutral (grey).

Data is the combined Myfxbook + Forexbenchmark feed (the same readings that drive
the heatmap's Crowd cell). Only instruments with real source coverage are shown
(the 28 FX pairs + Gold); indices/metals use a COT-based crowd proxy instead and
are not part of the retail-broker feed.
"""
from __future__ import annotations

import json
from pathlib import Path

from src.output.timefmt import updated_at_str

OUTPUT_DIR = Path(__file__).resolve().parents[2] / "data"
CACHE_DIR = OUTPUT_DIR / "cache"

# Friendlier labels for non-FX symbols (uppercased in CSS).
_LABELS = {"XAUUSD": "Gold"}


def _covered() -> set[str]:
    """Symbols with real retail data in either source (not the 50/50 fallback)."""
    cov: set[str] = set()
    for fn in ("myfxbook_outlook.json", "forexbenchmark_outlook.json"):
        try:
            cov |= set(json.load(open(CACHE_DIR / fn)))
        except Exception:
            pass
    return cov


def _signal(long_pct: float) -> str:
    """Contrarian signal class, matching score_sentiment.retail_score (60/40)."""
    if long_pct >= 60:
        return "bear"      # crowd heavily long -> contrarian bearish
    if long_pct <= 40:
        return "bull"      # crowd heavily short -> contrarian bullish
    return "neut"


def _fmt(pct: float) -> str:
    """1 decimal, trailing .0 trimmed (89.0 -> '89%', 87.11 -> '87.1%')."""
    s = f"{pct:.1f}".rstrip("0").rstrip(".")
    return f"{s}%"


def _rows_html(readings: dict) -> str:
    covered = _covered()
    rows = [(sym, r.long_pct, r.short_pct) for sym, r in readings.items()
            if sym in covered]
    rows.sort(key=lambda x: x[1], reverse=True)
    if not rows:
        return '<div class="rs-empty">No retail data available.</div>'

    out = []
    for sym, lng, sht in rows:
        label = _LABELS.get(sym, sym)
        sig = _signal(lng)
        out.append(
            f'<div class="rs-row" data-sym="{sym}">'
            f'<div class="rs-label {sig}">{label}</div>'
            f'<div class="rs-bar">'
            f'<div class="rs-long" style="width:{lng:.4f}%"><span>{_fmt(lng)}</span></div>'
            f'<div class="rs-short" style="width:{sht:.4f}%"><span>{_fmt(sht)}</span></div>'
            f'</div></div>'
        )
    return "\n".join(out)


def render(retail_readings: dict, output_path: Path | None = None) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = output_path or (OUTPUT_DIR / "retail.html")
    html = (_HTML
            .replace("__UPDATED_AT__", updated_at_str())
            .replace("__ROWS__", _rows_html(retail_readings)))
    output_path.write_text(html, encoding="utf-8")
    return output_path


_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Vector | Retail Sentiment</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'><stop offset='0' stop-color='%233d77e8'/><stop offset='1' stop-color='%231e4fd1'/></linearGradient></defs><rect width='32' height='32' rx='7' fill='url(%23g)'/><text x='16' y='23' font-family='Arial' font-size='18' font-weight='bold' fill='white' text-anchor='middle'>V</text></svg>">
<link rel="stylesheet" href="vector.css">
<style>
  body{line-height:1.5}
  main{max-width:1120px;margin:0 auto;padding:22px 24px 60px}
  .rs-top{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:14px}
  .rs-intro h2{font-size:16px;font-weight:700;margin:0 0 4px;display:flex;align-items:center;gap:9px}
  .rs-intro h2 .dot{width:8px;height:8px;border-radius:50%;background:var(--accent)}
  .rs-intro p{color:var(--muted);font-size:12.5px;margin:0;max-width:680px}
  .rs-search{background:var(--row);color:var(--text);border:1px solid var(--border);
    padding:8px 12px;border-radius:7px;font-size:12.5px;min-width:170px}
  .rs-search:focus{outline:none;border-color:var(--accent)}

  .rs-legend{display:flex;gap:16px;align-items:center;margin:0 0 14px;font-size:11.5px;color:var(--muted)}
  .rs-legend .k{display:inline-flex;align-items:center;gap:6px}
  .rs-legend .sw{width:11px;height:11px;border-radius:3px;display:inline-block}
  .sw.long{background:var(--bull)} .sw.short{background:var(--bear)}
  .sw.sbear{background:#e2571f} .sw.sbull{background:var(--bull)} .sw.sneut{background:#5a627e}

  .rs-list{display:flex;flex-direction:column;gap:6px}
  .rs-row{display:flex;align-items:stretch;height:30px}
  .rs-label{flex:0 0 134px;display:flex;align-items:center;justify-content:center;
    font-weight:700;font-size:12px;letter-spacing:.4px;color:#fff;text-transform:uppercase;
    border-radius:5px 0 0 5px}
  .rs-label.bear{background:#e2571f}
  .rs-label.bull{background:var(--bull)}
  .rs-label.neut{background:#5a627e}
  .rs-bar{flex:1;display:flex;align-items:stretch;background:var(--row);
    border-radius:0 5px 5px 0;overflow:hidden}
  .rs-long,.rs-short{display:flex;align-items:center;justify-content:flex-end;min-width:0}
  .rs-long{background:var(--bull)}
  .rs-short{background:var(--bear)}
  .rs-long span,.rs-short span{font-size:11.5px;font-weight:600;color:#fff;
    padding:0 9px;white-space:nowrap}
  .rs-empty{color:var(--muted);font-size:13px;padding:16px}
  footer{color:var(--muted);font-size:11px;text-align:center;margin-top:36px;
    border-top:1px solid var(--border);padding-top:16px}
</style>
</head>
<body>
<header>
  <div class="brand">
    <div class="brand-mark">V</div>
    <div>
      <div class="brand-name">VECTOR</div>
      <h1>Retail Sentiment</h1>
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
  <a class="navlink" href="macro.html">Macro Calendar</a>
  <a class="navlink active" href="retail.html">Retail Sentiment</a>
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
  <div class="rs-top">
    <div class="rs-intro">
      <h2><span class="dot"></span>Contrarian Signal</h2>
      <p>Crowd positioning from retail brokers (Myfxbook + Forexbenchmark, averaged), the
        same feed behind the heatmap's Crowd cell. Read it contrarian: a heavily long crowd
        leans bearish, a heavily short crowd leans bullish. Sorted most-long first.</p>
    </div>
    <input id="rs-search" class="rs-search" type="search" placeholder="Filter symbol..." autocomplete="off">
  </div>

  <div class="rs-legend">
    <span class="k"><span class="sw long"></span>Long%</span>
    <span class="k"><span class="sw short"></span>Short%</span>
    <span class="k" style="margin-left:10px">Signal:</span>
    <span class="k"><span class="sw sbear"></span>Bearish (crowd long)</span>
    <span class="k"><span class="sw sneut"></span>Neutral</span>
    <span class="k"><span class="sw sbull"></span>Bullish (crowd short)</span>
  </div>

  <div class="rs-list" id="rs-list">
__ROWS__
  </div>

  <footer>Source: Myfxbook + Forexbenchmark (averaged) &middot; contrarian thresholds 60% / 40% long</footer>
</main>

<script>
  (function(){
    var box = document.getElementById('rs-search');
    var rows = Array.prototype.slice.call(document.querySelectorAll('#rs-list .rs-row'));
    box.addEventListener('input', function(){
      var q = box.value.trim().toUpperCase();
      rows.forEach(function(r){
        r.style.display = (!q || r.getAttribute('data-sym').indexOf(q) !== -1) ? '' : 'none';
      });
    });
  })();
</script>
</body>
</html>
"""
