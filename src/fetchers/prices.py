"""
yfinance fetcher for FX OHLC.
Pulls daily candles for the last ~25 years to support trend + seasonality scoring.
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


def fetch_prices() -> dict[str, pd.DataFrame]:
    """
    Returns dict[symbol] -> DataFrame indexed by date with columns Open/High/Low/Close.
    """
    with open(CONFIG_DIR / "pairs.yaml") as f:
        cfg = yaml.safe_load(f)

    out: dict[str, pd.DataFrame] = {}
    for p in cfg["pairs"]:
        sym = p["symbol"]
        ticker = p["yf_ticker"]
        cache = _cache_path(sym)
        if _is_fresh(cache):
            df = pd.read_pickle(cache)
        else:
            try:
                df = yf.Ticker(ticker).history(period="25y", interval="1d", auto_adjust=False)
                if df.empty:
                    raise RuntimeError("empty dataframe from yfinance")
                df = df[["Open", "High", "Low", "Close"]].copy()
                df.to_pickle(cache)
            except Exception as e:
                print(f"[prices] {sym} ({ticker}) failed: {e}")
                df = pd.DataFrame()
        out[sym] = df
        time.sleep(0.1)
    return out


def fetch_prices_4h() -> dict[str, pd.DataFrame]:
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
        if _is_fresh(cache):
            df = pd.read_pickle(cache)
        else:
            try:
                # Pull 90 days of hourly bars; enough for SMA200 on 4H
                df_h = yf.Ticker(ticker).history(period="90d", interval="60m", auto_adjust=False)
                if df_h.empty:
                    raise RuntimeError("empty hourly dataframe")
                df = df_h[["Open", "High", "Low", "Close"]].resample("4h").agg({
                    "Open": "first",
                    "High": "max",
                    "Low": "min",
                    "Close": "last",
                }).dropna()
                df.to_pickle(cache)
            except Exception as e:
                print(f"[prices_4h] {sym} ({ticker}) failed: {e}")
                df = pd.DataFrame()
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
