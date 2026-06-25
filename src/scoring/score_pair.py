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
from src.scoring.score_surprise import surprise_score
from src.scoring.score_technical import range_position, seasonality_score, trend_score

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

# Equity indices use EdgeFinder's SMA20/50/200 chart-trend method; FX pairs and
# metals (the rest of COMMODITY_CCYS) use the published SMA3/14 crossover. See
# trend_score in score_technical.py and the trend-score-divergence memory.
INDEX_CCYS = {"NDX", "NKY", "UKX"}

DISPLAY_NAMES = {"XAUUSD": "Gold", "XAU": "Gold", "PLATINUM": "Platinum", "XPT": "Platinum",
                 "SILVER": "Silver", "XAG": "Silver"}


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


def _dir_fcst(actual, forecast, previous, direction, deadband_pct: float = 0.0):
    """Surprise score with EdgeFinder's no-forecast rule.

    EdgeFinder is surprise-only: with no published forecast there is no
    surprise to measure, so the cell is NEUTRAL (0), not a momentum read vs
    Previous. `previous` is accepted for signature symmetry but intentionally
    unused for scoring (kept so callers read naturally and a future change can
    reference it). Returns None only when the release itself is missing
    (actual is None), so a genuinely absent indicator still shows n/a.
    """
    if actual is None:
        return None
    if forecast is None:
        return 0
    return _dir(actual, forecast, direction, deadband_pct)


def _dir_fcst_or_prev(actual, forecast, previous, direction, deadband_pct: float = 0.0):
    """Surprise score with a momentum fallback: score Actual vs Forecast when a
    forecast is published, otherwise Actual vs Previous (instead of EdgeFinder's
    no-forecast=neutral rule). Used only where requested (AUD mPMI / PPI), since
    those releases routinely publish no forecast. Returns None only when there is
    nothing to compare against (no actual, or neither forecast nor previous)."""
    if actual is None:
        return None
    benchmark = forecast if forecast is not None else previous
    if benchmark is None:
        return None
    return _dir(actual, benchmark, direction, deadband_pct)


