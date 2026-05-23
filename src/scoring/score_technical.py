"""
Technical scoring: trend (SMA crossover + slope) and seasonality.
"""
from __future__ import annotations

import pandas as pd


def trend_score(df_daily: pd.DataFrame, df_4h: pd.DataFrame | None = None) -> int:
    """
    SMA(3) vs SMA(14) crossover + SMA(14) slope.

    1. Slope of 14-day SMA: upward -> +1, downward/flat -> -1
    2. Crossover: 3-day above 14-day -> +2, below -> -2
    3. If crossover bullish but slope bearish: +2 - 1 = +1
       If crossover bearish but slope bullish: -2 + 1 = -1
       Otherwise: crossover value unchanged (+2 or -2)

    Possible scores: -2, -1, +1, +2.
    """
    if df_daily is None or df_daily.empty or len(df_daily) < 15:
        return 0
    closes = df_daily["Close"]
    sma3 = float(closes.rolling(3).mean().iloc[-1])
    sma14 = float(closes.rolling(14).mean().iloc[-1])
    sma14_prev = float(closes.rolling(14).mean().iloc[-2])

    slope = 1 if sma14 > sma14_prev else -1
    crossover = 2 if sma3 > sma14 else -2

    if crossover > 0 and slope < 0:
        return crossover - 1
    if crossover < 0 and slope > 0:
        return crossover + 1
    return crossover


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
