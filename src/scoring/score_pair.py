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

import os
from pathlib import Path

import yaml

from src.scoring.score_macro import score_indicator
from src.fetchers.cot import COMMODITY_CCYS
from src.scoring.score_sentiment import cot_score, cot_score_commodity, crowd_score_commodity, retail_score
from src.scoring.score_surprise import momentum_score, surprise_score
from src.scoring.score_technical import seasonality_score, trend_score

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

DISPLAY_NAMES = {"XAUUSD": "Gold", "XAU": "Gold"}


def load_indicators_cfg() -> dict:
    with open(CONFIG_DIR / "indicators.yaml") as f:
        return yaml.safe_load(f)


def load_pairs_cfg() -> dict:
    with open(CONFIG_DIR / "pairs.yaml") as f:
        return yaml.safe_load(f)


def _dir(actual, benchmark, direction, deadband_pct: float = 0.0):
    """Directional surprise score with an optional neutral deadband.

    Returns +1 when `actual` beats `benchmark`, -1 when it misses, 0 when the
    surprise falls inside the deadband. `deadband_pct` is a fraction of
    |benchmark| that the surprise must exceed before the cell scores non-zero,
    so marginal beats/misses round to neutral instead of always becoming +-1.
    deadband_pct=0.0 reproduces the original strict-sign behaviour exactly.
    Flipped at the end for down_is_bullish indicators.
    """
    if actual is None or benchmark is None:
        return None
    tol = deadband_pct * abs(benchmark)
    diff = actual - benchmark
    if diff > tol:
        s = 1
    elif diff < -tol:
        s = -1
    else:
        s = 0
    if direction == "down_is_bullish":
        s = -s
    return s


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
    myfxbook_ppi: dict | None = None,
    investing_cc: dict | None = None,
    investing_jolts: dict | None = None,
    investing_adp: dict | None = None,
    investing_retail_sales: dict | None = None,
    rates_outlook: dict | None = None,
    investing_core: dict | None = None,
    treasury_2y: list | None = None,
    surprise_deadband: float | None = None,
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
    # Neutral deadband for macro surprise scoring. Precedence: explicit arg >
    # env var VECTOR_SURPRISE_DEADBAND > config key surprise_deadband_pct > 0.0.
    # 0.0 = strict sign (original behaviour); e.g. 0.05 = require the beat/miss
    # to exceed 5% of the benchmark before the cell scores +-1.
    if surprise_deadband is not None:
        db = float(surprise_deadband)
    else:
        env_db = os.environ.get("VECTOR_SURPRISE_DEADBAND")
        if env_db not in (None, ""):
            db = float(env_db)
        else:
            db = float(cfg.get("surprise_deadband_pct", 0.0) or 0.0)
    ff_history = ff_history or {}
    te_history = te_history or {}
    investing_mpmi = investing_mpmi or {}
    investing_spmi = investing_spmi or {}
    investing_cpi = investing_cpi or {}
    investing_ppi = investing_ppi or {}
    myfxbook_ppi = myfxbook_ppi or {}
    investing_cc = investing_cc or {}
    investing_jolts = investing_jolts or {}
    investing_adp = investing_adp or {}
    investing_retail_sales = investing_retail_sales or {}
    rates_outlook = rates_outlook or {}
    investing_core = investing_core or {}
    treasury_2y = treasury_2y or []
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
                    per_ccy[ccy][ind_id] = _dir(actual, benchmark, direction, db)
                    continue

                # Retail sales:
                # - AUD: ABS Monthly Household Spending Indicator (acceleration).
                # - CAD: Investing.com Retail Sales MoM (id 260). Actual vs
                #   Forecast; fall back to Previous if Forecast missing.
                # - Other 6: TE retail sales. Actual vs Consensus, fall back
                #   to TEForecast if Consensus missing.
                if ind_id == "retail_sales" and ccy == "CAD" and investing_retail_sales.get("CAD"):
                    rel = investing_retail_sales["CAD"]
                    actual = rel.get("actual")
                    benchmark = rel.get("forecast")
                    if benchmark is None:
                        benchmark = rel.get("previous")
                    if actual is None or benchmark is None:
                        per_ccy[ccy][ind_id] = None
                        continue
                    per_ccy[ccy][ind_id] = _dir(actual, benchmark, direction, db)
                    continue
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
                    per_ccy[ccy][ind_id] = _dir(actual, benchmark, direction, db)
                    continue

                # Consumer Confidence:
                # - USD: Investing.com CB Consumer Confidence (event id 48).
                #   Actual vs Forecast; fall back to Previous if Forecast is
                #   missing. This is the source of truth for the USD cell.
                # - Other 7: TE momentum (Actual vs Previous, below).
                if ind_id == "consumer_conf" and ccy == "USD" and investing_cc.get("USD"):
                    rel = investing_cc["USD"]
                    actual = rel.get("actual")
                    benchmark = rel.get("forecast")
                    if benchmark is None:
                        benchmark = rel.get("previous")
                    if actual is None or benchmark is None:
                        per_ccy[ccy][ind_id] = None
                        continue
                    per_ccy[ccy][ind_id] = _dir(actual, benchmark, direction, db)
                    continue

                # Consumer Confidence (non-USD): Actual vs PREVIOUS on the
                # latest release (momentum scoring). The TEForecast comparison
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
                # - NZD/GBP: Investing.com PPI Output. Actual vs Forecast;
                #   fall back to Previous if Forecast missing.
                # - CHF/AUD: Myfxbook PPI YoY. Actual vs Consensus; fall
                #   back to Previous if Consensus missing.
                # - Other 4 (USD/EUR/JPY/CAD): TE producer-prices page.
                #   Actual vs Consensus, fall back to TEForecast.
                if ind_id == "ppi" and ccy in ("CHF", "AUD") and myfxbook_ppi.get(ccy):
                    rel = myfxbook_ppi[ccy]
                    actual = rel.get("actual")
                    benchmark = rel.get("consensus")
                    if benchmark is None:
                        benchmark = rel.get("previous")
                    if actual is None or benchmark is None:
                        per_ccy[ccy][ind_id] = None
                        continue
                    per_ccy[ccy][ind_id] = _dir(actual, benchmark, direction, db)
                    continue
                if ind_id == "ppi" and ccy in ("NZD", "GBP") and investing_ppi.get(ccy):
                    rel = investing_ppi[ccy]
                    actual = rel.get("actual")
                    benchmark = rel.get("forecast")
                    if benchmark is None:
                        benchmark = rel.get("previous")
                    if actual is None or benchmark is None:
                        per_ccy[ccy][ind_id] = None
                        continue
                    per_ccy[ccy][ind_id] = _dir(actual, benchmark, direction, db)
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
                    per_ccy[ccy][ind_id] = _dir(actual, benchmark, direction, db)
                    continue

                # NFP: US-only indicator. TE non-farm-payrolls.
                # Score USD: Actual vs Consensus (priority), fall back to
                # TEForecast. Non-USD currencies get 0 (neutral) so USD
                # pairs reflect USD's NFP direction.
                if ind_id == "nfp":
                    if ccy != "USD":
                        per_ccy[ccy][ind_id] = None
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
                    per_ccy[ccy][ind_id] = _dir(actual, benchmark, direction, db)
                    continue

                # JOLTS (Job Openings): US-only monthly release.
                # Score USD via Investing.com JOLTS Job Openings (event id
                # 1057): Actual vs Forecast, fall back to Previous if Forecast
                # missing. Falls back to TE (Actual vs Consensus/TEForecast)
                # when no Investing data. Non-USD = 0 (neutral) so USD pairs
                # reflect USD's JOLTS direction. Direction is up_is_bullish
                # (more openings = strong economy = stronger USD).
                if ind_id == "jolts":
                    if ccy != "USD":
                        per_ccy[ccy][ind_id] = None
                        continue
                    if investing_jolts.get("USD"):
                        rel = investing_jolts["USD"]
                        actual = rel.get("actual")
                        benchmark = rel.get("forecast")
                        if benchmark is None:
                            benchmark = rel.get("previous")
                        if actual is None or benchmark is None:
                            per_ccy[ccy][ind_id] = None
                            continue
                        per_ccy[ccy][ind_id] = _dir(actual, benchmark, direction, db)
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
                    per_ccy[ccy][ind_id] = _dir(actual, benchmark, direction, db)
                    continue

                # ADP Employment Change: US-only monthly release.
                # Score USD via Investing.com ADP Nonfarm Employment Change
                # (event id 1): Actual vs Forecast, fall back to Previous if
                # Forecast missing. Falls back to TE (Actual vs Consensus/
                # TEForecast) when no Investing data. Non-USD = 0 (neutral) so
                # USD pairs reflect USD's ADP direction. Direction is
                # up_is_bullish (more jobs added = stronger USD).
                if ind_id == "adp":
                    if ccy != "USD":
                        per_ccy[ccy][ind_id] = None
                        continue
                    if investing_adp.get("USD"):
                        rel = investing_adp["USD"]
                        actual = rel.get("actual")
                        benchmark = rel.get("forecast")
                        if benchmark is None:
                            benchmark = rel.get("previous")
                        if actual is None or benchmark is None:
                            per_ccy[ccy][ind_id] = None
                            continue
                        per_ccy[ccy][ind_id] = _dir(actual, benchmark, direction, db)
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
                    per_ccy[ccy][ind_id] = _dir(actual, benchmark, direction, db)
                    continue

                # Jobless Claims (Unemployment Claims): US-only weekly TE
                # release. Score USD: Actual vs Consensus (priority), fall
                # back to TEForecast. Non-USD currencies get 0 (neutral) so
                # USD pairs reflect USD's claims direction. Direction is
                # down_is_bullish (lower claims = bullish USD).
                if ind_id == "jobless_claims":
                    if ccy != "USD":
                        per_ccy[ccy][ind_id] = None
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
                    per_ccy[ccy][ind_id] = _dir(actual, benchmark, direction, db)
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
                    per_ccy[ccy][ind_id] = _dir(actual, benchmark, direction, db)
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
                    per_ccy[ccy][ind_id] = _dir(actual, benchmark, direction, db)
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
                    per_ccy[ccy][ind_id] = _dir(actual, benchmark, direction, db)
                    continue

                # PMI (mpmi, spmi): EF uses CHANGE from previous to latest, not surprise.
                # For mPMI we prefer Investing.com's per-currency Latest Release page.
                # For sPMI we prefer the investing_spmi dict (6 Investing pages + 2 TE pages).
                # Both fall back to combined TE + FF history if their fresh source is missing.
                if ind_id == "mpmi" and investing_mpmi.get(ccy):
                    rel = investing_mpmi[ccy]
                    per_ccy[ccy][ind_id] = momentum_score([rel], direction=direction)
                    continue
                # sPMI special case: CHF (procure.ch) and NZD (BusinessNZ PSI)
                # score Actual vs Forecast (priority), fall back to Previous.
                # BusinessNZ doesn't publish a consensus forecast for NZD, so
                # NZD always uses the Previous fallback in practice.
                # The other 6 currencies keep the standard momentum approach.
                if ind_id == "spmi" and ccy in ("CHF", "NZD") and investing_spmi.get(ccy):
                    rel = investing_spmi[ccy]
                    actual = rel.get("actual")
                    benchmark = rel.get("forecast")
                    if benchmark is None:
                        benchmark = rel.get("previous")
                    if actual is None or benchmark is None:
                        per_ccy[ccy][ind_id] = None
                        continue
                    per_ccy[ccy][ind_id] = _dir(actual, benchmark, direction, db)
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
            if ccy in COMMODITY_CCYS:
                per_ccy[ccy]["cot"] = cot_score_commodity(cot_reading)
            else:
                per_ccy[ccy]["cot"] = cot_score(cot_reading)
        else:
            per_ccy[ccy]["cot"] = None

    # Commodity currencies (XAU, etc.) have no macro data but need COT scored.
    # Gold GDP: inverted safe-haven logic using US GDP (strong GDP = bearish).
    for ccy in COMMODITY_CCYS:
        if ccy not in per_ccy:
            per_ccy[ccy] = {}
            for cat in ("Growth", "Inflation", "Jobs"):
                for ind in cfg["categories"][cat]:
                    per_ccy[ccy][ind["id"]] = None

            if ccy == "XAU":
                # Gold uses US macro data with inverted safe-haven logic:
                # strong US economy = bearish for gold, weak = bullish.
                if te_history:
                    us_gdp = te_history.get("USD|gdp", [])
                    if us_gdp:
                        latest = sorted(us_gdp, key=lambda x: x.get("date", ""), reverse=True)[0]
                        actual = latest.get("actual")
                        benchmark = latest.get("consensus")
                        if benchmark is None:
                            benchmark = latest.get("forecast")
                        if actual is not None and benchmark is not None:
                            if actual > benchmark:
                                per_ccy[ccy]["gdp"] = -1
                            elif actual < benchmark:
                                per_ccy[ccy]["gdp"] = 1
                            else:
                                per_ccy[ccy]["gdp"] = 0

                # Retail sales: US data, Actual vs Forecast, inverted.
                if te_history:
                    us_retail = te_history.get("USD|retail_sales", [])
                    if us_retail:
                        latest = sorted(us_retail, key=lambda x: x.get("date", ""), reverse=True)[0]
                        actual = latest.get("actual")
                        benchmark = latest.get("consensus")
                        if benchmark is None:
                            benchmark = latest.get("forecast")
                        if actual is not None and benchmark is not None:
                            if actual > benchmark:
                                per_ccy[ccy]["retail_sales"] = -1
                            elif actual < benchmark:
                                per_ccy[ccy]["retail_sales"] = 1
                            else:
                                per_ccy[ccy]["retail_sales"] = 0

                # Consumer confidence: US Investing CB, Actual vs Forecast, inverted.
                us_cc = investing_cc.get("USD")
                if us_cc:
                    actual = us_cc.get("actual")
                    benchmark = us_cc.get("forecast")
                    if benchmark is None:
                        benchmark = us_cc.get("previous")
                    if actual is not None and benchmark is not None:
                        if actual > benchmark:
                            per_ccy[ccy]["consumer_conf"] = -1
                        elif actual < benchmark:
                            per_ccy[ccy]["consumer_conf"] = 1
                        else:
                            per_ccy[ccy]["consumer_conf"] = 0

                # mPMI: US manufacturing PMI, momentum (Actual vs Previous), inverted.
                us_mpmi = investing_mpmi.get("USD")
                if us_mpmi:
                    s = momentum_score([us_mpmi])
                    per_ccy[ccy]["mpmi"] = -s
                # sPMI: US services PMI, momentum (Actual vs Previous), inverted.
                us_spmi = investing_spmi.get("USD")
                if us_spmi:
                    s = momentum_score([us_spmi])
                    per_ccy[ccy]["spmi"] = -s

                # CPI: change (headline + core vs forecast) + location.
                us_cpi = investing_cpi.get("USD")
                core_cpi = investing_core.get("core_cpi")
                if us_cpi:
                    cpi_actual = us_cpi.get("actual")
                    cpi_bench = us_cpi.get("forecast")
                    if cpi_bench is None:
                        cpi_bench = us_cpi.get("previous")
                    s = 0
                    if cpi_actual is not None and cpi_bench is not None:
                        if cpi_actual > cpi_bench:
                            s -= 1
                        elif cpi_actual < cpi_bench:
                            s += 1
                    if core_cpi:
                        cc_actual = core_cpi.get("actual")
                        cc_bench = core_cpi.get("forecast")
                        if cc_bench is None:
                            cc_bench = core_cpi.get("previous")
                        if cc_actual is not None and cc_bench is not None:
                            if cc_actual > cc_bench:
                                s -= 1
                            elif cc_actual < cc_bench:
                                s += 1
                    if cpi_actual is not None:
                        if cpi_actual < 1:
                            s += 1
                        elif cpi_actual > 3:
                            s += 1
                    per_ccy[ccy]["cpi"] = s

                # PPI: change (headline + core vs forecast) + location.
                us_ppi_rels = te_history.get("USD|ppi", [])
                core_ppi = investing_core.get("core_ppi")
                if us_ppi_rels:
                    latest_ppi = sorted(us_ppi_rels, key=lambda x: x.get("date", ""), reverse=True)[0]
                    ppi_actual = latest_ppi.get("actual")
                    ppi_bench = latest_ppi.get("consensus")
                    if ppi_bench is None:
                        ppi_bench = latest_ppi.get("forecast")
                    s = 0
                    if ppi_actual is not None and ppi_bench is not None:
                        if ppi_actual > ppi_bench:
                            s -= 1
                        elif ppi_actual < ppi_bench:
                            s += 1
                    if core_ppi:
                        cp_actual = core_ppi.get("actual")
                        cp_bench = core_ppi.get("forecast")
                        if cp_bench is None:
                            cp_bench = core_ppi.get("previous")
                        if cp_actual is not None and cp_bench is not None:
                            if cp_actual > cp_bench:
                                s -= 1
                            elif cp_actual < cp_bench:
                                s += 1
                    if ppi_actual is not None:
                        if ppi_actual < 1:
                            s += 1
                        elif ppi_actual > 3:
                            s += 1
                    per_ccy[ccy]["ppi"] = s

                # PCE: change (actual vs forecast) + location.
                us_pce_rels = te_history.get("USD|pce", [])
                if us_pce_rels:
                    latest_pce = sorted(us_pce_rels, key=lambda x: x.get("date", ""), reverse=True)[0]
                    pce_actual = latest_pce.get("actual")
                    pce_bench = latest_pce.get("consensus")
                    if pce_bench is None:
                        pce_bench = latest_pce.get("forecast")
                    s = 0
                    if pce_actual is not None and pce_bench is not None:
                        if pce_actual > pce_bench:
                            s -= 1
                        elif pce_actual < pce_bench:
                            s += 1
                    if pce_actual is not None:
                        if pce_actual < 1:
                            s += 1
                        elif pce_actual > 3:
                            s += 1
                    per_ccy[ccy]["pce"] = s

                # Interest rates: 2Y Treasury yield vs 8-day SMA, inverted for gold.
                if len(treasury_2y) >= 8:
                    yields_asc = [o.value for o in reversed(treasury_2y)]
                    sma8 = sum(yields_asc[-8:]) / 8
                    latest_yield = yields_asc[-1]
                    if latest_yield > sma8:
                        per_ccy[ccy]["rates"] = -1
                    elif latest_yield < sma8:
                        per_ccy[ccy]["rates"] = 1
                    else:
                        per_ccy[ccy]["rates"] = 0

                # NFP: US data, Actual vs Consensus/TEForecast, inverted.
                us_nfp_rels = te_history.get("USD|nfp", [])
                if us_nfp_rels:
                    latest = sorted(us_nfp_rels, key=lambda x: x.get("date", ""), reverse=True)[0]
                    actual = latest.get("actual")
                    benchmark = latest.get("consensus")
                    if benchmark is None:
                        benchmark = latest.get("forecast")
                    if actual is not None and benchmark is not None:
                        if actual > benchmark:
                            per_ccy[ccy]["nfp"] = -1
                        elif actual < benchmark:
                            per_ccy[ccy]["nfp"] = 1
                        else:
                            per_ccy[ccy]["nfp"] = 0

                # Unemployment Rate: US data, inverted. Higher unemployment =
                # weak economy = bullish gold.
                us_unemp_rels = te_history.get("USD|unemployment_rate", [])
                if us_unemp_rels:
                    latest = sorted(us_unemp_rels, key=lambda x: x.get("date", ""), reverse=True)[0]
                    actual = latest.get("actual")
                    benchmark = latest.get("consensus")
                    if benchmark is None:
                        benchmark = latest.get("forecast")
                    if actual is not None and benchmark is not None:
                        if actual > benchmark:
                            per_ccy[ccy]["unemployment_rate"] = 1
                        elif actual < benchmark:
                            per_ccy[ccy]["unemployment_rate"] = -1
                        else:
                            per_ccy[ccy]["unemployment_rate"] = 0

                # Jobless Claims: US data, inverted. Higher claims = weak
                # economy = bullish gold.
                us_claims_rels = te_history.get("USD|jobless_claims", [])
                if us_claims_rels:
                    latest = sorted(us_claims_rels, key=lambda x: x.get("date", ""), reverse=True)[0]
                    actual = latest.get("actual")
                    benchmark = latest.get("consensus")
                    if benchmark is None:
                        benchmark = latest.get("forecast")
                    if actual is not None and benchmark is not None:
                        if actual > benchmark:
                            per_ccy[ccy]["jobless_claims"] = 1
                        elif actual < benchmark:
                            per_ccy[ccy]["jobless_claims"] = -1
                        else:
                            per_ccy[ccy]["jobless_claims"] = 0

                # ADP: Investing source (Actual vs Forecast), fallback TE, inverted.
                us_adp = investing_adp.get("USD")
                if us_adp:
                    actual = us_adp.get("actual")
                    benchmark = us_adp.get("forecast")
                    if benchmark is None:
                        benchmark = us_adp.get("previous")
                    if actual is not None and benchmark is not None:
                        if actual > benchmark:
                            per_ccy[ccy]["adp"] = -1
                        elif actual < benchmark:
                            per_ccy[ccy]["adp"] = 1
                        else:
                            per_ccy[ccy]["adp"] = 0
                else:
                    us_adp_rels = te_history.get("USD|adp", [])
                    if us_adp_rels:
                        latest = sorted(us_adp_rels, key=lambda x: x.get("date", ""), reverse=True)[0]
                        actual = latest.get("actual")
                        benchmark = latest.get("consensus")
                        if benchmark is None:
                            benchmark = latest.get("forecast")
                        if actual is not None and benchmark is not None:
                            if actual > benchmark:
                                per_ccy[ccy]["adp"] = -1
                            elif actual < benchmark:
                                per_ccy[ccy]["adp"] = 1
                            else:
                                per_ccy[ccy]["adp"] = 0

                # JOLTS: Investing source (Actual vs Forecast), fallback TE, inverted.
                us_jolts = investing_jolts.get("USD")
                if us_jolts:
                    actual = us_jolts.get("actual")
                    benchmark = us_jolts.get("forecast")
                    if benchmark is None:
                        benchmark = us_jolts.get("previous")
                    if actual is not None and benchmark is not None:
                        if actual > benchmark:
                            per_ccy[ccy]["jolts"] = -1
                        elif actual < benchmark:
                            per_ccy[ccy]["jolts"] = 1
                        else:
                            per_ccy[ccy]["jolts"] = 0
                else:
                    us_jolts_rels = te_history.get("USD|jolts", [])
                    if us_jolts_rels:
                        latest = sorted(us_jolts_rels, key=lambda x: x.get("date", ""), reverse=True)[0]
                        actual = latest.get("actual")
                        benchmark = latest.get("consensus")
                        if benchmark is None:
                            benchmark = latest.get("forecast")
                        if actual is not None and benchmark is not None:
                            if actual > benchmark:
                                per_ccy[ccy]["jolts"] = -1
                            elif actual < benchmark:
                                per_ccy[ccy]["jolts"] = 1
                            else:
                                per_ccy[ccy]["jolts"] = 0

            if ccy == "NKY":
                # Nikkei 225: Japanese equity index. Risk-on mapping (a strong
                # Japanese economy lifts the index), so growth, jobs and
                # inflation reuse JPY's per-currency scores directly. Interest
                # Rates uses the US 2Y yield (EdgeFinder's index rate input),
                # inverted because rising yields are an equity headwind. US-only
                # labour cells (NFP/ADP/JOLTS/Claims/PCE) stay blank, matching
                # EdgeFinder's index row.
                jpy = per_ccy.get("JPY", {})
                for ind_id in ("gdp", "mpmi", "spmi", "retail_sales",
                               "consumer_conf", "cpi", "ppi", "unemployment_rate"):
                    per_ccy[ccy][ind_id] = jpy.get(ind_id)
                if len(treasury_2y) >= 8:
                    yields_asc = [o.value for o in reversed(treasury_2y)]
                    sma8 = sum(yields_asc[-8:]) / 8
                    latest_yield = yields_asc[-1]
                    if latest_yield > sma8:
                        per_ccy[ccy]["rates"] = -1
                    elif latest_yield < sma8:
                        per_ccy[ccy]["rates"] = 1
                    else:
                        per_ccy[ccy]["rates"] = 0

            cot_reading = cot_data.get(ccy)
            if cot_reading and not getattr(cot_reading, "is_stale", False):
                per_ccy[ccy]["cot"] = cot_score_commodity(cot_reading)
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
        # Exception: for commodity pairs (base in COMMODITY_CCYS), COT is
        # the asset's own score, not a base-quote diff.
        for ind_id in indicator_ids:
            if ind_id in pair_level:
                continue
            if not quote:
                # Standalone instrument (e.g. an index with no quote currency):
                # show the asset's own per-indicator score, not a base-quote diff.
                s = per_ccy.get(base, {}).get(ind_id)
                scores[ind_id] = max(-2, min(2, s)) if s is not None else 0
                continue
            if ind_id == "cot" and base in COMMODITY_CCYS:
                s = per_ccy.get(base, {}).get("cot")
                scores[ind_id] = s if s is not None else 0
                continue
            if ind_id in ("nfp", "unemployment_rate", "jobless_claims", "adp", "jolts") and base in COMMODITY_CCYS:
                s = per_ccy.get(base, {}).get(ind_id)
                scores[ind_id] = s if s is not None else 0
                continue
            base_s = per_ccy.get(base, {}).get(ind_id)
            quote_s = per_ccy.get(quote, {}).get(ind_id)
            if base_s is None or quote_s is None:
                if ind_id in ("nfp", "jobless_claims", "adp", "jolts"):
                    if base_s is not None:
                        scores[ind_id] = max(-2, min(2, base_s))
                    elif quote_s is not None:
                        scores[ind_id] = max(-2, min(2, -quote_s))
                    else:
                        scores[ind_id] = None
                else:
                    scores[ind_id] = 0
            else:
                diff = base_s - quote_s
                scores[ind_id] = max(-2, min(2, diff))

        # Pair-level indicators
        df_4h = (prices_4h or {}).get(sym)
        scores["trend"] = trend_score(df, df_4h)
        scores["seasonality"] = seasonality_score(df, as_of_date=as_of_date,
                                                   commodity=base == "XAU")
        if base in COMMODITY_CCYS and cot_data:
            # Non-FX assets (Gold, Nikkei) have no retail-broker sentiment feed,
            # so crowd uses COT non-reportable positioning as a contrarian proxy
            # (heavy retail long = bearish, heavy retail short = bullish). This
            # gives the Nikkei a contrarian signal EdgeFinder's index row leaves
            # neutral, by deliberate choice.
            scores["crowd"] = crowd_score_commodity(cot_data.get(base))
        else:
            scores["crowd"] = retail_score(retail_data.get(sym))

        total = sum(v for v in scores.values() if v is not None)

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
            "display_name": DISPLAY_NAMES.get(sym, sym),
            "base": base,
            "quote": quote,
            "scores": scores,
            "total": total,
            "bias": bias_label(total, thresholds),
            "cot_stale": cot_stale,
        })

    rows.sort(key=lambda r: r["total"], reverse=True)
    return rows


