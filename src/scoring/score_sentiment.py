"""
Sentiment scoring: COT (institutional, momentum) and Crowd (retail, contrarian).
"""
from __future__ import annotations

from src.fetchers.cot import CotReading
from src.fetchers.retail import RetailReading


def cot_score(reading: CotReading | None) -> int:
    """
    EdgeFinder-style: +1 if net positioning bullish, -1 if bearish.
                      +1 if weekly change positive, -1 if negative.
    Total cell range: -2..+2.
    """
    if reading is None:
        return 0
    pos_score = 0
    chg_score = 0
    if reading.long_pct > 55:
        pos_score = 1
    elif reading.long_pct < 45:
        pos_score = -1
    if reading.weekly_change_pct > 1.0:
        chg_score = 1
    elif reading.weekly_change_pct < -1.0:
        chg_score = -1
    return pos_score + chg_score


def retail_score(reading: RetailReading | None) -> int:
    """
    Contrarian: heavy retail long -> bearish for that pair (-2).
    Returns score for the PAIR (not per-currency), since retail data is
    pair-specific.

    Thresholds tuned to be less aggressive at the extremes so we match
    EdgeFinder's distribution better. Only truly extreme positioning
    (>= 82% one-sided) scores ±2.
    """
    if reading is None:
        return 0
    longp = reading.long_pct
    if longp >= 82:
        return -2
    if longp >= 65:
        return -1
    if longp <= 18:
        return 2
    if longp <= 35:
        return 1
    return 0
