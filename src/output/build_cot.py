"""
Render the COT Detail dashboard to data/cot.html.

Two interactive Chart.js tools:
  1. Latest COT Report - stacked bar of Long%/Short% per currency
  2. COT Data History - 52-week time series + data table for any selected currency

The latest report is built from the per-currency CotReading objects we already
pull for scoring. The history is fetched separately via fetch_cot_history.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

OUTPUT_DIR = Path(__file__).resolve().parents[2] / "data"
TEMPLATE_DIR = Path(__file__).resolve().parent



def render(cot_data: dict, cot_history: dict | None = None, output_path: Path | None = None) -> Path:
    """
    cot_data: dict[ccy] -> CotReading (latest only, used for Tool 1)
    cot_history: dict[ccy] -> list of weekly dicts (newest first, used for Tool 2).
                 If None, the history tool will render empty.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = output_path or (OUTPUT_DIR / "cot.html")

    # Latest report rows for Tool 1
    latest = []
    for ccy, r in (cot_data or {}).items():
        latest.append({
            "ccy": ccy,
            "long_pct": float(r.long_pct),
            "short_pct": float(r.short_pct),
            "report_date": r.report_date,
        })

    # Pick the most recent report date across currencies as the "latest" label
    report_date = "n/a"
    if latest:
        report_date = max((x["report_date"] for x in latest if x.get("report_date")), default="n/a")

    history = cot_history or {}
    currencies = sorted([c for c in history.keys() if history[c]])

    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
    )
    tmpl = env.get_template("cot_template.html")
    html = tmpl.render(
        report_date=report_date,
        updated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        latest_json=json.dumps(latest),
        history_json=json.dumps(history),
        currencies_json=json.dumps(currencies),
    )
    output_path.write_text(html, encoding="utf-8")
    return output_path
