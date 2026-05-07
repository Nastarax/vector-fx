"""
Render the COT Detail page to data/cot.html.
"""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

OUTPUT_DIR = Path(__file__).resolve().parents[2] / "data"
TEMPLATE_DIR = Path(__file__).resolve().parent


def render(cot_data: dict, output_path: Path | None = None) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = output_path or (OUTPUT_DIR / "cot.html")

    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
    )

    rows = list(cot_data.values())
    rows.sort(key=lambda r: r.weekly_change_pct, reverse=True)
    report_date = rows[0].report_date if rows else "n/a"

    tmpl = env.get_template("cot_template.html")
    html = tmpl.render(rows=rows, report_date=report_date)
    output_path.write_text(html, encoding="utf-8")
    return output_path
