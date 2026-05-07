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


def _cache_path(symbol: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"px_{symbol}.pkl"  # pickle: no extra deps required


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


if __name__ == "__main__":
    prices = fetch_prices()
    for s, df in prices.items():
        if df.empty:
            print(f"{s}: NO DATA")
        else:
            print(f"{s}: {len(df)} bars, last close {df['Close'].iloc[-1]:.4f}")