def _dir_mag(actual, benchmark, direction, t0_pp: float, t1_pp: float):
    """Magnitude-binned surprise score (-2..+2) for a single currency.

    Unlike _dir (sign-only +-1), this bins by the SIZE of the surprise in
    percentage points so a large beat/miss can reach +-2 and dominate the
    pair diff: |surprise| <= t0 -> 0, t0 < |surprise| <= t1 -> +-1,
    |surprise| > t1 -> +-2. Sign taken from actual - benchmark, flipped for
    down_is_bullish. Returns None when either input is missing.
    """
    if actual is None or benchmark is None:
        return None
    diff = actual - benchmark
    mag = abs(diff)
    sign = 1 if diff > 0 else (-1 if diff < 0 else 0)
    if mag <= t0_pp:
        s = 0
    elif mag <= t1_pp:
        s = sign
    else:
        s = sign * 2
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
    investing_gdp: dict | None = None,
    myfxbook_ppi: dict | None = None,
    investing_cc: dict | None = None,
    investing_jolts: dict | None = None,
    investing_adp: dict | None = None,
    investing_pce: dict | None = None,
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
    # PPI magnitude scoring (prototype, off by default). Env VECTOR_PPI_MAGNITUDE
    # (1/0) overrides the config flag for quick A/B runs.
    ppi_cfg = cfg.get("ppi_magnitude") or {}
    ppi_mag_enabled = bool(ppi_cfg.get("enabled", False))
    env_ppi = os.environ.get("VECTOR_PPI_MAGNITUDE")
    if env_ppi not in (None, ""):
        ppi_mag_enabled = env_ppi not in ("0", "false", "False")
    ppi_t0 = float(ppi_cfg.get("t0_pp", 0.45))
    ppi_t1 = float(ppi_cfg.get("t1_pp", 0.70))
    ppi_te_use_forecast = bool(ppi_cfg.get("te_use_forecast", True))
    ff_history = ff_history or {}
    te_history = te_history or {}
    investing_mpmi = investing_mpmi or {}
    investing_spmi = investing_spmi or {}
    investing_cpi = investing_cpi or {}
    investing_ppi = investing_ppi or {}
    investing_gdp = investing_gdp or {}
    myfxbook_ppi = myfxbook_ppi or {}
    investing_cc = investing_cc or {}
    investing_jolts = investing_jolts or {}
    investing_adp = investing_adp or {}
    investing_pce = investing_pce or {}
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

                # GDP:
                # - JPY: Investing.com Japan GDP QoQ (id 119), Actual vs Forecast
                #   (neutral if no forecast, per EF's surprise rule).
                # - Other 7: latest TE release, Actual vs Consensus (priority),
                #   fall back to TEForecast if Consensus is missing.
                if ind_id == "gdp" and ccy == "JPY" and investing_gdp.get("JPY"):
                    rel = investing_gdp["JPY"]
                    per_ccy[ccy][ind_id] = _dir_fcst(
                        rel.get("actual"), rel.get("forecast"),
                        rel.get("previous"), direction, db)
                    continue
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
                    per_ccy[ccy][ind_id] = _dir_fcst(
                        rel.get("actual"), rel.get("forecast"),
                        rel.get("previous"), direction, db)
                    continue
                if ind_id == "retail_sales" and ccy == "AUD":
                    # ABS Monthly Household Spending Indicator publishes no
                    # consensus forecast, so under EF's surprise-only rule the
                    # AUD retail cell is neutral (0) whenever a print exists.
                    mhsi = abs_au_mhsi or {}
                    cur = mhsi.get("current_mom")
                    per_ccy[ccy][ind_id] = 0 if cur is not None else None
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

                # Consumer Confidence (non-USD): Actual vs Forecast (Consensus,
                # TEForecast fallback), the same surprise rule as every other
                # release. Beating a low TE forecast while declining vs Previous
                # still scores bullish: that is the surprise principle EF uses,
                # not a bug. No forecast published -> neutral (via _dir_fcst).
                if ind_id == "consumer_conf":
                    te_rels = te_history.get(key, [])
                    if not te_rels:
                        per_ccy[ccy][ind_id] = None
                        continue
                    latest = sorted(te_rels, key=lambda x: x.get("date", ""), reverse=True)[0]
                    forecast = latest.get("consensus")
                    if forecast is None:
                        forecast = latest.get("forecast")  # TEForecast fallback
                    per_ccy[ccy][ind_id] = _dir_fcst(
                        latest.get("actual"), forecast,
                        latest.get("previous"), direction, db)
                    continue

                # PPI YoY:
                # - NZD/GBP: Investing.com PPI Output. Actual vs Forecast;
                #   fall back to Previous if Forecast missing.
                # - CHF/AUD: Myfxbook PPI YoY. Actual vs Consensus; fall
                #   back to Previous if Consensus missing.
                # - Other 4 (USD/EUR/JPY/CAD): TE producer-prices page.
                #   Actual vs Consensus, fall back to TEForecast.
                # Source precedence is identical regardless of scoring mode;
                # only the final scorer differs (sign-only _dir/_dir_fcst vs
                # magnitude _dir_mag) when ppi_magnitude is enabled.
                if ind_id == "ppi":
                    src = None
                    actual = benchmark = previous = None
                    if ccy in ("CHF", "AUD") and myfxbook_ppi.get(ccy):
                        rel = myfxbook_ppi[ccy]
                        actual = rel.get("actual")
                        benchmark = rel.get("consensus")
                        previous = rel.get("previous")
                        src = "fcst"
                    elif ccy in ("NZD", "GBP") and investing_ppi.get(ccy):
                        rel = investing_ppi[ccy]
                        actual = rel.get("actual")
                        benchmark = rel.get("forecast")
                        previous = rel.get("previous")
                        src = "fcst"
                    else:
                        te_rels = te_history.get(key, [])
                        if not te_rels:
                            per_ccy[ccy][ind_id] = None
                            continue
                        latest = sorted(te_rels, key=lambda x: x.get("date", ""), reverse=True)[0]
                        actual = latest.get("actual")
                        cons = latest.get("consensus")
                        fcst = latest.get("forecast")  # TEForecast
                        if ppi_mag_enabled and ppi_te_use_forecast:
                            benchmark = fcst if fcst is not None else cons
                        else:
                            benchmark = cons if cons is not None else fcst
                        src = "te"

                    if ppi_mag_enabled:
                        # Magnitude path: when no forecast/consensus is published,
                        # fall back to Previous (momentum) so a surprise size can
                        # still be measured; _dir_mag returns None if both absent.
                        bench = benchmark if benchmark is not None else previous
                        per_ccy[ccy][ind_id] = _dir_mag(
                            actual, bench, direction, ppi_t0, ppi_t1)
                    elif ccy == "AUD":
                        # AUD PPI: when no forecast is published, fall back to
                        # Actual vs Previous (momentum) instead of neutral, per
                        # request. With a forecast present it behaves like _dir_fcst.
                        per_ccy[ccy][ind_id] = _dir_fcst_or_prev(
                            actual, benchmark, previous, direction, db)
                    elif src == "fcst":
                        # EF no-forecast rule: missing forecast -> neutral.
                        per_ccy[ccy][ind_id] = _dir_fcst(
                            actual, benchmark, previous, direction, db)
                    else:
                        if actual is None or benchmark is None:
                            per_ccy[ccy][ind_id] = None
                        else:
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

                # PCE YoY: US-only indicator. USD = Investing Core PCE Price
                # Index YoY (id 905), Actual vs Forecast (fallback Previous);
                # falls back to TE pce-price-index-annual-change if the
                # Investing cache is empty. Non-USD currencies get 0 (neutral)
                # so USD pairs reflect USD's PCE direction in the diff while
                # non-USD-only pairs (e.g., EURGBP) show 0 as expected.
                if ind_id == "pce":
                    if ccy != "USD":
                        per_ccy[ccy][ind_id] = 0
                        continue
                    if investing_pce.get("USD"):
                        rel = investing_pce["USD"]
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

                # CPI YoY: Investing.com per-currency Latest Release. Actual
                # vs Forecast where the forecast is published. Falls back to
                # Actual vs Previous for JPY (Investing's Japan CPI YoY page
                # never lists a forecast) and for CHF when the next Swiss
                # release's forecast hasn't been published yet.
                if ind_id == "cpi" and investing_cpi.get(ccy):
                    rel = investing_cpi[ccy]
                    per_ccy[ccy][ind_id] = _dir_fcst(
                        rel.get("actual"), rel.get("forecast"),
                        rel.get("previous"), direction, db)
                    continue

                # PMI (mpmi, spmi): EdgeFinder scores these Actual vs Forecast,
                # the same surprise rule as every other release. Verified against
                # EF's live Top Setups: JPY met both PMI forecasts exactly
                # (54.5 vs 54.5, 50.0 vs 50.0) and scored 0, so USDJPY mPMI/sPMI
                # read +1 (USD beat), not +2 (which momentum would give). When a
                # currency has no published forecast (CAD mPMI, CAD/NZD/CHF sPMI
                # via direct sources) the cell is NEUTRAL (0), not momentum.
                # mPMI prefers Investing.com's per-currency Latest Release page;
                # sPMI prefers the investing_spmi dict (6 Investing + 2 TE pages).
                # Both fall back to combined TE + FF history.
                # AUD mPMI publishes no forecast, so per request it falls back to
                # Actual vs Previous (momentum) rather than reading neutral. When
                # a forecast IS present it scores Actual vs Forecast like the rest.
                if ind_id == "mpmi" and ccy == "AUD" and investing_mpmi.get("AUD"):
                    rel = investing_mpmi["AUD"]
                    per_ccy[ccy][ind_id] = _dir_fcst_or_prev(
                        rel.get("actual"), rel.get("forecast"),
                        rel.get("previous"), direction, db)
                    continue
                if ind_id == "mpmi" and investing_mpmi.get(ccy):
                    rel = investing_mpmi[ccy]
                    per_ccy[ccy][ind_id] = _dir_fcst(
                        rel.get("actual"), rel.get("forecast"),
                        rel.get("previous"), direction, db)
                    continue
                if ind_id == "spmi" and investing_spmi.get(ccy):
                    rel = investing_spmi[ccy]
                    per_ccy[ccy][ind_id] = _dir_fcst(
                        rel.get("actual"), rel.get("forecast"),
                        rel.get("previous"), direction, db)
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
                    if not combined:
                        per_ccy[ccy][ind_id] = None
                        continue
                    latest = combined[0]
                    forecast = latest.get("consensus") or latest.get("forecast")
                    per_ccy[ccy][ind_id] = _dir_fcst(
                        latest.get("actual"), forecast,
                        latest.get("previous"), direction, db)
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

                # mPMI: US manufacturing PMI, Actual vs Forecast (fallback
                # Previous), inverted for the safe-haven mapping.
                us_mpmi = investing_mpmi.get("USD")
                if us_mpmi:
                    actual = us_mpmi.get("actual")
                    benchmark = us_mpmi.get("forecast")
                    if benchmark is None:
                        benchmark = us_mpmi.get("previous")
                    if actual is not None and benchmark is not None:
                        if actual > benchmark:
                            per_ccy[ccy]["mpmi"] = -1
                        elif actual < benchmark:
                            per_ccy[ccy]["mpmi"] = 1
                        else:
                            per_ccy[ccy]["mpmi"] = 0
                # sPMI: US ISM Non-Manufacturing PMI, Actual vs Forecast
                # (fallback Previous), inverted for the safe-haven mapping.
                us_spmi = investing_spmi.get("USD")
                if us_spmi:
                    actual = us_spmi.get("actual")
                    benchmark = us_spmi.get("forecast")
                    if benchmark is None:
                        benchmark = us_spmi.get("previous")
                    if actual is not None and benchmark is not None:
                        if actual > benchmark:
                            per_ccy[ccy]["spmi"] = -1
                        elif actual < benchmark:
                            per_ccy[ccy]["spmi"] = 1
                        else:
                            per_ccy[ccy]["spmi"] = 0

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
                    # Each gold macro cell is a single +1/0/-1 (no +-2): the
                    # headline/core/location components only decide the sign.
                    per_ccy[ccy]["cpi"] = max(-1, min(1, s))

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
                    per_ccy[ccy]["ppi"] = max(-1, min(1, s))

                # PCE: change (actual vs forecast) + location. USD = Investing
                # Core PCE Price Index YoY (fallback TE) so the gold cell tracks
                # the same source as the USD currency row.
                us_pce = investing_pce.get("USD")
                us_pce_rels = te_history.get("USD|pce", [])
                if us_pce or us_pce_rels:
                    if us_pce:
                        pce_actual = us_pce.get("actual")
                        pce_bench = us_pce.get("forecast")
                        if pce_bench is None:
                            pce_bench = us_pce.get("previous")
                    else:
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
                    per_ccy[ccy]["pce"] = max(-1, min(1, s))

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

            if ccy == "NDX":
                # NASDAQ-100: US equity index. Risk-on mapping driven entirely
                # by US macro (unlike the Nikkei, whose labour cells stay blank):
                # growth + jobs mirror the USD currency cells un-inverted (strong
                # US economy = bullish index); inflation (CPI/PPI/PCE) is inverted
                # (hot inflation = Fed-hike fear = bearish equities). Interest
                # Rates uses the US 2Y yield vs its 21-day SMA, inverted (rising
                # yields = equity headwind) - the "2 Yr Yield (21 day SMA)" cell
                # EdgeFinder shows for indices. Verified against EdgeFinder's
                # NASDAQ Asset Scorecard (Fundamentals +1: growth +2, inflation
                # -3, jobs +2).
                usd = per_ccy.get("USD", {})
                for ind_id in ("gdp", "mpmi", "spmi", "retail_sales",
                               "consumer_conf", "nfp", "adp",
                               "unemployment_rate", "jobless_claims", "jolts"):
                    per_ccy[ccy][ind_id] = usd.get(ind_id)
                for ind_id in ("cpi", "ppi", "pce"):
                    v = usd.get(ind_id)
                    per_ccy[ccy][ind_id] = (-v if v is not None else None)
                if len(treasury_2y) >= 21:
                    yields_asc = [o.value for o in reversed(treasury_2y)]
                    sma21 = sum(yields_asc[-21:]) / 21
                    latest_yield = yields_asc[-1]
                    if latest_yield > sma21:
                        per_ccy[ccy]["rates"] = -1
                    elif latest_yield < sma21:
                        per_ccy[ccy]["rates"] = 1
                    else:
                        per_ccy[ccy]["rates"] = 0

            if ccy == "UKX":
                # FTSE 100: UK equity index, same risk-on mapping as the NASDAQ
                # but driven by UK macro via GBP's cells. Growth + jobs mirror
                # GBP un-inverted (strong UK economy = bullish index); inflation
                # (CPI/PPI) is inverted (hot inflation = BoE-hike fear = bearish
                # equities). Verified against EdgeFinder's UK100 "Stocks Impact"
                # column: e.g. the CPI miss (2.8 vs 3.0) is GBP-bearish but
                # stocks-bullish (+1). US-only labour cells (NFP/ADP/JOLTS/Claims)
                # and PCE stay blank. Interest Rates uses the 2Y yield vs its
                # 21-day SMA, inverted - the same index rate input as the NASDAQ
                # (US 2Y; there is no daily UK 2Y gilt feed available).
                gbp = per_ccy.get("GBP", {})
                for ind_id in ("gdp", "mpmi", "spmi", "retail_sales",
                               "consumer_conf", "unemployment_rate"):
                    per_ccy[ccy][ind_id] = gbp.get(ind_id)
                for ind_id in ("cpi", "ppi"):
                    v = gbp.get(ind_id)
                    per_ccy[ccy][ind_id] = (-v if v is not None else None)
                if len(treasury_2y) >= 21:
                    yields_asc = [o.value for o in reversed(treasury_2y)]
                    sma21 = sum(yields_asc[-21:]) / 21
                    latest_yield = yields_asc[-1]
                    if latest_yield > sma21:
                        per_ccy[ccy]["rates"] = -1
                    elif latest_yield < sma21:
                        per_ccy[ccy]["rates"] = 1
                    else:
                        per_ccy[ccy]["rates"] = 0

            if ccy == "XPT":
                # Platinum: industrial precious metal (~40% auto-catalyst
                # demand). Unlike Gold's safe-haven inversion, EdgeFinder scores
                # platinum's real economy PRO-CYCLICALLY: a strong US economy is
                # bullish, so growth + jobs mirror the USD currency cells
                # un-inverted. Inflation is inverted and rates come from the 2Y
                # yield (hot inflation / rising real yields lift the USD and
                # weigh on the metal). Verified cell-for-cell against EdgeFinder.
                usd = per_ccy.get("USD", {})
                for ind_id in ("gdp", "mpmi", "spmi", "retail_sales",
                               "consumer_conf", "nfp", "adp",
                               "unemployment_rate", "jobless_claims", "jolts"):
                    per_ccy[ccy][ind_id] = usd.get(ind_id)
                for ind_id in ("cpi", "ppi", "pce"):
                    v = usd.get(ind_id)
                    per_ccy[ccy][ind_id] = (-v if v is not None else None)
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

            if ccy == "XAG":
                # Silver: safe-haven precious metal (like Gold, unlike the
                # industrial Platinum). EdgeFinder inverts ALL US macro: a strong
                # economy, strong jobs and hot inflation are bearish silver; weak
                # data is bullish. Rates from the 2Y yield (rising = hawkish =
                # bearish metal). Verified cell-for-cell against EdgeFinder's
                # silver Asset Scorecard.
                usd = per_ccy.get("USD", {})
                for ind_id in ("gdp", "mpmi", "spmi", "retail_sales",
                               "consumer_conf", "cpi", "ppi", "pce", "nfp",
                               "adp", "unemployment_rate", "jobless_claims",
                               "jolts"):
                    v = usd.get(ind_id)
                    per_ccy[ccy][ind_id] = (-v if v is not None else None)
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