def build_currency_rows(
    per_ccy: dict,
    cot_data: dict | None = None,
) -> list[dict]:
    """
    Return one row per individual currency (USD, EUR, GBP, ...) showing the
    per-currency macro scores directly (no pair-diff calculation). Pair-only
    indicators (trend, seasonality, crowd) are set to None so the template
    renders them as 'n/a' instead of misleading zeros.
    """
    cfg = load_indicators_cfg()
    thresholds = cfg["bias_thresholds"]

    indicator_ids: list[str] = []
    for cat_name, inds in cfg["categories"].items():
        for ind in inds:
            indicator_ids.append(ind["id"])
    pair_level = {"trend", "seasonality", "crowd"}

    rows = []
    for ccy in ("USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD", "XAU"):
        ccy_scores = per_ccy.get(ccy, {})
        scores: dict[str, int | None] = {}
        for ind_id in indicator_ids:
            if ind_id in pair_level:
                scores[ind_id] = None  # not applicable to a single currency
            else:
                s = ccy_scores.get(ind_id)
                scores[ind_id] = s if s is not None else 0

        total = sum(v for v in scores.values() if v is not None)

        # Flag the COT cell stale if this currency's COT reading is stale
        cot_stale = False
        if cot_data:
            reading = cot_data.get(ccy)
            if reading and getattr(reading, "is_stale", False):
                cot_stale = True

        rows.append({
            "symbol": ccy,
            "display_name": DISPLAY_NAMES.get(ccy, ccy),
            "base": ccy,
            "quote": "",
            "scores": scores,
            "total": total,
            "bias": bias_label(total, thresholds),
            "cot_stale": cot_stale,
            "is_currency": True,
        })
    # Sort by total descending, same ordering convention as pair rows
    rows.sort(key=lambda r: r["total"], reverse=True)
    return rows


