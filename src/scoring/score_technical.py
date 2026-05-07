"""
Technical scoring: trend (price vs moving averages) and seasonality.
"""
from __future__ import annotations

import pandas as pd


def trend_score(df: pd.DataFrame) -> int:
    """
    Score -2..+2 based on price vs SMA20/50/200.
    +2: price above all 3 SMAs and SMA20 > SMA50 > SMA200 (full bull alignment)
    +1: price above majority of SMAs
     0: mixed
    -1: price below majority of SMAs
    -2: price below all 3 and bear alignment
    """
    if df is None or df.empty or len(df) < 200:
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


def seasonality_score(df: pd.DataFrame) -> int:
    """
    Score -2..+2 based on average return for the current calendar month
    over the last ~10 years.
    """
    if df is None or df.empty or len(df) < 252 * 5:
        return 0
    monthly = df["Close"].resample("ME").last().dropna()
    rets = monthly.pct_change().dropna()
    rets = rets.tail(120)  # last 10 years
    if rets.empty:
        return 0
    current_month = pd.Timestamp.now(tz=monthly.index.tz).month
    same_month = rets[rets.index.month == current_month]
    if same_month.empty:
        return 0
    avg = same_month.mean()
    # Bucket by absolute average monthly return
    if avg > 0.015:
        return 2
    if avg > 0.003:
        return 1
    if avg < -0.015:
        return -2
    if avg < -0.003:
        return -1
    return 0
