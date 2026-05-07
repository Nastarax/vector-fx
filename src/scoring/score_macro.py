"""
Convert raw FRED observations into -2..+2 scores per (currency, indicator).

Methodology (intentionally simple, matches the spirit of EdgeFinder's
'Surprise Meter' even though we don't have forecasts on free tier):

Score = momentum of latest reading vs prior, normalized by std dev of recent
percent changes. Then bucketed into [-2, -1, 0, +1, +2].

For YoY-style series we compare to 12 months ago. For high-frequency series
(weekly claims, daily rates) we compare to ~6 periods ago. lookback_periods
is configurable in indicators.yaml.
"""
from __future__ import annotations

from statistics import mean, pstdev
from typing import Optional

from src.fetchers.fred import FredObservation


def _pct_change(series: list[float], periods: int) -> Optional[float]:
    """Pct change from `periods` ago to latest."""
    if len(series) < periods + 1:
        return None
    latest = series[0]
    prior = series[periods]
    if prior == 0:
        return None
    return 100 * (latest - prior) / abs(prior)


def _z_bucket(change: float, history: list[float]) -> int:
    """
    Bucket a single % change relative to history of similar changes.
    Returns -2..+2.
    """
    if not history:
        # Without history, fall back to simple sign
        if change > 0.5:
            return 2
        if change > 0:
            return 1
        if change < -0.5:
            return -2
        if change < 0:
            return -1
        return 0

    mu = mean(history)
    sigma = pstdev(history) if len(history) > 1 else 0
    if sigma == 0:
        if change > mu:
            return 1
        if change < mu:
            return -1
        return 0
    z = (change - mu) / sigma
    if z >= 1.0:
        return 2
    if z >= 0.25:
        return 1
    if z <= -1.0:
        return -2
    if z <= -0.25:
        return -1
    return 0


def score_indicator(
    obs: list[FredObservation],
    direction: str = "up_is_bullish",
    lookback_periods: int = 12,
) -> int:
    """
    Returns -2..+2 score for one currency/indicator.
    direction: 'up_is_bullish' or 'down_is_bullish'.
    """
    if not obs or len(obs) < lookback_periods + 2:
        return 0

    values = [o.value for o in obs]  # newest first
    # Latest pct change vs `lookback_periods` ago
    change = _pct_change(values, lookback_periods)
    if change is None:
        return 0

    # History of similar same-period-ago changes for normalization
    history = []
    max_hist = min(24, len(values) - lookback_periods - 1)
    for i in range(1, max_hist):
        ch = _pct_change(values[i:], lookback_periods)
        if ch is not None:
            history.append(ch)

    score = _z_bucket(change, history)
    if direction == "down_is_bullish":
        score = -score
    return score