# Range-position bands for the setup state. <=35% of the lookback range is
# "discount" territory, >=65% is "premium"; in between is mid-range.
_LOC_DISCOUNT = 35
_LOC_PREMIUM = 65


def _setup_state(bias: str, loc_pct: int | None) -> str | None:
    """
    Combine pair bias with range position into an entry-readiness state for
    supply/demand entries:
      watch    - bias is directional AND price has pulled back to the side of
                 the range where you'd hunt zones (bullish+discount or
                 bearish+premium)
      extended - bias is directional but price is at the far end of the range
      mid      - bias is directional, price mid-range
      None     - neutral bias or no price data (rendered as n/a)
    """
    if loc_pct is None or bias == "Neutral":
        return None
    bullish = bias in ("Bullish", "Very Bullish")
    if bullish:
        if loc_pct <= _LOC_DISCOUNT:
            return "watch"
        if loc_pct >= _LOC_PREMIUM:
            return "extended"
    else:
        if loc_pct >= _LOC_PREMIUM:
            return "watch"
        if loc_pct <= _LOC_DISCOUNT:
            return "extended"
    return "mid"


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
        scores["trend"] = trend_score(df, df_4h, equity_index=base in INDEX_CCYS)
        scores["seasonality"] = seasonality_score(df, as_of_date=as_of_date,
                                                   commodity=base == "XAU")
        if base in ("XPT", "XAG") and cot_data:
            # Platinum/Silver: +-1 contrarian from COT non-reportable, matching
            # EdgeFinder's metal crowd scale (Gold/Nikkei keep the +-2 commodity
            # crowd below). Heavy retail long = bearish, heavy retail short = bullish.
            r = cot_data.get(base)
            lp = getattr(r, "retail_long_pct", 50.0) if r else 50.0
            scores["crowd"] = -1 if lp >= 60 else (1 if lp <= 40 else 0)
        elif base in COMMODITY_CCYS and cot_data:
            # Non-FX assets (Gold, Nikkei) have no retail-broker sentiment feed,
            # so crowd uses COT non-reportable positioning as a contrarian proxy.
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

        bias = bias_label(total, thresholds)
        loc_pct = range_position(df)

        rows.append({
            "symbol": sym,
            "display_name": DISPLAY_NAMES.get(sym, sym),
            "base": base,
            "quote": quote,
            "scores": scores,
            "total": total,
            "bias": bias,
            "loc_pct": loc_pct,
            "setup": _setup_state(bias, loc_pct),
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

    # Currency rows sum fewer active cells than pairs, so they get their own
    # thresholds (see currency_bias_thresholds in indicators.yaml). USD and
    # XAU carry all 15 cells; other fiat zero the 5 US-only cells.
    ccy_thresh_cfg = cfg.get("currency_bias_thresholds", {})

    def _ccy_thresholds(ccy: str) -> dict:
        t = ccy_thresh_cfg.get("full" if ccy in ("USD", "XAU") else "reduced")
        if not t:
            return thresholds  # config missing: fall back to pair thresholds
        return {
            "very_bullish": t["very_bullish"],
            "bullish": t["bullish"],
            "bearish": -t["bullish"],
            "very_bearish": -t["very_bullish"],
        }

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
            "bias": bias_label(total, _ccy_thresholds(ccy)),
            "loc_pct": None,
            "setup": None,
            "cot_stale": cot_stale,
            "is_currency": True,
        })
    # Sort by total descending, same ordering convention as pair rows
    rows.sort(key=lambda r: r["total"], reverse=True)
    return rows


