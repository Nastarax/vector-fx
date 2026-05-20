"""
Sentiment scoring: COT (institutional, momentum) and Crowd (retail, contrarian).
"""
from __future__ import annotations

from src.fetchers.cot import CotReading
from src.fetchers.retail import RetailReading


def cot_score(reading: CotReading | None, neutral_threshold: float = 0.0) -> int:
    """
    EdgeFinder COT methodology (per-currency), single component.

    A1 Trading's EdgeFinder scores COT purely on the week-over-week change in
    institutional net long %:

        Net Change = Current Long% - Previous Long%

        +1  if Net Change >  neutral_threshold   (institutions adding longs /
                                                   closing shorts -> bullish)
        -1  if Net Change < -neutral_threshold   (institutions selling /
                                                   closing longs -> bearish)
         0  if |Net Change| <= neutral_threshold (positioning ~unchanged)

    Per-currency range: -1 / 0 / +1.

    NOTE: an earlier version added a second "overall net positioning"
    component (long_contracts vs short_contracts). That is NOT part of the
    real A1 methodology and was removed. COT depends only on the weekly
    *change* in positioning, not the absolute long/short balance.

    `neutral_threshold` is the deadband (in percentage points of Long%) for
    treating a week as "relatively unchanged". Default 0.0 = strict sign.
    Raise it (e.g. 0.5) to ignore tiny week-over-week wobble.

    The pair-level COT cell is base_score - quote_score (handled in
    build_pair_rows), clamped to -2..+2.
    """
    if reading is None:
        return 0
    change = reading.long_pct_change
    if change > neutral_threshold:
        return 1
    if change < -neutral_threshold:
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
