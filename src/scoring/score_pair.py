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
from src.scoring.score_surprise import momentum_score, surprise_score
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


def build_currency_scores(
    macro_data: dict,
    cot_data: dict,
    ff_history: dict | None = None,
    te_history: dict | None = None,
    investing_mpmi: dict | None = None,
    investing_spmi: dict | None = None,
    abs_au_mhsi: dict | None = None,
    investing_cpi: dict | None = None,
    investing_ppi: dict | None = None,
) -> dict:
    """
    Returns: per_ccy[currency][indicator_id] = int score (-2..+2) OR None
    when data is unavailable for that currency/indicator.

    Scoring preference:
    1. Combined TE + FF Surprise (Actual vs Forecast). TE takes priority on
       overlapping dates because its TEForecast matches EdgeFinder methodology.
       Combined history from both sources gives richer z-score baseline.
    2. FRED momentum (rate of change z-score) - fallback when no surprise data

    investing_mpmi: per-currency latest Manufacturing PMI release from
    Investing.com {"USD": {"actual": ..., "previous": ..., "date": ...}, ...}.
    Source of truth for mPMI when present; falls back to combined TE+FF data.
    """
    cfg = load_indicators_cfg()
    ff_history = ff_history or {}
    te_history = te_history or {}
    investing_mpmi = investing_mpmi or {}
    investing_spmi = investing_spmi or {}
    investing_cpi = investing_cpi or {}
    investing_ppi = investing_ppi or {}
    per_ccy: dict[str, dict[str, int | None]] = {}

    for ccy in macro_data:
        per_ccy[ccy] = {}
        for cat in ("Growth", "Inflation", "Jobs"):
            for ind in cfg["categories"][cat]:
                ind_id = ind["id"]
                direction = ind.get("direction", "up_is_bullish")
                key = f"{ccy}|{ind_id}"

                # GDP: use Consensus (analyst median) from TE pages, not TEForecast.
                # Per user preference. Swap the 'consensus' value into the 'forecast'
                # field so surprise_score uses it.
                if ind_id == "gdp":
                    te_rels = te_history.get(key, [])
                    te_rels_sorted = sorted(te_rels, key=lambda x: x.get("date", ""), reverse=True)
                    consensus_rels = []
                    for r in te_rels_sorted:
                        consensus_value = r.get("consensus")
                        if consensus_value is None:
                            continue
                        modified = dict(r)
                        modified["forecast"] = consensus_value
                        consensus_rels.append(modified)
                    per_ccy[ccy][ind_id] = surprise_score(consensus_rels, direction=direction)
                    continue

                # Retail sales:
                # - AUD: ABS Monthly Household Spending Indicator. Australia
                #   deprecated retail sales; MHSI is the official replacement.
                #   Scoring: current MoM% > previous MoM% -> +1 (acceleration).
                # - Other 7 currencies: TE retail sales. Actual vs Consensus,
                #   fall back to TEForecast if Consensus is missing.
                # Only the latest release is scored.
                if ind_id == "retail_sales" and ccy == "AUD":
                    mhsi = abs_au_mhsi or {}
                    cur = mhsi.get("current_mom")
                    prev = mhsi.get("previous_mom")
                    if cur is None or prev is None:
                        per_ccy[ccy][ind_id] = None
                        continue
                    if cur > prev:
                        s = 1
                    elif cur < prev:
                        s = -1
                    else:
                        s = 0
                    if direction == "down_is_bullish":
                        s = -s
                    per_ccy[ccy][ind_id] = s
                    continue
                if ind_id == "retail_sales":
                    te_rels = te_history.get(key, [])
                    if not te_rels:
                        per_ccy[ccy][ind_id] = None
                        continue
                    latest = sorted(te_rels, key=lambda x: x.get("date", ""), reverse=True)[0]
                    actual = latest.get("actual")
                    benchmark = latest.get("consensus")
                    if benchmark is None:
                        benchmark = latest.get("forecast")  # TEForecast fallback
                    if actual is None or benchmark is None:
                        per_ccy[ccy][ind_id] = None
                        continue
                    if actual > benchmark:
                        s = 1
                    elif actual < benchmark:
                        s = -1
                    else:
                        s = 0
                    if direction == "down_is_bullish":
                        s = -s
                    per_ccy[ccy][ind_id] = s
                    continue

                # Consumer Confidence: Actual vs PREVIOUS on the latest
                # release (momentum scoring). The TEForecast comparison
                # produced counter-intuitive results when TE set a low bar
                # (e.g., JPY: TEForecast 31.0 vs Previous 33.3, Actual 32.2
                # technically "beat" the forecast but confidence still
                # declined month-over-month). Momentum reflects the actual
                # trend direction, which is what matters for swing trading.
                # Same methodology EdgeFinder uses for PMI.
                if ind_id == "consumer_conf":
                    te_rels = te_history.get(key, [])
                    if not te_rels:
                        per_ccy[ccy][ind_id] = None
                        continue
                    latest = sorted(te_rels, key=lambda x: x.get("date", ""), reverse=True)[0]
                    actual = latest.get("actual")
                    previous = latest.get("previous")
                    if actual is None or previous is None:
                        per_ccy[ccy][ind_id] = None
                        continue
                    if actual > previous:
                        s = 1
                    elif actual < previous:
                        s = -1
                    else:
                        s = 0
                    if direction == "down_is_bullish":
                        s = -s
                    per_ccy[ccy][ind_id] = s
                    continue

                # PPI YoY:
                # - NZD: Investing.com PPI Output (latest release). Actual
                #   vs Forecast; fall back to Previous if Forecast missing.
                # - Other 7 (USD/EUR/GBP/JPY/CHF/AUD/CAD): TE producer-prices
                #   page. Actual vs Consensus, fall back to TEForecast if
                #   Consensus missing. GBP uses ppi-input-yoy slug (handled
                #   via TE_INDICATOR_SLUG_OVERRIDES).
                if ind_id == "ppi" and ccy == "NZD" and investing_ppi.get("NZD"):
                    rel = investing_ppi["NZD"]
                    actual = rel.get("actual")
                    benchmark = rel.get("forecast")
                    if benchmark is None:
                        benchmark = rel.get("previous")
                    if actual is None or benchmark is None:
                        per_ccy[ccy][ind_id] = None
                        continue
                    if actual > benchmark:
                        s = 1
                    elif actual < benchmark:
                        s = -1
                    else:
                        s = 0
                    if direction == "down_is_bullish":
                        s = -s
                    per_ccy[ccy][ind_id] = s
                    continue
                if ind_id == "ppi":
                    te_rels = te_history.get(key, [])
                    if not te_rels:
                        per_ccy[ccy][ind_id] = None
                        continue
                    latest = sorted(te_rels, key=lambda x: x.get("date", ""), reverse=True)[0]
                    actual = latest.get("actual")
                    benchmark = latest.get("consensus")
                    if benchmark is None:
                        benchmark = latest.get("forecast")  # TEForecast fallback
                    if actual is None or benchmark is None:
                        per_ccy[ccy][ind_id] = None
                        continue
                    if actual > benchmark:
                        s = 1
                    elif actual < benchmark:
                        s = -1
                    else:
                        s = 0
                    if direction == "down_is_bullish":
                        s = -s
                    per_ccy[ccy][ind_id] = s
                    continue

                # CPI YoY: Investing.com per-currency Latest Release. Actual
                # vs Forecast where the forecast is published. Falls back to
                # Actual vs Previous for JPY (Investing's Japan CPI YoY page
                # never lists a forecast) and for CHF when the next Swiss
                # release's forecast hasn't been published yet.
                if ind_id == "cpi" and investing_cpi.get(ccy):
                    rel = investing_cpi[ccy]
                    actual = rel.get("actual")
                    forecast = rel.get("forecast")
                    previous = rel.get("previous")
                    benchmark = forecast if forecast is not None else previous
                    if actual is None or benchmark is None:
                        per_ccy[ccy][ind_id] = None
                        continue
                    if actual > benchmark:
                        s = 1
                    elif actual < benchmark:
                        s = -1
                    else:
                        s = 0
                    if direction == "down_is_bullish":
                        s = -s
                    per_ccy[ccy][ind_id] = s
                    continue

                # PMI (mpmi, spmi): EF uses CHANGE from previous to latest, not surprise.
                # For mPMI we prefer Investing.com's per-currency Latest Release page.
                # For sPMI we prefer the investing_spmi dict (6 Investing pages + 2 TE pages).
                # Both fall back to combined TE + FF history if their fresh source is missing.
                if ind_id == "mpmi" and investing_mpmi.get(ccy):
                    rel = investing_mpmi[ccy]
                    per_ccy[ccy][ind_id] = momentum_score([rel], direction=direction)
                    continue
                if ind_id == "spmi" and investing_spmi.get(ccy):
                    rel = investing_spmi[ccy]
                    per_ccy[ccy][ind_id] = momentum_score([rel], direction=direction)
                    continue
                if ind_id in ("mpmi", "spmi"):
                    te_rels = te_history.get(key, [])
                    ff_rels = ff_history.get(key, [])
                    seen_dates = set()
                    combined = []
                    for r in te_rels:
                        d = r.get("date")
                        if d and d not in seen_dates:
                            combined.append(r)
                            seen_dates.add(d)
                    for r in ff_rels:
                        d = r.get("date")
                        if d and d not in seen_dates:
                            combined.append(r)
                            seen_dates.add(d)
                    combined.sort(key=lambda x: x.get("date", ""), reverse=True)
                    per_ccy[ccy][ind_id] = momentum_score(combined, direction=direction)
                    continue

                # Other indicators: combine TE + FF, prefer TE on duplicate dates
                te_rels = te_history.get(key, [])
                ff_rels = ff_history.get(key, [])
                seen_dates = set()
                combined = []
                for r in te_rels:
                    d = r.get("date")
                    if d and d not in seen_dates:
                        combined.append(r)
                        seen_dates.add(d)
                for r in ff_rels:
                    d = r.get("date")
                    if d and d not in seen_dates:
                        combined.append(r)
                        seen_dates.add(d)
                combined.sort(key=lambda x: x.get("date", ""), reverse=True)

                per_ccy[ccy][ind_id] = surprise_score(combined, direction=direction)
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


def build_heatmap(macro_data, cot_data, retail_data, prices, prices_4h=None, as_of_date=None, ff_history=None, te_history=None, investing_mpmi=None, investing_spmi=None, abs_au_mhsi=None, investing_cpi=None, investing_ppi=None) -> dict:
    cfg = load_indicators_cfg()
    indicator_meta = []
    cat_groups: dict[str, list[str]] = {}
    for cat_name, inds in cfg["categories"].items():
        cat_groups[cat_name] = [i["id"] for i in inds]
        for i in inds:
            indicator_meta.append({"id": i["id"], "label": i["label"], "category": cat_name})

    per_ccy = build_currency_scores(macro_data, cot_data, ff_history=ff_history, te_history=te_history, investing_mpmi=investing_mpmi, investing_spmi=investing_spmi, abs_au_mhsi=abs_au_mhsi, investing_cpi=investing_cpi, investing_ppi=investing_ppi)
    rows = build_pair_rows(per_ccy, prices, retail_data, prices_4h=prices_4h, as_of_date=as_of_date)
    return {
        "indicators": indicator_meta,
        "categories": cat_groups,
        "rows": rows,
        "per_ccy": per_ccy,
        "as_of_date": as_of_date,
    }