def build_heatmap(macro_data, cot_data, retail_data, prices, prices_4h=None, as_of_date=None, ff_history=None, te_history=None, investing_mpmi=None, investing_spmi=None, abs_au_mhsi=None, investing_cpi=None, investing_ppi=None, investing_gdp=None, myfxbook_ppi=None, investing_cc=None, investing_jolts=None, investing_adp=None, investing_pce=None, investing_retail_sales=None, rates_outlook=None, investing_core=None, treasury_2y=None) -> dict:
    cfg = load_indicators_cfg()
    indicator_meta = []
    cat_groups: dict[str, list[str]] = {}
    for cat_name, inds in cfg["categories"].items():
        cat_groups[cat_name] = [i["id"] for i in inds]
        for i in inds:
            indicator_meta.append({"id": i["id"], "label": i["label"], "category": cat_name})

    per_ccy = build_currency_scores(macro_data, cot_data, ff_history=ff_history, te_history=te_history, investing_mpmi=investing_mpmi, investing_spmi=investing_spmi, abs_au_mhsi=abs_au_mhsi, investing_cpi=investing_cpi, investing_ppi=investing_ppi, investing_gdp=investing_gdp, myfxbook_ppi=myfxbook_ppi, investing_cc=investing_cc, investing_jolts=investing_jolts, investing_adp=investing_adp, investing_pce=investing_pce, investing_retail_sales=investing_retail_sales, rates_outlook=rates_outlook, investing_core=investing_core, treasury_2y=treasury_2y)
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
        investing_pce=investing_pce,
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
    "PCE YoY": 45,   # USD only (Investing Core PCE Price Index YoY), monthly
}
_QUARTERLY_CPI_CCYS = {"AUD", "NZD"}
_MAX_AGE_CPI_QUARTERLY = 110

