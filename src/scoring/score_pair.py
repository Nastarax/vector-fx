"""
Top-level scoring: orchestrates all per-currency and per-pair scores into
the heatmap matrix.

Output shape:
  result = {
    "indicators": [...flat list of indicators in render order...],
    "categories": {category_name: [indicator_id, ...]},
    "rows": [
      {
        "symbol": "EURUSD",
        "base": "EUR", "quote": "USD",
        "scores": {indicator_id: int, ...},   # -2..+2 per cell (pair score)
        "total": int,
        "bias": "Bullish",
      },
      ...
    ]
  }
"""
from __future__ import annotations

from pathlib import Path

import yaml

from src.scoring.score_macro import score_indicator
from src.scoring.score_sentiment import cot_score, retail_score
from src.scoring.score_surprise import surprise_score
from src.scoring.score_technical import seasonality_score, trend_score

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def load_indicators_cfg() -> dict:
    with open(CONFIG_DIR / "indicators.yaml") as f:
        return yaml.safe_load(f)


def load_pairs_cfg() -> dict:
    with open(CONFIG_DIR / "pairs.yaml") as f:
        return yaml.safe_load(f)


def bias_label(total: int, thresholds: dict) -> str:
    if total >= thresholds["very_bullish"]:
        return "Very Bullish"
    if total >= thresholds["bullish"]:
        return "Bullish"
    if total <= thresholds["very_bearish"]:
        return "Very Bearish"
    if total <= thresholds["bearish"]:
        return "Bearish"
    return "Neutral"


def build_currency_scores(macro_data: dict, cot_data: dict, ff_history: dict | None = None) -> dict:
    """
    Returns: per_ccy[currency][indicator_id] = int score (-2..+2) OR None
    when data is unavailable for that currency/indicator.

    Scoring preference:
    1. ForexFactory Surprise (Actual vs Forecast) - matches EdgeFinder methodology
    2. FRED momentum (rate of change z-score) - fallback when no FF data
    """
    cfg = load_indicators_cfg()
    ff_history = ff_history or {}
    per_ccy: dict[str, dict[str, int | None]] = {}

    for ccy in macro_data:
        per_ccy[ccy] = {}
        for cat in ("Growth", "Inflation", "Jobs"):
            for ind in cfg["categories"][cat]:
                ind_id = ind["id"]
                direction = ind.get("direction", "up_is_bullish")

                # Try surprise score from FF first
                ff_key = f"{ccy}|{ind_id}"
                ff_releases = ff_history.get(ff_key, [])
                surprise = surprise_score(ff_releases, direction=direction)

                if surprise is not None:
                    per_ccy[ccy][ind_id] = surprise
                else:
                    # Fall back to FRED momentum
                    obs = macro_data[ccy].get(ind_id, [])
                    per_ccy[ccy][ind_id] = score_indicator(
                        obs,
                        direction=direction,
                        lookback_periods=ind.get("lookback_periods", 12),
                    )
        # COT: USD Index trades on ICE which isn't in the CME TFF file we pull,
        # so USD usually has no COT reading. Default to 0 (neutral) instead of
        # None so pair scores still compute based on the other currency's
        # positioning. EdgeFinder appears to do the same (AUDUSD COT = AUD's
        # score because USD's reading is treated as neutral).
        cot_reading = cot_data.get(ccy)
        if cot_reading:
            per_ccy[ccy]["cot"] = cot_score(cot_reading)
        elif ccy == "USD":
            per_ccy[ccy]["cot"] = 0  # explicit neutral, not unknown
        else:
            per_ccy[ccy]["cot"] = None
    return per_ccy


def build_pair_rows(
    per_ccy: dict,
    prices: dict,
    retail_data: dict,
    prices_4h: dict | None = None,
    as_of_date: str | None = None,
) -> list[dict]:
    cfg = load_indicators_cfg()
    pairs_cfg = load_pairs_cfg()
    thresholds = cfg["bias_thresholds"]

    # Flat list of indicator ids in render order
    indicator_ids: list[str] = []
    for cat_name, inds in cfg["categories"].items():
        for ind in inds:
            indicator_ids.append(ind["id"])

    # Indicators that are pair-level (not per-currency diff)
    pair_level = {"trend", "seasonality", "crowd"}

    rows = []
    for p in pairs_cfg["pairs"]:
        sym = p["symbol"]
        base = p["base"]
        quote = p["quote"]
        df = prices.get(sym)

        scores: dict[str, int] = {}

        # Currency-diff indicators.
        # If EITHER side has no data (None), score the cell 0 ("unknown")
        # rather than letting the visible side's score dominate the diff.
        for ind_id in indicator_ids:
            if ind_id in pair_level:
                continue
            base_s = per_ccy.get(base, {}).get(ind_id)
            quote_s = per_ccy.get(quote, {}).get(ind_id)
            if base_s is None or quote_s is None:
                scores[ind_id] = 0
            else:
                diff = base_s - quote_s
                scores[ind_id] = max(-2, min(2, diff))

        # Pair-level indicators
        df_4h = (prices_4h or {}).get(sym)
        scores["trend"] = trend_score(df, df_4h)
        scores["seasonality"] = seasonality_score(df, as_of_date=as_of_date)
        scores["crowd"] = retail_score(retail_data.get(sym))

        total = sum(scores.values())
        rows.append({
            "symbol": sym,
            "base": base,
            "quote": quote,
            "scores": scores,
            "total": total,
            "bias": bias_label(total, thresholds),
        })

    rows.sort(key=lambda r: r["total"], reverse=True)
    return rows


def build_heatmap(macro_data, cot_data, retail_data, prices, prices_4h=None, as_of_date=None, ff_history=None) -> dict:
    cfg = load_indicators_cfg()
    indicator_meta = []
    cat_groups: dict[str, list[str]] = {}
    for cat_name, inds in cfg["categories"].items():
        cat_groups[cat_name] = [i["id"] for i in inds]
        for i in inds:
            indicator_meta.append({"id": i["id"], "label": i["label"], "category": cat_name})

    per_ccy = build_currency_scores(macro_data, cot_data, ff_history=ff_history)
    rows = build_pair_rows(per_ccy, prices, retail_data, prices_4h=prices_4h, as_of_date=as_of_date)
    return {
        "indicators": indicator_meta,
        "categories": cat_groups,
        "rows": rows,
        "per_ccy": per_ccy,
        "as_of_date": as_of_date,
    }
