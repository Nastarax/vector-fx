"""
Quick utility: test every FRED series ID in fred_series.yaml and report
which ones work / fail. Run anytime after editing the YAML.

Usage: python scripts/test_fred.py
"""
import os
import sys
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")
API_KEY = os.getenv("FRED_API_KEY")
if not API_KEY:
    print("Set FRED_API_KEY in .env first.")
    sys.exit(1)

with open(ROOT / "config" / "fred_series.yaml") as f:
    series_map = yaml.safe_load(f)

ok = 0
bad = []
skipped = 0

for ccy, indicators in series_map.items():
    for ind, series_id in indicators.items():
        if not series_id:
            skipped += 1
            continue
        url = "https://api.stlouisfed.org/fred/series/observations"
        params = {"series_id": series_id, "api_key": API_KEY, "file_type": "json", "limit": 1}
        try:
            r = requests.get(url, params=params, timeout=10)
            if r.status_code == 200:
                ok += 1
                print(f"  OK   {ccy:4s} {ind:20s} {series_id}")
            else:
                bad.append((ccy, ind, series_id, r.status_code))
                print(f"  FAIL {ccy:4s} {ind:20s} {series_id}  HTTP {r.status_code}")
        except Exception as e:
            bad.append((ccy, ind, series_id, str(e)))
            print(f"  ERR  {ccy:4s} {ind:20s} {series_id}  {e}")

print(f"\nSummary: {ok} OK, {len(bad)} failed, {skipped} skipped (null)")
if bad:
    print("\nFix or null these in fred_series.yaml:")
    for ccy, ind, sid, code in bad:
        print(f"  {ccy}/{ind}: {sid} ({code})")
