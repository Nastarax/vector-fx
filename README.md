# EdgeFinder Clone

Self-hosted rebuild of A1 Trading's EdgeFinder. Aggregates macro, sentiment, and technical data into a Top Setups heatmap that ranks 13 FX pairs from Very Bullish to Very Bearish.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env, paste your free FRED API key
python main.py
open data/output.html
```

## What's inside

```
config/
  pairs.yaml        13 FX pairs and their tickers
  indicators.yaml   scoring rules per category
  fred_series.yaml  FRED series IDs per currency
src/
  fetchers/         pull data (FRED, COT, retail, prices)
  scoring/          turn data into -2..+2 cells, sum into pair score
  output/           render HTML heatmap
data/cache/         JSON cache of fetched data
.github/workflows/  hourly cron via GitHub Actions
```

## Scoring methodology

Each cell scores -2 to +2 for one currency on one indicator. Pair score = base currency cell - quote currency cell, summed across all indicators. Total maps to a bias label using thresholds in `indicators.yaml`.

Refresh cadence:
- Macro indicators: pull on release schedule (FRED data refreshes when the source data publishes)
- COT report: weekly, Friday 3:30 PM ET
- Retail sentiment + technicals: hourly

## Data sources (all free)

- FRED (Federal Reserve Economic Data): macro for 8 currencies
- CFTC Commitment of Traders: institutional positioning
- Myfxbook Community Outlook: retail sentiment
- yfinance: FX OHLC for trend and seasonality
