"""
Technical scoring: trend (SMA crossover + slope) and seasonality.
"""
from __future__ import annotations

import pandas as pd


def _ma_alignment(closes: pd.Series, windows: tuple[int, ...]) -> tuple[int, int]:
    """Count how many of the given SMA windows the latest close sits above vs
    below. Windows with insufficient history are skipped. Returns (above, below)."""
    above = below = 0
    if closes.empty:
        return 0, 0
    px = float(closes.iloc[-1])
    for w in windows:
        if len(closes) < w:
            continue
        sma = float(closes.rolling(w).mean().iloc[-1])
        if px > sma:
            above += 1
        elif px < sma:
            below += 1
    return above, below


def _trend_ma_alignment(df_daily: pd.DataFrame, df_4h: pd.DataFrame | None) -> int:
    """Equity-index trend: where price sits relative to SMA20/50/200 on the Daily
    chart and SMA20/50/200 on the 4H chart. Each available SMA on each timeframe
    casts one above/below vote; the pooled vote fraction maps to -2..+2:

        ratio = (#above - #below) / #votes
        ratio >=  0.5 -> +2,  > 0 -> +1,  == 0 -> 0,  < 0 -> -1,  <= -0.5 -> -2

    Slow MAs keep a single sharp day from flipping the trend, and the 4H chart
    actually contributes - the method EdgeFinder uses on its equity index/stock
    cards (NASDAQ, DOW). yfinance NaN partial-day bars are dropped first.
    """
    above = below = 0
    for df in (df_daily, df_4h):
        if df is None or df.empty:
            continue
        a, b = _ma_alignment(df["Close"].dropna(), (20, 50, 200))
        above += a
        below += b
    votes = above + below
    if votes == 0:
        return 0
    ratio = (above - below) / votes
    if ratio >= 0.5:
        return 2
    if ratio > 0:
        return 1
    if ratio <= -0.5:
        return -2
    if ratio < 0:
        return -1
    return 0


def trend_score(df_daily: pd.DataFrame, df_4h: pd.DataFrame | None = None,
                equity_index: bool = False) -> int:
    """
    "4H / Daily Chart Trend" cell, scored by asset class:

    - FX pairs AND metals (equity_index=False): EdgeFinder's published method -
      SMA(3) vs SMA(14) crossover (+-2) plus the 14-day SMA slope (+-1). Bullish
      crossover with a falling SMA -> +1; bearish crossover with a rising SMA ->
      -1. Possible scores: -2, -1, +1, +2. Verified vs EF: FX pairs and Gold/
      Silver match this; the longer SMA20/50/200 method gives -1/0 where EF shows
      -2 for those, so metals stay on the crossover (NOT the index method).
    - Equity indices (equity_index=True, i.e. NDX/NKY): SMA20/50/200 price
      alignment pooled across the Daily and 4H charts (see _trend_ma_alignment).
      This is what EdgeFinder's index cards use; the short crossover whipsaws on a
      single sharp day and read NASDAQ bearish at all-time highs.

    yfinance NaN partial-day bars are dropped first either way: a NaN last close
    makes the SMAs NaN, every comparison False, and the score a hardcoded -2.
    """
    if equity_index:
        return _trend_ma_alignment(df_daily, df_4h)

    if df_daily is None or df_daily.empty:
        return 0
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
