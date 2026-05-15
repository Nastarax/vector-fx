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
    rates_outlook: dict | None = None,
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
    rates_outlook = rates_outlook or {}
    per_ccy: dict[str, dict[str, int | None]] = {}

    for ccy in macro_data:
        per_ccy[ccy] = {}
        for cat in ("Growth", "Inflation", "Jobs"):
            for ind in cfg["categories"][cat]:
                ind_id = ind["id"]
                direction = ind.get("direction", "up_is_bullish")
                key = f"{ccy}|{ind_id}"

                # GDP: latest TE release. Actual vs Consensus (priority), fall
                # back to TEForecast if Consensus is missing for that release.
                if ind_id == "gdp":
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

                # NFP: US-only indicator. TE non-farm-payrolls.
                # Score USD: Actual vs Consensus (priority), fall back to
                # TEForecast. Non-USD currencies get 0 (neutral) so USD
                # pairs reflect USD's NFP direction.
                if ind_id == "nfp":
                    if ccy != "USD":
                        per_ccy[ccy][ind_id] = 0
                        continue
                    te_rels = te_history.get(key, [])
                    if not te_rels:
                        per_ccy[ccy][ind_id] = None
                        continue
                    latest = sorted(te_rels, key=lambda x: x.get("date", ""), reverse=True)[0]
                    actual = latest.get("actual")
                    benchmark = latest.get("consensus")
                    if benchmark is None:
                        benchmark = latest.get("forecast")
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

                # JOLTS (Job Openings): US-only monthly TE release.
                # Score USD: Actual vs Consensus (priority), fall back to
                # TEForecast. Non-USD currencies = 0 (neutral) so USD pairs
                # reflect USD's JOLTS direction. Direction is up_is_bullish
                # (more openings = strong economy = stronger USD).
                if ind_id == "jolts":
                    if ccy != "USD":
                        per_ccy[ccy][ind_id] = 0
                        continue
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

                # ADP Employment Change: US-only monthly TE release.
                # Score USD: Actual vs Consensus (priority), fall back to
                # TEForecast. Non-USD currencies = 0 (neutral) so USD pairs
                # reflect USD's ADP direction. Direction is up_is_bullish
                # (more jobs added = stronger USD).
                if ind_id == "adp":
                    if ccy != "USD":
                        per_ccy[ccy][ind_id] = 0
                        continue
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

                # Jobless Claims (Unemployment Claims): US-only weekly TE
                # release. Score USD: Actual vs Consensus (priority), fall
                # back to TEForecast. Non-USD currencies get 0 (neutral) so
                # USD pairs reflect USD's claims direction. Direction is
                # down_is_bullish (lower claims = bullish USD).
                if ind_id == "jobless_claims":
                    if ccy != "USD":
                        per_ccy[ccy][ind_id] = 0
                        continue
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

                # Unemployment Rate: TE unemployment-rate page for all 8
                # currencies. Score: Actual vs Consensus (priority), fall back
                # to TEForecast. Then flipped by direction=down_is_bullish so
                # that a LOWER actual than expected scores +1 (bullish for the
                # currency, signals economic strength) and a HIGHER actual
                # scores -1 (bearish, signals weakness).
                if ind_id == "unemployment_rate":
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

                # PCE YoY: US-only indicator. TE pce-price-index-annual-change.
                # Score USD: Actual vs Consensus (priority), fall back to
                # TEForecast. Non-USD currencies get 0 (neutral) so USD
                # pairs reflect USD's PCE direction in the diff while
                # non-USD-only pairs (e.g., EURGBP) show 0 as expected.
                if ind_id == "pce":
                    if ccy != "USD":
                        per_ccy[ccy][ind_id] = 0
                        continue
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

                # Interest Rates: EdgeFinder methodology — compare next
                # meeting's TEForecast to the current central bank rate.
                # Both values come from TradingEconomics interest-rate
                # pages (free, scraped fresh every run). If TEForecast is
                # not yet published for the next meeting, the fetcher uses
                # current rate as the forecast (so score = 0, "no expected
                # change"). Updated every main.py run.
                if ind_id == "rates":
                    outlook = rates_outlook.get(ccy)
                    if not outlook:
                        per_ccy[ccy][ind_id] = None
                        continue
                    current_rate = outlook.get("current")
                    forecast = outlook.get("forecast")
                    if current_rate is None or forecast is None:
                        per_ccy[ccy][ind_id] = None
                        continue
                    if forecast > current_rate:
                        s = 1
                    elif forecast < current_rate:
                        s = -1
                    else:
                        s = 0
                    if direction == "down_is_bullish":
                        s = -s
                    per_ccy[ccy][ind_id] = s
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
        # COT: all 8 currencies including USD ("USD INDEX - ICE FUTURES U.S."
        # in the CFTC Legacy report). If a reading is missing for any reason
        # (API hiccup, market name changed again), fall through to None and
        # the pair-diff logic in build_pair_rows treats either-side-None as 0.
        # If the reading is older than MAX_STALE_DAYS, treat it as None too —
        # don't score with stale data. Heatmap will mark it visibly.
        cot_reading = cot_data.get(ccy)
        if cot_reading and not getattr(cot_reading, "is_stale", False):
            per_ccy[ccy]["cot"] = cot_score(cot_reading)
        else:
            per_ccy[ccy]["cot"] = None
    return per_ccy


