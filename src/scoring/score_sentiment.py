"""
Sentiment scoring: COT (institutional, momentum) and Crowd (retail, contrarian).
"""
from __future__ import annotations

from src.fetchers.cot import CotReading
from src.fetchers.retail import RetailReading


def cot_score(reading: CotReading | None) -> int:
    """
    EdgeFinder methodology, exact match.

    1. Net Positioning (+1 / -1):
       +1 if Long contracts > Short contracts
       -1 if Short contracts > Long contracts

    2. Net % Change (+1 / -1 / 0):
       Long% this week MINUS Long% last week
       +1 if positive
       -1 if negative
       0 if exactly zero

    Total range: -2..+2.
    """
    if reading is None:
        return 0

    # Net Positioning: binary, no neutral zone
    pos_score = 1 if reading.long_contracts > reading.short_contracts else -1

    # Net % Change: based on change in Long%, not normalized net change
    if reading.long_pct_change > 0:
        chg_score = 1
    elif reading.long_pct_change < 0:
        chg_score = -1
    else:
        chg_score = 0

    return pos_score + chg_score


def retail_score(reading: RetailReading | None) -> int:
    """
    EdgeFinder methodology, exact match.

    Strict contrarian logic on retail broker Long%:
      Long% >= 60%  -> -1 (crowd heavily long = contrarian bearish)
      40% < Long% < 60%  -> 0 (mixed positioning)
      Long% <= 40%  -> +1 (crowd heavily short = contrarian bullish)

    Score range: -1 / 0 / +1.

    Note: EF combines retail broker data (OANDA + Myfxbook + others) plus
    AAII Investor Sentiment + Put/Call Ratio for indices/stocks. For pure
    FX pairs, only retail broker positioning applies (AAII and Put/Call
    are equity-market signals).
    """
    if reading is None:
        return 0
    longp = reading.long_pct
    if longp >= 60:
        return -1
    if longp <= 40:
        return 1
    return 0