# CHF sPMI comes from TE Swiss Services PMI, which is dated by REFERENCE MONTH,
# not release date like the Investing/BusinessNZ sources. The Swiss reading
# publishes ~35 days after the reference month starts, so a current CHF value
# reads ~35 days "older" than its release-dated peers and trips the 40-day sPMI
# window even when fresh. Widen it for CHF only: 40 (intended since-release) + 35
# (reference-to-release lag). A genuinely missed release (90+ days) still flags.
_SPMI_REFERENCE_MONTH_CCYS = {"CHF"}
_MAX_AGE_SPMI_REFMONTH = 75


def _compute_data_staleness(cot_data, investing_cpi, investing_ppi,
                            investing_mpmi, investing_spmi, as_of_date,
                            investing_cc=None, investing_jolts=None,
                            investing_adp=None, investing_pce=None,
                            myfxbook_ppi=None,
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

    # sPMI: monthly for all 8. CHF is reference-month-dated (see constants above)
    # and gets a wider window so a current reading isn't false-flagged.
    for ccy, reading in (investing_spmi or {}).items():
        max_age = _MAX_AGE_SPMI_REFMONTH if ccy in _SPMI_REFERENCE_MONTH_CCYS else _MAX_AGE_DAYS["sPMI"]
        _check("sPMI", ccy, (reading or {}).get("date"), max_age)

    # Consumer Confidence (Investing): USD only, monthly.
    for ccy, reading in (investing_cc or {}).items():
        _check("Consumer Confidence", ccy, (reading or {}).get("date"), _MAX_AGE_DAYS["Consumer Confidence"])

    # JOLTS (Investing): USD only, monthly with ~6-week publication lag.
    for ccy, reading in (investing_jolts or {}).items():
        _check("JOLTS", ccy, (reading or {}).get("date"), _MAX_AGE_DAYS["JOLTS"])

    # ADP (Investing): USD only, monthly.
    for ccy, reading in (investing_adp or {}).items():
        _check("ADP", ccy, (reading or {}).get("date"), _MAX_AGE_DAYS["ADP"])

    # PCE YoY (Investing): USD only, monthly.
    for ccy, reading in (investing_pce or {}).items():
        _check("PCE YoY", ccy, (reading or {}).get("date"), _MAX_AGE_DAYS["PCE YoY"])

    # PPI YoY (Myfxbook): CHF only, monthly.
    for ccy, reading in (myfxbook_ppi or {}).items():
        _check("PPI YoY", ccy, (reading or {}).get("date"), _MAX_AGE_DAYS["mPMI"])

    # Retail Sales (Investing): CAD only, monthly.
    for ccy, reading in (investing_retail_sales or {}).items():
        _check("Retail Sales", ccy, (reading or {}).get("date"), _MAX_AGE_DAYS["mPMI"])

    return out
