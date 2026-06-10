"""
Render the Top Setups heatmap to HTML using Jinja2.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.output.timefmt import updated_at_str

OUTPUT_DIR = Path(__file__).resolve().parents[2] / "data"
TEMPLATE_DIR = Path(__file__).resolve().parent


def _cell_class(v: int) -> str:
    if v == 2:
        return "c2"
    if v == 1:
        return "c1"
    if v == -1:
        return "cn1"
    if v == -2:
        return "cn2"
    return "c0"


def _bias_class(bias: str) -> str:
    return {
        "Very Bullish": "b-vbull",
        "Bullish": "b-bull",
        "Neutral": "b-neut",
        "Bearish": "b-bear",
        "Very Bearish": "b-vbear",
    }.get(bias, "b-neut")


def _total_class(total: int) -> str:
    if total >= 9:
        return "total-c2"
    if total >= 5:
        return "total-c1"
    if total <= -9:
        return "total-cn2"
    if total <= -5:
        return "total-cn1"
    return ""


def render(heatmap: dict, output_path: Path | None = None) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    as_of = heatmap.get("as_of_date")
    if output_path is None:
        if as_of:
            output_path = OUTPUT_DIR / f"backtest_{as_of}.html"
        else:
            output_path = OUTPUT_DIR / "output.html"

    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
    )
    env.globals["cell_class"] = _cell_class

    rows_for_render = []
    for r in heatmap["rows"]:
        rows_for_render.append({
            **r,
            "bias_class": _bias_class(r["bias"]),
            "total_class": _total_class(r["total"]),
        })

    cot_status = heatmap.get("cot_status", {})
    stale_cots = sorted([
        (ccy, info) for ccy, info in cot_status.items()
        if info.get("status") == "stale"
    ])

    # All stale items across COT + Investing-sourced indicators, grouped by
    # indicator for cleaner banner display.
    stale_items = heatmap.get("stale_items", [])
    stale_by_indicator: dict[str, list[dict]] = {}
    for item in stale_items:
        stale_by_indicator.setdefault(item["indicator"], []).append(item)
    # Stable display order
    indicator_order = ["COT", "CPI YoY", "PPI YoY", "mPMI", "sPMI"]
    stale_groups = [
        (ind, sorted(stale_by_indicator[ind], key=lambda x: x["ccy"]))
        for ind in indicator_order
        if ind in stale_by_indicator
    ]

    tmpl = env.get_template("template.html")
    html = tmpl.render(
        updated_at=updated_at_str(),
        as_of_date=as_of,
        indicators=heatmap["indicators"],
        categories=heatmap["categories"],
        rows=rows_for_render,
        row_count=len(rows_for_render),
        cot_status=cot_status,
        stale_cots=stale_cots,
        stale_groups=stale_groups,
    )
    output_path.write_text(html, encoding="utf-8")
    return output_path
