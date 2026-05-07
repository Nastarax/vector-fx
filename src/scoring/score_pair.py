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


def build_currency_scores(macro_data: dict, cot_data: dict) -> dict:
    """
    Returns: per_ccy[currency][indicator_id] = -2..+2
    For 'cot' the value is the per-currency COT score.
    Trend, seasonality, retail are pair-level (handled separately).
    """
    cfg = load_indicators_cfg()
    per_ccy: dict[str, dict[str, int]] = {}

    for ccy in macro_data:
        per_ccy[ccy] = {}
        # Macro indicators
        for cat in ("Growth", "Inflation", "Jobs"):
            for ind in cfg["categories"][cat]:
                ind_id = ind["id"]
                obs = macro_data[ccy].get(ind_id, [])
                per_ccy[ccy][ind_id] = score_indicator(
                    obs,
                    direction=ind.get("direction", "up_is_bullish"),
                    lookback_periods=ind.get("lookback_periods", 12),
                )
        # COT (per currency)
        per_ccy[ccy]["cot"] = cot_score(cot_data.get(ccy))
    return per_ccy


def build_pair_rows(
    per_ccy: dict,
    prices: dict,
    retail_data: dict,
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

        # Currency-diff indicators
        for ind_id in indicator_ids:
            if ind_id in pair_level:
                continue
            base_s = per_ccy.get(base, {}).get(ind_id, 0)
            quote_s = per_ccy.get(quote, {}).get(ind_id, 0)
            diff = base_s - quote_s
            # Clamp to -2..+2 so a single cell still fits the EdgeFinder visual
            scores[ind_id] = max(-2, min(2, diff))

        # Pair-level indicators
        scores["trend"] = trend_score(df)
        scores["seasonality"] = seasonality_score(df)
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


def build_heatmap(macro_data, cot_data, retail_data, prices) -> dict:
    cfg = load_indicators_cfg()
    indicator_meta = []
    cat_groups: dict[str, list[str]] = {}
    for cat_name, inds in cfg["categories"].items():
        cat_groups[cat_name] = [i["id"] for i in inds]
        for i in inds:
            indicator_meta.append({"id": i["id"], "label": i["label"], "category": cat_name})

    per_ccy = build_currency_scores(macro_data, cot_data)
    rows = build_pair_rows(per_ccy, prices, retail_data)
    return {
        "indicators": indicator_meta,
        "categories": cat_groups,
        "rows": rows,
        "per_ccy": per_ccy,
    }
