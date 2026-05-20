"""
Inflation Data page renderer.

Four components (per A1 EdgeFinder Inflation view), all 8 currencies:
  1. Latest CPI bar chart        - current reported CPI YoY, sorted low->high
  2. CPI current vs previous     - table: Currency | Previous | Current
  3. Historical CPI line chart   - multi-year CPI YoY per currency (from FRED)
  4. PPI bars + current/previous - same as CPI but for Producer Prices

Data sources:
  - Latest CPI / PPI (bars + tables): the econ_data rows built by
    build_economic_heatmap (Investing.com CPI cache + TE/Investing PPI). These
    are the authoritative "latest reported" figures shown elsewhere in Vector.
  - Historical CPI line: computed YoY from FRED CPI *index* series that
    fetch_all_macro already pulls every run (macro_data[ccy]["cpi"]).

NOTE on the line chart: FRED's OECD-sourced international CPI series lag the
official release by several months (e.g. GBP/CAD ~12 months behind), and the
JPY series is stale. The historical chart reflects whatever FRED has; the
"latest reported" bars/tables use the live Investing.com values, so the most
recent line-chart point may not match the bar value. That's expected.

Output: data/inflation.html, single page with currency dropdown for the bars.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

OUTPUT_DIR = Path(__file__).resolve().parents[2] / "data"

CURRENCIES = ("USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD")

# Currencies whose FRED CPI series is quarterly (YoY = 4 periods back).
_QUARTERLY = {"AUD", "NZD"}


def _yoy_from_index(obs: list, periods: int) -> list[dict]:
    """Given FRED index observations (FredObservation objects, any order),
    return [{date, value}] of YoY % change, ascending by date."""
    pts = []
    for o in obs:
        try:
            pts.append((o.date, float(o.value)))
        except (AttributeError, ValueError, TypeError):
            continue
    pts.sort(key=lambda x: x[0])
    out = []
    for i in range(periods, len(pts)):
        d, v = pts[i]
        _, v_prior = pts[i - periods]
        if v_prior:
            out.append({"date": d, "value": round((v / v_prior - 1) * 100, 2)})
    return out


def _latest_from_econ(econ_data: dict, indicator_label: str) -> dict:
    """Pull {ccy: {actual, previous, date}} for one indicator from econ rows."""
    out = {}
    for ccy in CURRENCIES:
        rows = econ_data.get(ccy, []) or []
        for r in rows:
            if r.get("indicator") == indicator_label:
                out[ccy] = {
                    "actual": r.get("actual"),
                    "previous": r.get("previous"),
                    "date": r.get("date") or "",
                }
                break
    return out


def build_all(econ_data: dict, macro_data: dict) -> dict:
    """Assemble the inflation page payload."""
    cpi_latest = _latest_from_econ(econ_data, "CPI YoY")
    ppi_latest = _latest_from_econ(econ_data, "PPI YoY")

    # Historical CPI YoY from FRED index series (already in macro_data)
    cpi_history = {}
    for ccy in CURRENCIES:
        obs = (macro_data.get(ccy, {}) or {}).get("cpi", []) or []
        periods = 4 if ccy in _QUARTERLY else 12
        series = _yoy_from_index(obs, periods)
        if series:
            cpi_history[ccy] = series

    # Drop hopelessly stale series so the line chart doesn't draw stub lines
    # that end years before the others (e.g. the deprecated JPY FRED series
    # stops in 2021). Cutoff: latest point more than ~18 months behind the
    # freshest currency gets excluded.
    if cpi_history:
        newest_per_ccy = {c: s[-1]["date"] for c, s in cpi_history.items() if s}
        global_newest = max(newest_per_ccy.values())
        gy, gm = int(global_newest[:4]), int(global_newest[5:7])
        global_months = gy * 12 + gm
        for ccy, last_date in list(newest_per_ccy.items()):
            ly, lm = int(last_date[:4]), int(last_date[5:7])
            if global_months - (ly * 12 + lm) > 18:
                del cpi_history[ccy]

    return {
        "currencies": list(CURRENCIES),
        "cpi_latest": cpi_latest,
        "ppi_latest": ppi_latest,
        "cpi_history": cpi_history,
    }


def _load_template() -> str:
    tpl_path = Path(__file__).parent / "inflation_template.html"
    return tpl_path.read_text(encoding="utf-8")


def render(payload: dict) -> str:
    """Write data/inflation.html."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    updated_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    template = _load_template()
    html = template.replace(
        "__INFLATION_JSON__", json.dumps(payload, default=str)
    ).replace("__UPDATED_STR__", updated_str)
    out_path = OUTPUT_DIR / "inflation.html"
    out_path.write_text(html, encoding="utf-8")
    return str(out_path)
