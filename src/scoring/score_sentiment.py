"""
Sentiment scoring: COT (institutional, momentum) and Crowd (retail, contrarian).
"""
from __future__ import annotations

from src.fetchers.cot import CotReading
from src.fetchers.retail import RetailReading


def cot_score(reading: CotReading | None) -> int:
    """
    EdgeFinder methodology for COT (per-currency component).

    Score = sign of weekly change in Long%:
      +1 if Long% (current) - Long% (prev week) > 0   (institutions buying)
      -1 if Long% (current) - Long% (prev week) < 0   (institutions selling)
       0 if no change

    Long% = long_contracts / (long_contracts + short_contracts).

    The pair-level COT cell is then base_score - quote_score (handled in
    build_pair_rows), clamped to -2..+2. This automatically inverts the
    quote currency's signal exactly as EdgeFinder specifies: a positive
    week-over-week change for the quote currency reduces the pair COT by 1.
    """
    if reading is None:
        return 0
    if reading.long_pct_change > 0:
        return 1
    if reading.long_pct_change < 0:
        return -1
    return 0


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
