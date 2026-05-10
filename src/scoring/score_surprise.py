"""
Surprise-based scoring (Actual vs Forecast).
This is what EdgeFinder uses. With ForexFactory data we can finally apply it.

Score logic:
- surprise = (actual - forecast) / |forecast|  (signed, normalized)
- Compute z-score of latest surprise vs trailing surprises for same indicator+country
- Apply impact weight: high-impact releases score more strongly than low-impact
- Bucket into -2..+2

Returns None when:
- No FF data exists for this indicator+country
- Latest release doesn't have both actual and forecast
"""
from __future__ import annotations

from statistics import mean, pstdev


# Impact weights: high-impact releases (BoJ decision, NFP, CPI) drive markets
# way more than low-impact ones (manufacturing surveys, etc.). Multiplying the
# z-score by the impact weight makes high-impact surprises hit -2/+2 buckets
# more easily, matching how markets actually price these events.
IMPACT_WEIGHTS = {
    "high": 1.5,
    "medium": 1.0,
    "low": 0.5,
}


def surprise_score(
    releases: list[dict],
    direction: str = "up_is_bullish",
) -> int | None:
    """
    releases: list of release dicts (newest first), each with 'surprise' and
    'impact' keys.
    Returns -2..+2 score for one currency/indicator, or None if no data.
    """
    if not releases:
        return None

    latest = releases[0]
    latest_s = latest.get("surprise")
    if latest_s is None:
        return None

    # Impact weight from latest release's classification (high/medium/low)
    impact = latest.get("impact", "medium")
    weight = IMPACT_WEIGHTS.get(impact, 1.0)

    # Build history of prior surprises for normalization
    history = []
    for r in releases[1:25]:
        s = r.get("surprise")
        if s is not None:
            history.append(s)

    if len(history) < 4:
        # Not enough history; weighted sign-based scoring
        weighted_s = latest_s * weight
        if weighted_s > 0.05:
            score = 2
        elif weighted_s > 0:
            score = 1
        elif weighted_s < -0.05:
            score = -2
        elif weighted_s < 0:
            score = -1
        else:
            score = 0
    else:
        mu = mean(history)
        sigma = pstdev(history) if len(history) > 1 else 0
        if sigma == 0:
            score = 1 if latest_s > mu else (-1 if latest_s < mu else 0)
        else:
            # Z-score then apply impact weight
            z = ((latest_s - mu) / sigma) * weight
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
