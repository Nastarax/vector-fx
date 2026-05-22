# Vector - FX Macro Heatmap (project handover)

Self-hosted FX swing-trading scoring tool branded **Vector**. Aggregates technical,
sentiment, and fundamental indicators into a -2..+2 score per cell, summed to a
per-pair / per-currency bias (Very Bullish .. Very Bearish). Lives at
`C:\Users\yanae\Desktop\Swing Trading\edgefinder`, pushed to GitHub repo
`Nastarax/edgefinder`, served via GitHub Pages. Dark theme, gradient blue "V" mark.

GitHub Actions runs `main.py` hourly at :05, commits regenerated outputs, GH Pages
deploys. Investing.com and Myfxbook are Cloudflare-blocked on GH Actions, so those
sources are refreshed locally (`scripts/refresh_investing.py`, Windows Task
Scheduler, daily 9:30 AM) and committed.

## Deploy flow

```powershell
python scripts\refresh_investing.py          # full local refresh of Cloudflare-blocked sources
python main.py                               # regenerate all pages
git add -A
git commit -m "..."
git push
```

Template-only changes need just `python main.py`.

### Targeted refresh (added this session)

`refresh_investing.py` now has a target registry. Pass one or more targets to refresh
only those instead of the full (slow) sweep:

```powershell
python scripts\refresh_investing.py chf_ppi          # just CHF PPI
python scripts\refresh_investing.py jolts adp         # just JOLTS + ADP
python scripts\refresh_investing.py cc                # just US Consumer Confidence
```

Targets: `mpmi, spmi, cpi, cpi_history, ppi, cc, jolts, adp, mfx_ppi, cad_retail`.
No args = all, in that order. It prints a tailored `git add` line for only what was
refreshed.

## Data sources by indicator (current)

```
Trend / Seasonality:  yfinance (Daily + 4H)
COT:                  CFTC Socrata API (Legacy Non-Commercial)
Crowd Sentiment:      Myfxbook + Forexbenchmark (averaged)
GDP:                  TE (Actual vs Consensus, fallback TEForecast)
mPMI:                 Investing per-currency, momentum (Actual vs Previous)
sPMI:                 Investing 6 ccy + Investing procure.ch (CHF) + BusinessNZ direct (NZD)
Retail Sales:         CAD = Investing Retail Sales MoM (id 260, Actual vs Forecast);
                      AUD = ABS MHSI; other 6 = TE (Actual vs Consensus)
Consumer Conf:        USD = Investing CB Consumer Confidence (Actual vs Forecast);
                      other 7 = TE momentum (Actual vs Previous)
CPI YoY:              Investing per-currency (Actual vs Forecast); JPY = Investing Tokyo
                      Core CPI (id 328); fallback Previous for CHF
PPI YoY:              CHF = Myfxbook Producer & Import Prices YoY (Actual vs Consensus);
                      AUD = Myfxbook Australia PPI YoY (Actual vs Consensus);
                      GBP = Investing PPI Output (id 730, Actual vs Forecast);
                      NZD = Investing PPI Output (id 247, Actual vs Forecast);
                      other 4 (USD/EUR/JPY/CAD) = TE (Actual vs Consensus,
                      fallback TEForecast).
PCE / NFP / Jobless Claims: TE, US-only
ADP:                  Investing ADP Nonfarm Employment Change (id 1), Actual vs Forecast, US-only
JOLTS:                Investing JOLTS Job Openings (id 1057), Actual vs Forecast, US-only
Interest Rates:       TE rate outlook (TEForecast vs current)
Unemployment Rate:    TE all 8 (down_is_bullish)
```

## Changes made this session (NOT yet committed)

All edits are in the working tree, uncommitted. Suggested commit + push as a batch.

1. **US Consumer Confidence -> Investing CB Consumer Confidence (id 48)**, Actual vs
   Forecast (fallback Previous), USD only. New fetcher
   `src/fetchers/investing_consumer_conf.py` (cache `data/cache/investing_consumer_conf.json`).
   Other 7 currencies stay on TE momentum.
2. **US JOLTS -> Investing JOLTS Job Openings (id 1057)**, Actual vs Forecast, USD only.
   New fetcher `src/fetchers/investing_jolts.py` (cache `data/cache/investing_jolts.json`).
3. **US ADP -> Investing ADP Nonfarm Employment Change (id 1)**, Actual vs Forecast,
   USD only. New fetcher `src/fetchers/investing_adp.py` (cache `data/cache/investing_adp.json`).
4. **CHF PPI -> Myfxbook Switzerland Producer & Import Prices YoY**, Actual vs Consensus
   (fallback Previous), CHF only. New fetcher `src/fetchers/myfxbook_ppi.py`
   (cache `data/cache/myfxbook_ppi.json`). NZD stays on Investing, the other 6 on TE.
5. **Targeted-refresh CLI** in `scripts/refresh_investing.py` (registry + arg parsing).
6. **K/M number formatting** on the Economic Heatmap (`build_economic_heatmap.py`, the
   `abbrevNum`/`fmt` JS) and the Asset Scorecard (`scorecard_template.html`,
   `abbrevNum`/`fmtNum`): >=1M -> "6.87M", >=1k -> "209K", smaller values unchanged. COT
   dashboard left as-is (full numbers) by request.

Wiring for the four new sources was threaded through:
`score_pair.py` (`build_currency_scores`, `build_heatmap`, `_compute_data_staleness`),
`build_economic_heatmap.py` (`_build_row`, `build_all`), and `main.py` (both the live
and backtest branches, plus both builder calls).

## Scoring conventions (per indicator override, in score_pair.py)

