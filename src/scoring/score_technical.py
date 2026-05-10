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


def _sign_bucket(avg: float) -> int:
    """
    EdgeFinder uses a tight neutral band: avg between -0.01% and +0.01% = 0.
    Anything else gets +1 (bullish) or -1 (bearish) based on sign.
    """
    if avg > 0.0001:
        return 1
    if avg < -0.0001:
        return -1
    return 0


def seasonality_score(df: pd.DataFrame, as_of_date: str | None = None) -> int:
    """
    EdgeFinder seasonality methodology.

    Combines BOTH monthly and weekly tendencies across 1y, 5y, 10y windows:
      - Monthly: avg return for current calendar month over 1y / 5y / 10y
      - Weekly:  avg return for current ISO week-of-year over 1y / 5y / 10y

    Each of the 6 sub-scores gets +1 (bullish), -1 (bearish), or 0 (neutral
    band of -0.01% to +0.01%). Sub-scores are averaged and scaled to -2..+2.
    """
    if df is None or df.empty or len(df) < 252 * 2:
        return 0

    if as_of_date:
        ref_date = pd.Timestamp(as_of_date)
    else:
        ref_date = pd.Timestamp.now(tz=df.index.tz) if df.index.tz is not None else pd.Timestamp.now()

    # MONTHLY: same calendar month historically
    monthly = df["Close"].resample("ME").last().dropna()
    monthly_rets = monthly.pct_change().dropna()
    same_month = monthly_rets[monthly_rets.index.month == ref_date.month]

    # WEEKLY: same ISO week-of-year historically
    weekly = df["Close"].resample("W").last().dropna()
    weekly_rets = weekly.pct_change().dropna()
    current_week = ref_date.isocalendar().week if hasattr(ref_date, "isocalendar") else ref_date.week
    weekly_isoweek = weekly_rets.index.isocalendar().week
    same_week = weekly_rets[weekly_isoweek == current_week]

    sub_scores: list[int] = []

    # Monthly 1y, 5y, 10y
    if len(same_month) >= 1:
        sub_scores.append(_sign_bucket(float(same_month.tail(1).mean())))
    if len(same_month) >= 3:
        sub_scores.append(_sign_bucket(float(same_month.tail(5).mean())))
    if len(same_month) >= 5:
        sub_scores.append(_sign_bucket(float(same_month.tail(10).mean())))

    # Weekly 1y, 5y, 10y
    if len(same_week) >= 1:
        sub_scores.append(_sign_bucket(float(same_week.tail(1).mean())))
    if len(same_week) >= 3:
        sub_scores.append(_sign_bucket(float(same_week.tail(5).mean())))
    if len(same_week) >= 5:
        sub_scores.append(_sign_bucket(float(same_week.tail(10).mean())))

    if not sub_scores:
        return 0

    # Average -1..+1, scale to -2..+2, round half-away-from-zero
    avg = sum(sub_scores) / len(sub_scores)
    scaled = avg * 2
    if scaled > 0:
        return min(2, int(scaled + 0.5))
    if scaled < 0:
        return max(-2, int(scaled - 0.5))
    return 0