def build_heatmap(macro_data, cot_data, retail_data, prices, prices_4h=None, as_of_date=None, ff_history=None, te_history=None, investing_mpmi=None, investing_spmi=None, abs_au_mhsi=None, investing_cpi=None, investing_ppi=None, myfxbook_ppi=None, investing_cc=None, investing_jolts=None, investing_adp=None, investing_retail_sales=None, rates_outlook=None, investing_core=None, treasury_2y=None) -> dict:
    cfg = load_indicators_cfg()
    indicator_meta = []
    cat_groups: dict[str, list[str]] = {}
    for cat_name, inds in cfg["categories"].items():
        cat_groups[cat_name] = [i["id"] for i in inds]
        for i in inds:
            indicator_meta.append({"id": i["id"], "label": i["label"], "category": cat_name})

    per_ccy = build_currency_scores(macro_data, cot_data, ff_history=ff_history, te_history=te_history, investing_mpmi=investing_mpmi, investing_spmi=investing_spmi, abs_au_mhsi=abs_au_mhsi, investing_cpi=investing_cpi, investing_ppi=investing_ppi, myfxbook_ppi=myfxbook_ppi, investing_cc=investing_cc, investing_jolts=investing_jolts, investing_adp=investing_adp, investing_retail_sales=investing_retail_sales, rates_outlook=rates_outlook, investing_core=investing_core, treasury_2y=treasury_2y)
    pair_rows = build_pair_rows(per_ccy, prices, retail_data, prices_4h=prices_4h, as_of_date=as_of_date, cot_data=cot_data)
    for r in pair_rows:
        r["is_currency"] = False
    currency_rows = build_currency_rows(per_ccy, cot_data=cot_data)
    rows = pair_rows + currency_rows

    # COT freshness map: ccy -> {"status": "fresh"|"stale"|"missing", "date": ..., "days_old": ...}
    # Used by the template to show staleness on the heatmap.
    cot_status = {}
    for ccy in ("USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD", "XAU"):
        reading = (cot_data or {}).get(ccy)
        if reading is None:
            cot_status[ccy] = {"status": "missing", "date": None, "days_old": None}
        elif getattr(reading, "is_stale", False):
            cot_status[ccy] = {"status": "stale", "date": reading.report_date, "days_old": reading.days_old}
        else:
            cot_status[ccy] = {"status": "fresh", "date": reading.report_date, "days_old": reading.days_old}

    # Aggregated freshness check across COT + all Investing-sourced indicators.
    # Different release cadences need different thresholds: weekly for COT,
    # monthly for CPI/PMI in most countries, quarterly for AUD/NZD CPI and
    # NZD PPI. Anything past its max-age window goes in the banner.
    stale_items = _compute_data_staleness(
        cot_data=cot_data,
        investing_cpi=investing_cpi,
        investing_ppi=investing_ppi,
        investing_mpmi=investing_mpmi,
        investing_spmi=investing_spmi,
        investing_cc=investing_cc,
        investing_jolts=investing_jolts,
        investing_adp=investing_adp,
        myfxbook_ppi=myfxbook_ppi,
        investing_retail_sales=investing_retail_sales,
        as_of_date=as_of_date,
    )

    return {
        "indicators": indicator_meta,
        "categories": cat_groups,
        "rows": rows,
        "per_ccy": per_ccy,
        "as_of_date": as_of_date,
        "cot_status": cot_status,
        "stale_items": stale_items,
    }


