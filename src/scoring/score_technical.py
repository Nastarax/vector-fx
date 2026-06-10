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
    if df_daily is None or df_daily.empty:
        return 0
    # Drop yfinance's NaN partial-day bars: a NaN last close makes the SMAs
    # NaN, every comparison False, and the score a hardcoded -2.
    closes = df_daily["Close"].dropna()
    if len(closes) < 15:
        return 0
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


def range_position(df_daily: pd.DataFrame, lookback: int = 40) -> int | None:
    """
    Where the last close sits inside the high-low range of the last `lookback`
    daily bars, as 0..100. 0 = at the range low, 100 = at the range high.
    Used for the heatmap Location column (supply/demand entry confluence):
    a bullish-bias pair near the bottom of its range is pulling back into
    territory where demand zones live; near the top it is extended.
    """
    if df_daily is None or df_daily.empty:
        return None
    # yfinance sometimes appends a partial current-day bar with NaN OHLC on
    # crosses; NaN poisons the min/max clamp into a silent 100, so drop it.
    data = df_daily[["High", "Low", "Close"]].dropna()
    if len(data) < lookback:
        return None
    window = data.tail(lookback)
    hi = float(window["High"].max())
    lo = float(window["Low"].min())
    if hi <= lo:
        return None
    close = float(window["Close"].iloc[-1])
    pct = (close - lo) / (hi - lo) * 100
    return int(round(max(0.0, min(100.0, pct))))


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


def seasonality_score(df: pd.DataFrame, as_of_date: str | None = None,
                      commodity: bool = False) -> int:
    """
    Seasonality scoring.

    FX pairs: combines monthly + weekly tendencies across 1y/5y/10y windows,
    averaged and scaled to -2..+2.

    Commodities/indices (commodity=True): 10-year monthly average only.
    Positive -> +2, negative -> -2. Stronger weighting because seasonal
    tendencies are more pronounced in commodities.
    """
    if df is None or df.empty or len(df) < 252 * 2:
        return 0

    if as_of_date:
        ref_date = pd.Timestamp(as_of_date)
    else:
        ref_date = pd.Timestamp.now(tz=df.index.tz) if df.index.tz is not None else pd.Timestamp.now()

    monthly = df["Close"].resample("ME").last().dropna()
    monthly_rets = monthly.pct_change().dropna()
    same_month = monthly_rets[monthly_rets.index.month == ref_date.month]

    if commodity:
        if len(same_month) < 5:
            return 0
        avg_10y = float(same_month.tail(10).mean())
        return 2 if avg_10y > 0 else -2

    # FX: full 6-component scoring
    weekly = df["Close"].resample("W").last().dropna()
    weekly_rets = weekly.pct_change().dropna()
    current_week = ref_date.isocalendar().week if hasattr(ref_date, "isocalendar") else ref_date.week
    weekly_isoweek = weekly_rets.index.isocalendar().week
    same_week = weekly_rets[weekly_isoweek == current_week]

    sub_scores: list[int] = []

    if len(same_month) >= 1:
        sub_scores.append(_sign_bucket(float(same_month.tail(1).mean())))
    if len(same_month) >= 3:
        sub_scores.append(_sign_bucket(float(same_month.tail(5).mean())))
    if len(same_month) >= 5:
        sub_scores.append(_sign_bucket(float(same_month.tail(10).mean())))

    if len(same_week) >= 1:
        sub_scores.append(_sign_bucket(float(same_week.tail(1).mean())))
    if len(same_week) >= 3:
        sub_scores.append(_sign_bucket(float(same_week.tail(5).mean())))
    if len(same_week) >= 5:
        sub_scores.append(_sign_bucket(float(same_week.tail(10).mean())))

    if not sub_scores:
        return 0

    avg = sum(sub_scores) / len(sub_scores)
    scaled = avg * 2
    if scaled > 0:
        return min(2, int(scaled + 0.5))
    if scaled < 0:
        return max(-2, int(scaled - 0.5))
    return 0
