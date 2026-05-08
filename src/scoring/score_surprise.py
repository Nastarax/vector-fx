"""
Surprise-based scoring (Actual vs Forecast).
This is what EdgeFinder uses. With ForexFactory data we can finally apply it.

Score logic:
- surprise = (actual - forecast) / |forecast|  (signed, normalized)
- Compute z-score of latest surprise vs trailing surprises for same indicator+country
- Bucket into -2..+2

Returns None when:
- No FF data exists for this indicator+country
- Latest release doesn't have both actual and forecast
"""
from __future__ import annotations

from statistics import mean, pstdev


def surprise_score(
    releases: list[dict],
    direction: str = "up_is_bullish",
) -> int | None:
    """
    releases: list of release dicts (newest first), each with 'surprise' key.
    Returns -2..+2 score for one currency/indicator, or None if no data.
    """
    if not releases:
        return None

    # Latest release's surprise
    latest = releases[0]
    latest_s = latest.get("surprise")
    if latest_s is None:
        return None  # no actual or forecast

    # Build history of surprises
    history = []
    for r in releases[1:25]:  # last ~25 prior releases for normalization
        s = r.get("surprise")
        if s is not None:
            history.append(s)

    if len(history) < 4:
        # Not enough history; use simple sign-based scoring
        if latest_s > 0.05:
            score = 2
        elif latest_s > 0:
            score = 1
        elif latest_s < -0.05:
            score = -2
        elif latest_s < 0:
            score = -1
        else:
            score = 0
    else:
        mu = mean(history)
        sigma = pstdev(history) if len(history) > 1 else 0
        if sigma == 0:
            score = 1 if latest_s > mu else (-1 if latest_s < mu else 0)
        else:
            z = (latest_s - mu) / sigma
            if z >= 0.75:
                score = 2
            elif z >= 0.2:
                score = 1
            elif z <= -0.75:
                score = -2
            elif z <= -0.2:
                score = -1
            else:
                score = 0

    if direction == "down_is_bullish":
        score = -score
    return score
