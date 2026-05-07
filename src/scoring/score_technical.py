"""
Technical scoring: trend on Daily + 4H, plus seasonality.

EdgeFinder's "4H / Daily Chart Trend" combines both timeframes into one cell.
We score each separately with the same logic, then average and round to int
in the -2..+2 range.
"""
from __future__ import annotations

import pandas as pd


def _trend_on_df(df: pd.DataFrame, min_bars: int = 200) -> int:
    """
    Score -2..+2 based on price vs SMA20/50/200.
    +2: price above all 3 SMAs and SMA20 > SMA50 > SMA200 (full bull alignment)
    +1: price above majority of SMAs
     0: mixed
    -1: price below majority of SMAs
    -2: price below all 3 and bear alignment
    """
    if df is None or df.empty or len(df) < min_bars:
        return 0
    closes = df["Close"]
    price = float(closes.iloc[-1])
    sma20 = float(closes.rolling(20).mean().iloc[-1])
    sma50 = float(closes.rolling(50).mean().iloc[-1])
    sma200 = float(closes.rolling(200).mean().iloc[-1])

    above = sum(1 for s in (sma20, sma50, sma200) if price > s)
    bull_align = sma20 > sma50 > sma200
    bear_align = sma20 < sma50 < sma200

    if above == 3 and bull_align:
        return 2
    if above >= 2:
        return 1
    if above == 0 and bear_align:
        return -2
    if above <= 1:
        return -1
    return 0


def trend_score(df_daily: pd.DataFrame, df_4h: pd.DataFrame | None = None) -> int:
    """
    Combined 4H + Daily trend score.
    If 4H is unavailable, falls back to Daily-only.
    Result clamped to -2..+2.
    """
    daily = _trend_on_df(df_daily, min_bars=200)
    if df_4h is None or df_4h.empty:
        return daily
    four_h = _trend_on_df(df_4h, min_bars=200)
    # Average the two timeframes; round to nearest int with .5 going away from 0
    avg = (daily + four_h) / 2
    if avg > 0:
        score = int(avg + 0.5)
    elif avg < 0:
        score = int(avg - 0.5)
    else:
        score = 0
    return max(-2, min(2, score))


def seasonality_score(df: pd.DataFrame, as_of_date: str | None = None) -> int:
    """
    Score -2..+2 based on average return for the current calendar month
    over the last ~10 years.
    If as_of_date is provided, scores seasonality for the month of that date.

    Thresholds tuned to give signal even on small monthly biases (matching
    EdgeFinder's behavior of rarely showing 0 for seasonality).
    """
    if df is None or df.empty or len(df) < 252 * 5:
        return 0
    monthly = df["Close"].resample("ME").last().dropna()
    rets = monthly.pct_change().dropna()
    rets = rets.tail(120)  # last 10 years
    if rets.empty:
        return 0
    if as_of_date:
        current_month = pd.Timestamp(as_of_date).month
    else:
        current_month = pd.Timestamp.now(tz=monthly.index.tz).month
    same_month = rets[rets.index.month == current_month]
    if same_month.empty:
        return 0
    avg = same_month.mean()
    # Tighter bands so meaningful historical biases register
    if avg > 0.010:
        return 2
    if avg > 0.001:
        return 1
    if avg < -0.010:
        return -2
    if avg < -0.001:
        return -1
    return 0