def build_pair_rows(
    per_ccy: dict,
    prices: dict,
    retail_data: dict,
    prices_4h: dict | None = None,
    as_of_date: str | None = None,
    cot_data: dict | None = None,
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

        # Flag the COT cell stale if EITHER currency in the pair has stale COT.
        # Used by the template to render a visible warning marker on the cell.
        cot_stale = False
        if cot_data:
            base_reading = cot_data.get(base)
            quote_reading = cot_data.get(quote)
            if (base_reading and getattr(base_reading, "is_stale", False)) or \
               (quote_reading and getattr(quote_reading, "is_stale", False)):
                cot_stale = True

        rows.append({
            "symbol": sym,
            "base": base,
            "quote": quote,
            "scores": scores,
            "total": total,
            "bias": bias_label(total, thresholds),
            "cot_stale": cot_stale,
        })

    rows.sort(key=lambda r: r["total"], reverse=True)
    return rows


def build_heatmap(macro_data, cot_data, retail_data, prices, prices_4h=None, as_of_date=None, ff_history=None, te_history=None, investing_mpmi=None, investing_spmi=None, abs_au_mhsi=None, investing_cpi=None, investing_ppi=None, rates_outlook=None) -> dict:
    cfg = load_indicators_cfg()
    indicator_meta = []
    cat_groups: dict[str, list[str]] = {}
    for cat_name, inds in cfg["categories"].items():
        cat_groups[cat_name] = [i["id"] for i in inds]
        for i in inds:
            indicator_meta.append({"id": i["id"], "label": i["label"], "category": cat_name})

    per_ccy = build_currency_scores(macro_data, cot_data, ff_history=ff_history, te_history=te_history, investing_mpmi=investing_mpmi, investing_spmi=investing_spmi, abs_au_mhsi=abs_au_mhsi, investing_cpi=investing_cpi, investing_ppi=investing_ppi, rates_outlook=rates_outlook)
    rows = build_pair_rows(per_ccy, prices, retail_data, prices_4h=prices_4h, as_of_date=as_of_date, cot_data=cot_data)

    # COT freshness map: ccy -> {"status": "fresh"|"stale"|"missing", "date": ..., "days_old": ...}
    # Used by the template to show staleness on the heatmap.
    cot_status = {}
    for ccy in ("USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD"):
        reading = (cot_data or {}).get(ccy)
        if reading is None:
            cot_status[ccy] = {"status": "missing", "date": None, "days_old": None}
        elif getattr(reading, "is_stale", False):
            cot_status[ccy] = {"status": "stale", "date": reading.report_date, "days_old": reading.days_old}
        else:
            cot_status[ccy] = {"status": "fresh", "date": reading.report_date, "days_old": reading.days_old}

    return {
        "indicators": indicator_meta,
        "categories": cat_groups,
        "rows": rows,
        "per_ccy": per_ccy,
        "as_of_date": as_of_date,
        "cot_status": cot_status,
    }
