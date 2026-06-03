"""
Asset Scorecard renderer.

Per-currency deep-dive view (one per ccy) showing:
  - Big bias gauge + overall score
  - Sub-scores broken into Technical / Sentiment+COT / Fundamentals
  - Fundamentals split into Growth / Inflation / Jobs sub-bias
  - Indicator detail tables reusing the economic heatmap row data

Technical and Crowd Sentiment are pair-level indicators in the main heatmap.
For the per-currency scorecard view, we aggregate them across the 7 pairs that
contain each currency: when the currency is the QUOTE side of a pair, the
pair-level score is flipped (a bullish EURUSD trend = bearish USD).

Output: data/scorecard.html. Currency selector via dropdown + URL hash anchor.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

OUTPUT_DIR = Path(__file__).resolve().parents[2] / "data"

CURRENCIES = ("USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD", "XAU", "NKY", "XPT")
DISPLAY_NAMES = {"XAU": "Gold", "NKY": "Nikkei 225", "XPT": "Platinum"}

# Sub-section bias thresholds. Range varies by section, so we threshold on
# a fraction of the theoretical max-abs score for that section.
#   |frac| >= 0.5  -> Very Bullish / Very Bearish
#   |frac| >  0    -> Bullish / Bearish
#   frac  == 0     -> Neutral
def _sub_bias(score: int | float | None, max_abs: int) -> str:
    if score is None or max_abs <= 0:
        return "n/a"
    frac = score / max_abs
    if frac >= 0.5:
        return "Very Bullish"
    if frac > 0:
        return "Bullish"
    if frac <= -0.5:
        return "Very Bearish"
    if frac < 0:
        return "Bearish"
    return "Neutral"


def _agg_pair_scores(pair_rows: list[dict], ccy: str, ind_id: str) -> int | None:
    """Average a pair-level indicator across all pairs containing the currency,
    flipping sign when the currency is the quote. Round to nearest int and
    clamp to -2..+2 so the result is comparable to per-currency scores."""
    vals = []
    for row in pair_rows:
        if row.get("is_currency"):
            continue
        base, quote = row.get("base"), row.get("quote")
        s = row.get("scores", {}).get(ind_id)
        if s is None:
            continue
        if base == ccy:
            vals.append(s)
        elif quote == ccy:
            vals.append(-s)
    if not vals:
        return None
    avg = sum(vals) / len(vals)
    rounded = int(round(avg))
    return max(-2, min(2, rounded))


def _impact_to_score(label: str) -> int:
    """Map the econ heatmap impact label back to an int delta for display."""
    return {"Bullish": 1, "Bearish": -1, "Neutral": 0}.get(label, 0)


def _build_currency(
    ccy: str,
    per_ccy: dict,
    pair_rows: list[dict],
    cot_data: dict | None,
    econ_rows: list[dict],
) -> dict:
    """Assemble one scorecard payload for a single currency."""
    ccy_scores = per_ccy.get(ccy, {})

    # Technical aggregation (trend + seasonality, both pair-level)
    trend_avg = _agg_pair_scores(pair_rows, ccy, "trend")
    seas_avg = _agg_pair_scores(pair_rows, ccy, "seasonality")
    technical_score = (trend_avg or 0) + (seas_avg or 0)

    # Sentiment+COT aggregation
    cot_s = ccy_scores.get("cot")
    crowd_avg = _agg_pair_scores(pair_rows, ccy, "crowd")
    sentiment_cot_score = (cot_s or 0) + (crowd_avg or 0)

    # COT details for the right-side table
    cot_reading = (cot_data or {}).get(ccy)
    long_pct = short_pct = change_pct = None
    cot_stale = False
    if cot_reading:
        # Reading shape per src/fetchers/cot.py: has long_pct, short_pct,
        # change_pct (Long% week-over-week), is_stale.
        long_pct = getattr(cot_reading, "long_pct", None)
        short_pct = getattr(cot_reading, "short_pct", None)
        change_pct = getattr(cot_reading, "change_pct", None)
        cot_stale = getattr(cot_reading, "is_stale", False)

    # Fundamentals split: bucket econ rows by section
    growth_ids = {"gdp", "mpmi", "spmi", "retail_sales", "consumer_conf"}
    inflation_ids = {"cpi", "ppi", "pce", "rates"}
    jobs_ids = {"nfp", "adp", "unemployment_rate", "jobless_claims", "jolts"}

    growth_rows, inflation_rows, jobs_rows = [], [], []
    for row in econ_rows:
        ind_id = row.get("ind_id")
        if ind_id in growth_ids:
            growth_rows.append(row)
        elif ind_id in inflation_ids:
            inflation_rows.append(row)
        elif ind_id in jobs_ids:
            jobs_rows.append(row)

    growth_score = sum(_impact_to_score(r["currency_impact"]) for r in growth_rows)
    inflation_score = sum(_impact_to_score(r["currency_impact"]) for r in inflation_rows)
    jobs_score = sum(_impact_to_score(r["currency_impact"]) for r in jobs_rows)
    fundamentals_score = growth_score + inflation_score + jobs_score

    total_score = technical_score + sentiment_cot_score + fundamentals_score

    # Sub-bias labels using section-specific max-abs (each indicator is ±1 in
    # impact label terms, so max-abs == row count)
    return {
        "ccy": ccy,
        "total_score": total_score,
        "bias": _sub_bias(total_score, len(growth_rows) + len(inflation_rows) + len(jobs_rows) + 4),
        "sub_scores": {
            "technical": technical_score,
            "sentiment_cot": sentiment_cot_score,
            "fundamentals": fundamentals_score,
            "growth": growth_score,
            "inflation": inflation_score,
            "jobs": jobs_score,
        },
        "sub_bias": {
            "technical": _sub_bias(technical_score, 4),
            "sentiment_cot": _sub_bias(sentiment_cot_score, 4),
            "fundamentals": _sub_bias(fundamentals_score, max(1, len(growth_rows) + len(inflation_rows) + len(jobs_rows))),
            "growth": _sub_bias(growth_score, max(1, len(growth_rows))),
            "inflation": _sub_bias(inflation_score, max(1, len(inflation_rows))),
            "jobs": _sub_bias(jobs_score, max(1, len(jobs_rows))),
        },
        "technical": {
            "trend": trend_avg,
            "seasonality": seas_avg,
            "trend_label": _sub_bias(trend_avg, 2) if trend_avg is not None else "n/a",
            "seasonality_label": _sub_bias(seas_avg, 2) if seas_avg is not None else "n/a",
        },
        "sentiment_cot": {
            "cot": cot_s,
            "crowd": crowd_avg,
            "cot_label": _sub_bias(cot_s, 2) if cot_s is not None else "n/a",
            "crowd_label": _sub_bias(crowd_avg, 2) if crowd_avg is not None else "n/a",
            "long_pct": long_pct,
            "short_pct": short_pct,
            "change_pct": change_pct,
            "cot_stale": cot_stale,
        },
        "fundamentals": {
            "growth_rows": growth_rows,
            "inflation_rows": inflation_rows,
            "jobs_rows": jobs_rows,
        },
    }


def build_all(per_ccy: dict, pair_rows: list[dict], cot_data: dict | None,
              econ_data: dict) -> dict:
    """Build per-currency scorecards.

    econ_data is the dict returned by build_economic_heatmap.build_all
    ({ccy: [rows]}). We mutate it lightly to add ind_id so we can bucket
    rows in this module without re-deriving them.
    """
    # Inject ind_id back onto econ rows (build_economic_heatmap doesn't
    # currently emit it; we get it from the indicator label).
    from src.output.build_economic_heatmap import INDICATORS as ECON_INDICATORS
    label_to_id = {ind["label"]: ind["id"] for ind in ECON_INDICATORS}

    out: dict[str, dict] = {}
    for ccy in CURRENCIES:
        rows = econ_data.get(ccy, []) or []
        for r in rows:
            if "ind_id" not in r:
                r["ind_id"] = label_to_id.get(r.get("indicator"), "")
        out[ccy] = _build_currency(ccy, per_ccy, pair_rows, cot_data, rows)
    return out


# Template lives in scorecard_template.html; loaded at render time.
def _load_template() -> str:
    tpl_path = Path(__file__).parent / "scorecard_template.html"
    return tpl_path.read_text(encoding="utf-8")


def render(scorecards: dict) -> str:
    """Write data/scorecard.html with all currency scorecards embedded."""
    from src.scoring.score_history import load_history

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    updated_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    labels = {c: DISPLAY_NAMES.get(c, c) for c in CURRENCIES}
    history = load_history()
    template = _load_template()
    html = (template
            .replace("__SCORECARDS_JSON__", json.dumps(scorecards, default=str))
            .replace("__UPDATED_STR__", updated_str)
            .replace("__CURRENCIES_JSON__", json.dumps(list(CURRENCIES)))
            .replace("__LABELS_JSON__", json.dumps(labels))
            .replace("__HISTORY_JSON__", json.dumps(history)))
    out_path = OUTPUT_DIR / "scorecard.html"
    out_path.write_text(html, encoding="utf-8")
    return str(out_path)
