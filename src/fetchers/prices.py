"""
yfinance fetcher for FX OHLC.
Pulls daily candles for the last ~25 years to support trend + seasonality scoring.

Resilience strategy:
- Try fresh cache (<1h old) first
- Otherwise hit yfinance with 3 retries + exponential backoff (yfinance flakes
  intermittently, especially on EURUSD=X which Yahoo rate-limits aggressively)
- If all yfinance attempts fail, fall back to STALE cache rather than returning
  an empty DataFrame. A day-old trend score is still useful; no score is not.
"""
from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import yaml
import yfinance as yf

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"
CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def _cache_path(symbol: str, suffix: str = "") -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    name = f"px_{symbol}{suffix}.pkl"
    return CACHE_DIR / name


def _is_fresh(path: Path, max_age_hours: int = 1) -> bool:
    if not path.exists():
        return False
    return (time.time() - path.stat().st_mtime) < max_age_hours * 3600


def _yf_history_with_retries(ticker: str, period: str, interval: str, max_attempts: int = 3) -> pd.DataFrame | None:
    """
    Wrap yf.Ticker(...).history() with retries. yfinance returns None or raises
    a TypeError on rate-limited responses; we retry with exponential backoff.
    Returns the DataFrame on success, None if all attempts fail.
    """
    for attempt in range(max_attempts):
        try:
            df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=False)
            # yfinance can return None or a non-DataFrame on internal errors
            if df is None or not isinstance(df, pd.DataFrame):
                raise RuntimeError("yfinance returned None or non-DataFrame")
            if df.empty:
                raise RuntimeError("empty dataframe from yfinance")
            return df
        except Exception as e:
            if attempt < max_attempts - 1:
                # Exponential backoff: 2s, 4s, 8s
                wait = 2 ** (attempt + 1)
                time.sleep(wait)
            else:
                print(f"[prices] {ticker} all {max_attempts} attempts failed: {e}")
    return None


def fetch_prices(as_of_date: str | None = None) -> dict[str, pd.DataFrame]:
    """
    Returns dict[symbol] -> DataFrame indexed by date with columns Open/High/Low/Close.
    If as_of_date (YYYY-MM-DD), filters each df to bars dated <= that date
    (for historical backtesting).
    """
    with open(CONFIG_DIR / "pairs.yaml") as f:
        cfg = yaml.safe_load(f)

    out: dict[str, pd.DataFrame] = {}
    for p in cfg["pairs"]:
        sym = p["symbol"]
        ticker = p["yf_ticker"]
        cache = _cache_path(sym)

        df: pd.DataFrame | None = None
        if _is_fresh(cache):
            df = pd.read_pickle(cache)
        else:
            df = _yf_history_with_retries(ticker, period="25y", interval="1d")
            if df is not None:
                df = df[["Open", "High", "Low", "Close"]].copy()
                df.to_pickle(cache)
            elif cache.exists():
                # yfinance failed; use stale cache rather than zero data
                stale_age_hours = (time.time() - cache.stat().st_mtime) / 3600
                print(f"[prices] {sym} using stale cache ({stale_age_hours:.1f}h old)")
                df = pd.read_pickle(cache)
            else:
                df = pd.DataFrame()

        if df is None:
            df = pd.DataFrame()
        if as_of_date and not df.empty:
            df = df.loc[df.index <= pd.Timestamp(as_of_date, tz=df.index.tz)]
        out[sym] = df
        time.sleep(0.1)
    return out


def fetch_prices_4h(as_of_date: str | None = None) -> dict[str, pd.DataFrame]:
    """
    Returns dict[symbol] -> DataFrame of 4H bars.
    yfinance doesn't expose 4H directly, so we pull 60m and resample.
    Used for the 4H Trend scoring component.
    """
    with open(CONFIG_DIR / "pairs.yaml") as f:
        cfg = yaml.safe_load(f)

    out: dict[str, pd.DataFrame] = {}
    for p in cfg["pairs"]:
        sym = p["symbol"]
        ticker = p["yf_ticker"]
        cache = _cache_path(sym, suffix="_4h")

        df: pd.DataFrame | None = None
        if _is_fresh(cache):
            df = pd.read_pickle(cache)
        else:
            df_h = _yf_history_with_retries(ticker, period="90d", interval="60m")
            if df_h is not None:
                df = df_h[["Open", "High", "Low", "Close"]].resample("4h").agg({
                    "Open": "first",
                    "High": "max",
                    "Low": "min",
                    "Close": "last",
                }).dropna()
                df.to_pickle(cache)
            elif cache.exists():
                stale_age_hours = (time.time() - cache.stat().st_mtime) / 3600
                print(f"[prices_4h] {sym} using stale cache ({stale_age_hours:.1f}h old)")
                df = pd.read_pickle(cache)
            else:
                df = pd.DataFrame()

        if df is None:
            df = pd.DataFrame()
        if as_of_date and not df.empty:
            df = df.loc[df.index <= pd.Timestamp(as_of_date, tz=df.index.tz)]
        out[sym] = df
        time.sleep(0.1)
    return out


if __name__ == "__main__":
    prices = fetch_prices()
    for s, df in prices.items():
        if df.empty:
            print(f"{s}: NO DATA")
        else:
            print(f"{s}: {len(df)} daily bars, last close {df['Close'].iloc[-1]:.4f}")
    print("\n4H bars:")
    prices_4h = fetch_prices_4h()
    for s, df in prices_4h.items():
        if df.empty:
            print(f"{s}: NO 4H DATA")
        else:
            print(f"{s}: {len(df)} 4H bars, last close {df['Close'].iloc[-1]:.4f}")
