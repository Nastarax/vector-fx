"""
Sentiment scoring: COT (institutional, momentum) and Crowd (retail, contrarian).
"""
from __future__ import annotations

from src.fetchers.cot import CotReading
from src.fetchers.retail import RetailReading


def cot_score(reading: CotReading | None) -> int:
    """
    EdgeFinder methodology for COT (per-currency component). Combines two
    independent ±1 signals into a per-currency score of -2..+2:

      1. Weekly Positioning Change:
           +1 if Long% (current) - Long% (prev week) > 0
           -1 otherwise

      2. Overall Net Positioning:
           +1 if long_contracts > short_contracts
           -1 otherwise

    Per-currency range: -2 (both bearish) to +2 (both bullish).

    The pair-level COT cell is then base_score - quote_score (handled in
    build_pair_rows), clamped to -2..+2.
    """
    if reading is None:
        return 0
    # Component 1: weekly change in Long%
    change_score = 1 if reading.long_pct_change > 0 else -1
    # Component 2: overall net positioning
    net_score = 1 if reading.long_contracts > reading.short_contracts else -1
    return change_score + net_score


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
