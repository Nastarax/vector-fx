"""
CFTC Commitment of Traders fetcher.
Pulls the latest 'TFF' (Traders in Financial Futures) report for currency futures.
Free public CSV from CFTC, updated every Friday ~3:30 PM ET for prior Tuesday.
"""
from __future__ import annotations

import io
import json
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import requests

CFTC_TFF_URL = (
    "https://www.cftc.gov/files/dea/history/fut_fin_txt_2026.zip"  # current year
)
# Fallback: 'all years' aggregated. Year-specific is smaller.
CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"

# Map currencies to CFTC market codes / contract names.
# These are the labels that appear in the TFF report.
CFTC_NAMES = {
    "USD": "U.S. DOLLAR INDEX - ICE FUTURES U.S.",
    "EUR": "EURO FX - CHICAGO MERCANTILE EXCHANGE",
    "GBP": "BRITISH POUND - CHICAGO MERCANTILE EXCHANGE",
    "JPY": "JAPANESE YEN - CHICAGO MERCANTILE EXCHANGE",
    "CHF": "SWISS FRANC - CHICAGO MERCANTILE EXCHANGE",
    "AUD": "AUSTRALIAN DOLLAR - CHICAGO MERCANTILE EXCHANGE",
    "CAD": "CANADIAN DOLLAR - CHICAGO MERCANTILE EXCHANGE",
    "NZD": "NZ DOLLAR - CHICAGO MERCANTILE EXCHANGE",
}


@dataclass
class CotReading:
    currency: str
    report_date: str
    long_contracts: int
    short_contracts: int
    net_position: int
    long_pct: float
    weekly_change_pct: float  # week-over-week change in net position, normalized


def _cache_path() -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / "cot_tff.csv"


def _is_fresh(path: Path, max_age_hours: int = 24) -> bool:
    if not path.exists():
        return False
    age = time.time() - path.stat().st_mtime
    return age < max_age_hours * 3600


def _download_tff() -> pd.DataFrame:
    cache = _cache_path()
    if _is_fresh(cache):
        return pd.read_csv(cache, low_memory=False)

    r = requests.get(CFTC_TFF_URL, timeout=30)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        # The TFF zip contains a single .txt file (CSV-formatted)
        name = [n for n in z.namelist() if n.lower().endswith(".txt")][0]
        with z.open(name) as f:
            df = pd.read_csv(f, low_memory=False)
    df.to_csv(cache, index=False)
    return df


def fetch_cot() -> dict[str, CotReading]:
    """
    Returns dict[currency] -> latest CotReading.
    """
    df = _download_tff()
    # Identify the column names dynamically (CFTC files have wordy headers)
    name_col = next((c for c in df.columns if "Market_and_Exchange_Names" in c), None)
    date_col = next((c for c in df.columns if "Report_Date_as_YYYY-MM-DD" in c), None)
    long_col = next((c for c in df.columns if "Asset_Mgr_Positions_Long_All" in c), None)
    short_col = next((c for c in df.columns if "Asset_Mgr_Positions_Short_All" in c), None)
    long_chg_col = next((c for c in df.columns if "Change_in_Asset_Mgr_Long_All" in c), None)
    short_chg_col = next((c for c in df.columns if "Change_in_Asset_Mgr_Short_All" in c), None)

    if not all([name_col, date_col, long_col, short_col]):
        raise RuntimeError("CFTC TFF schema unexpected; check column names.")

    df = df.sort_values(date_col, ascending=False)

    out: dict[str, CotReading] = {}
    for ccy, market in CFTC_NAMES.items():
        sub = df[df[name_col] == market].head(2)  # latest + previous
        if len(sub) == 0:
            continue
        row = sub.iloc[0]
        long_c = int(row[long_col])
        short_c = int(row[short_col])
        net = long_c - short_c
        total = long_c + short_c
        long_pct = 100 * long_c / total if total else 50.0

        # Weekly change in net position normalized by total
        if long_chg_col and short_chg_col:
            net_chg = int(row[long_chg_col]) - int(row[short_chg_col])
        elif len(sub) > 1:
            prev = sub.iloc[1]
            prev_net = int(prev[long_col]) - int(prev[short_col])
            net_chg = net - prev_net
        else:
            net_chg = 0
        chg_pct = 100 * net_chg / total if total else 0.0

        out[ccy] = CotReading(
            currency=ccy,
            report_date=str(row[date_col]),
            long_contracts=long_c,
            short_contracts=short_c,
            net_position=net,
            long_pct=long_pct,
            weekly_change_pct=chg_pct,
        )
    return out


if __name__ == "__main__":
    cot = fetch_cot()
    for ccy, r in cot.items():
        print(f"{ccy}: long%={r.long_pct:.1f}  net={r.net_position}  wkchg%={r.weekly_change_pct:+.2f}  ({r.report_date})")
