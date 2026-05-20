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

# Persistent CPI YoY history archive. Accumulates points from every run and
# never drops old ones, so the line chart survives a source going dark (TE
# changing pages, FRED deprecating a series, etc.).
ARCHIVE_FILE = OUTPUT_DIR / "cache" / "cpi_history_archive.json"

CURRENCIES = ("USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD")

# Currencies whose FRED CPI series is quarterly (YoY = 4 periods back).
_QUARTERLY = {"AUD", "NZD"}

# How many years of CPI history to show on the line chart. Long enough to
# capture the 2020-2022 inflation cycle plus prior baseline, short enough that
# the recent moves aren't visually compressed.
_DISPLAY_YEARS = 12

_MONTH_ABBR = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _load_archive() -> dict:
    if not ARCHIVE_FILE.exists():
        return {}
    try:
        with open(ARCHIVE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_archive(arch: dict) -> None:
    try:
        ARCHIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(ARCHIVE_FILE, "w") as f:
            json.dump(arch, f, indent=2, sort_keys=True)
    except Exception:
        pass


def _merge_points(arch: dict, ccy: str, points: list[dict]) -> None:
    """Merge [{date, value}] into arch[ccy] keyed by date (YYYY-MM-01).
    New points overwrite same-date values (latest fetch wins); old dates kept."""
    bucket = arch.setdefault(ccy, {})
    for p in points:
        d, v = p.get("date"), p.get("value")
        if d and v is not None:
            bucket[d] = v


def _tokyo_recent_to_points(tokyo_core: dict) -> list[dict]:
    """Convert TE Tokyo Core CPI 'recent' rows to [{date(YYYY-MM-01), value}].
    The reported month (e.g. 'Apr') maps to the reference period; the year comes
    from the release date."""
    out = []
    for row in (tokyo_core or {}).get("recent", []) or []:
        actual = row.get("actual")
        rel_date = row.get("date") or ""
        ref = (row.get("ref_month") or "").strip().lower()[:3]
        mo = _MONTH_ABBR.get(ref)
        if actual is None or mo is None or len(rel_date) < 4:
            continue
        year = int(rel_date[:4])
        out.append({"date": f"{year:04d}-{mo:02d}-01", "value": actual})
    return out


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


def build_all(econ_data: dict, cpi_index_by_ccy: dict, tokyo_core: dict | None = None) -> dict:
    """Assemble the inflation page payload.

    cpi_index_by_ccy: {ccy: list[FredObservation]} of CPI *index* levels (the
    long history from fred.fetch_cpi_history). YoY is computed here.
    tokyo_core: TE Tokyo Core CPI dict (JPY source) with a 'recent' table.

    Sources are merged into a persistent archive so history is never lost when
    a source goes dark, then the line chart is rebuilt from the archive.
    """
    cpi_latest = _latest_from_econ(econ_data, "CPI YoY")
    ppi_latest = _latest_from_econ(econ_data, "PPI YoY")

    # ---- 1. Gather this run's points from every source ----
    fresh: dict[str, list[dict]] = {}
    # FRED index -> YoY for the 7 non-JPY currencies
    for ccy in CURRENCIES:
        if ccy == "JPY":
            continue
        obs = (cpi_index_by_ccy or {}).get(ccy, []) or []
        periods = 4 if ccy in _QUARTERLY else 12
        series = _yoy_from_index(obs, periods)
        if series:
            fresh[ccy] = series
    # JPY from Investing Tokyo Core CPI: deep history list ({date,value}) when
    # present, else fall back to the older 'recent' row shape.
    jpy_pts = list((tokyo_core or {}).get("history") or [])
    if not jpy_pts:
        jpy_pts = _tokyo_recent_to_points(tokyo_core or {})
    if jpy_pts:
        fresh["JPY"] = jpy_pts
    # Hybrid splice: append the current reported CPI YoY (from cpi_latest) so
    # each line reaches "now", closing the FRED publication-lag gap.
    for ccy in CURRENCIES:
        latest = cpi_latest.get(ccy)
        if latest and latest.get("actual") is not None and latest.get("date"):
            ym = str(latest["date"])[:7]  # YYYY-MM
            if len(ym) == 7:
                fresh.setdefault(ccy, []).append({"date": f"{ym}-01", "value": latest["actual"]})

    # ---- 2. Merge into the persistent archive, then save ----
    archive = _load_archive()
    for ccy, pts in fresh.items():
        _merge_points(archive, ccy, pts)
    _save_archive(archive)

    # ---- 3. Rebuild history series from the archive (union over all runs) ----
    cpi_history: dict[str, list[dict]] = {}
    for ccy in CURRENCIES:
        bucket = archive.get(ccy, {})
        if not bucket:
            continue
        series = [{"date": d, "value": v} for d, v in sorted(bucket.items())]
        cpi_history[ccy] = series

    # ---- 4. Trim to a common window so the left edge lines up ----
    # Prefer aligning everything to JPY's earliest point (per design: show all
    # currencies back to where JPY history begins). Fall back to a fixed
    # N-year window if JPY is unavailable.
    if cpi_history:
        jpy = cpi_history.get("JPY")
        if jpy:
            cutoff = jpy[0]["date"]
        else:
            all_newest = max(s[-1]["date"] for s in cpi_history.values() if s)
            cutoff_year = int(all_newest[:4]) - _DISPLAY_YEARS
            cutoff = f"{cutoff_year:04d}{all_newest[4:]}"
        for ccy in list(cpi_history.keys()):
            cpi_history[ccy] = [p for p in cpi_history[ccy] if p["date"] >= cutoff]
            if not cpi_history[ccy]:
                del cpi_history[ccy]

    # ---- 5. Drop hopelessly stale series (latest point >18mo behind freshest) ----
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