- Per-source overrides live in `build_currency_scores` (`src/scoring/score_pair.py`),
  each as an `if ind_id == ... and ccy == ...` block that runs before the generic TE
  fallback, so missing/empty caches degrade gracefully to TE.
- US-only indicators (NFP, ADP, JOLTS, Jobless Claims, PCE) score 0 (neutral) for non-USD
  so USD pairs reflect USD's direction.
- COT score is single-component: week-over-week change in Long% only (+1/0/-1 per
  threshold). Pair COT cell = base - quote, clamped -2..+2. (This was fixed earlier; the
  old two-component version was a bug.)

## Fetcher pattern (Investing / Myfxbook)

Each source is its own module under `src/fetchers/`. Investing fetchers
(`investing_cpi`, `investing_ppi`, `investing_consumer_conf`, `investing_jolts`,
`investing_adp`) share the same shape: a `*_URLS` dict, `_fetch_with_retries` (curl_cffi
Chrome impersonation, plain-requests fallback), a `parse_latest_release` that reads the
"Latest Release  Actual ... Forecast ... Previous" block, `fetch_*()`, `load_cached()`,
and a module-level `_LAST_FRESH` set used by the refresh script's two-pass retry.

**Myfxbook is different** (`myfxbook_ppi.py`): the event page renders the latest release
as labeled `<span>` blocks (`Previous: -2.7% / Consensus: -2.6% / Actual: -2%`), NOT a
table. `_parse_release_block` scopes to the "Latest Release" div and reads the value after
each label; `_parse_table` is a fallback for any table-layout pages. The page does not
carry the latest release date (history table is lazy-loaded), so the date is estimated as
the next-release date (from the add-to-calendar link) minus ~30 days. Myfxbook needs
curl_cffi (Cloudflare); plain requests get blocked.

## Environment notes

- **curl_cffi must be installed locally** for Investing + Myfxbook (Cloudflare TLS
  impersonation). Without it the fetchers fall back to plain requests and get blocked.
- The earlier Cowork sandbox had a mount bug that served stale/truncated copies of
  freshly-edited files (caused false syntax errors and stale imports). This does NOT
  apply when running in Claude Code directly on the filesystem. Still, run `python main.py`
  once to confirm the full pipeline renders cleanly, since that end-to-end run could not be
  done in the Cowork sandbox.
- Confirm `data/cache/` is tracked by git (not gitignored), or the committed caches won't
  reach GH Actions and the hourly run will have nothing to read.

## Pages

- Main heatmap (`data/index.html` via `build_heatmap`): the -2..+2 matrix, 28 pairs +
  8 currency rows.
- COT dashboard (`build_cot`), Seasonality (`build_seasonality`).
- Economic Heatmap (`data/*.html` via `build_economic_heatmap`): per-currency macro
  release tables (Actual/Forecast/Previous/Surprise + impact chips).
- Asset Scorecard (`data/scorecard.html` via `build_scorecard` + `scorecard_template.html`):
  per-currency deep dive, bias gauge, sub-scores, indicator tables.
- Inflation Data (`data/inflation.html` via `build_inflation` + `inflation_template.html`):
  CPI/PPI bars + tables + historical CPI line chart. Persistent CPI archive at
  `data/cache/cpi_history_archive.json` (merges every run, never drops old points).

## Dead endpoints (do not retry)

- `markets.tradingeconomics.com` (old TE chart host): dead, does not resolve.
- TE chart-data CloudFront (`d3ii0wo49og5mi.cloudfront.net/...`): works in browser, sandbox
  can't reach it. Investing `__NEXT_DATA__` is the JPY CPI source instead.
- TE `more-history` AJAX: 403. TE free scraping only gives recent calendar rows.

## Outstanding / next steps

- **Commit + push this session's work** (the 4 new sources + targeted-refresh CLI + K/M
  formatting). Run `python main.py` first to confirm it renders.
- **Delete `data/cache/chf_ppi_debug_CHF.html`** (~280KB leftover from parser debugging;
  the Cowork sandbox lacked delete permission). `git rm` or delete before committing.
- **CHF PPI cache** (`data/cache/myfxbook_ppi.json`) was hand-written this session with the
  real live values (Actual -2.0, Consensus -2.6, Previous -2.7, est. date 2026-05-16,
  score +1). The next `refresh_investing.py chf_ppi` regenerates it via the real code path.
- **GBP PPI open question**: Vector uses the `ppi-input-yoy` TE slug for GBP, which is the
  odd one out vs the other currencies' output/headline PPI. Whether EdgeFinder (A1 Trading)
  uses input or output for the UK is unconfirmed; the way to settle it is to read the actual
  UK PPI % number EdgeFinder displays (input ~7.7% vs output ~4.0% as of May 2026). If it's
  output, switch GBP to `ppi-output-yoy`.
- Backtest harness idea (paused): point-in-time IC test at the currency level (8 currencies,
  not 28 pairs), starting with trend + COT (clean history), measuring Information
  Coefficient + return-by-score buckets + a long-top/short-bottom basket vs random.
- From the original handover, still open: calibrate Asset Scorecard sub-bias thresholds;
  calibrate separate currency-row bias thresholds (they reuse pair thresholds, skew
  Neutral); "Delta vs yesterday" column on the main heatmap; delete old probe scripts
  (`scripts\probe_te_tokyo.py`, `scripts\probe_te_chart.py`); optional Australia Monthly CPI
  Indicator for a smoother AUD line.

## User context

Yanaël, aiming to become a Global Macro trader (first step: multi-asset futures prop).
Coaching tennis in NY May-July 2026, moving to France Jan 2027. Preferences: be direct,
no fluff, no hedging, no corporate tone, never use the em-dash character.
