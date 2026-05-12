"""
EdgeFinder-exact surprise scoring.

Pure binary methodology: compare latest Actual to Forecast.
- Actual > Forecast -> +1 (beat expectations)
- Actual < Forecast -> -1 (missed expectations)
- Actual == Forecast -> 0 (matched expectations)

Then flip sign for "down_is_bullish" indicators (unemployment, jobless claims)
where a higher actual value is BAD for the currency.

No z-score normalization, no impact weighting. EdgeFinder doesn't use those.
"""
from __future__ import annotations


def momentum_score(
    releases: list[dict],
    direction: str = "up_is_bullish",
) -> int | None:
    """
    EdgeFinder's PMI methodology: change from PREVIOUS to LATEST.
    Not surprise-based (no forecast involved).

    Latest > Previous -> +1 (positive growth, bullish for currency)
    Latest < Previous -> -1 (negative growth, bearish for currency)
    Latest == Previous -> 0

    Used for Manufacturing PMI, Services PMI where EF documents the
    change-from-previous methodology specifically.

    Iterates through releases (sorted newest-first) to find the latest one
    with BOTH actual and previous filled in. Skips future-scheduled releases
    that have previous but no actual yet.
    """
    if not releases:
        return None

    actual = previous = None
    for r in releases:
        a = r.get("actual")
        p = r.get("previous")
        if a is not None and p is not None:
            actual = a
            previous = p
            break

    if actual is None or previous is None:
        return None

    if actual > previous:
        score = 1
    elif actual < previous:
        score = -1
    else:
        score = 0

    if direction == "down_is_bullish":
        score = -score
    return score


def surprise_score(
    releases: list[dict],
    direction: str = "up_is_bullish",
) -> int | None:
    """
    releases: list of release dicts (newest first), each with 'actual' and
    'forecast' keys.
    Returns -1 / 0 / +1 for one currency/indicator, or None if no usable data.
    """
    if not releases:
        return None

    latest = releases[0]
    actual = latest.get("actual")
    forecast = latest.get("forecast")
    if actual is None or forecast is None:
        return None

    if actual > forecast:
        score = 1
    elif actual < forecast:
        score = -1
    else:
        score = 0

    if direction == "down_is_bullish":
        score = -score
    return score