# Per-indicator max age (in days) before we flag the data as stale.
# Tune here if any of these change publishing cadence.
_MAX_AGE_DAYS = {
    "COT":     14,   # weekly publish + buffer week
    "CPI YoY": 40,   # monthly for most; quarterly handled separately below
    "PPI YoY": 110,  # NZD only, quarterly release
    "mPMI":    40,   # monthly
    "sPMI":    40,   # monthly
    "Consumer Confidence": 40,  # USD only (Investing CB Consumer Confidence), monthly
    "JOLTS": 75,     # USD only (Investing JOLTS Job Openings), monthly but ~6wk lag
    "ADP":   40,     # USD only (Investing ADP Employment Change), monthly
}
_QUARTERLY_CPI_CCYS = {"AUD", "NZD"}
_MAX_AGE_CPI_QUARTERLY = 110


def _compute_data_staleness(cot_data, investing_cpi, investing_ppi,
                            investing_mpmi, investing_spmi, as_of_date,
                            investing_cc=None, investing_jolts=None,
                            investing_adp=None, myfxbook_ppi=None,
                            investing_retail_sales=None) -> list:
    """
    Return a flat list of stale data entries across COT + Investing-sourced
    indicators. Each entry: {indicator, ccy, date, days_old, max_age}.
    Caller iterates and renders them in the banner.
    """
    from datetime import datetime, timezone
    today_str = as_of_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        today = datetime.strptime(today_str, "%Y-%m-%d")
    except (TypeError, ValueError):
        today = datetime.now(timezone.utc).replace(tzinfo=None)

    out: list[dict] = []

    def _check(indicator: str, ccy: str, date_str: str, max_age: int):
        if not date_str:
            return
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return
        days_old = (today - d).days
        if days_old > max_age:
            out.append({
                "indicator": indicator,
                "ccy": ccy,
                "date": date_str,
                "days_old": int(days_old),
                "max_age": max_age,
            })

    # COT: already has is_stale flag computed in cot.py.
    if cot_data:
        for ccy, r in cot_data.items():
            if getattr(r, "is_stale", False):
                out.append({
                    "indicator": "COT",
                    "ccy": ccy,
                    "date": r.report_date,
                    "days_old": int(getattr(r, "days_old", 0)),
                    "max_age": _MAX_AGE_DAYS["COT"],
                })

    # CPI YoY: per-currency, with quarterly handling for AUD/NZD.
    for ccy, reading in (investing_cpi or {}).items():
        max_age = _MAX_AGE_CPI_QUARTERLY if ccy in _QUARTERLY_CPI_CCYS else _MAX_AGE_DAYS["CPI YoY"]
        _check("CPI YoY", ccy, (reading or {}).get("date"), max_age)

    # PPI YoY (Investing): NZD only, quarterly.
    for ccy, reading in (investing_ppi or {}).items():
        _check("PPI YoY", ccy, (reading or {}).get("date"), _MAX_AGE_DAYS["PPI YoY"])

    # mPMI: monthly for all 8.
    for ccy, reading in (investing_mpmi or {}).items():
        _check("mPMI", ccy, (reading or {}).get("date"), _MAX_AGE_DAYS["mPMI"])

    # sPMI: monthly for all 8.
    for ccy, reading in (investing_spmi or {}).items():
        _check("sPMI", ccy, (reading or {}).get("date"), _MAX_AGE_DAYS["sPMI"])

    # Consumer Confidence (Investing): USD only, monthly.
    for ccy, reading in (investing_cc or {}).items():
        _check("Consumer Confidence", ccy, (reading or {}).get("date"), _MAX_AGE_DAYS["Consumer Confidence"])

    # JOLTS (Investing): USD only, monthly with ~6-week publication lag.
    for ccy, reading in (investing_jolts or {}).items():
        _check("JOLTS", ccy, (reading or {}).get("date"), _MAX_AGE_DAYS["JOLTS"])

    # ADP (Investing): USD only, monthly.
    for ccy, reading in (investing_adp or {}).items():
        _check("ADP", ccy, (reading or {}).get("date"), _MAX_AGE_DAYS["ADP"])

    # PPI YoY (Myfxbook): CHF only, monthly.
    for ccy, reading in (myfxbook_ppi or {}).items():
        _check("PPI YoY", ccy, (reading or {}).get("date"), _MAX_AGE_DAYS["mPMI"])

    # Retail Sales (Investing): CAD only, monthly.
    for ccy, reading in (investing_retail_sales or {}).items():
        _check("Retail Sales", ccy, (reading or {}).get("date"), _MAX_AGE_DAYS["mPMI"])

    return out
