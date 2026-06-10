# Vector - FX Macro Heatmap (project handover)

Self-hosted FX swing-trading scoring tool branded **Vector**. Aggregates technical,
sentiment, and fundamental indicators into a -2..+2 score per cell, summed to a
per-pair / per-currency bias (Very Bullish .. Very Bearish). Lives at
`C:\Users\yanae\Desktop\Swing Trading\edgefinder`, pushed to GitHub repo
`Nastarax/edgefinder`, served via GitHub Pages. Dark theme, gradient blue "V" mark.

GitHub Actions runs `main.py` every 30 min at :05/:35 UTC (`.github/workflows/hourly.yml`),
commits regenerated outputs, GH Pages deploys. NB: checkout resets file mtimes, which
made the px cache always look "<1h fresh" in CI, so Actions used to render with prices
frozen at the last local push; `_is_fresh` in `src/fetchers/prices.py` now returns
False when `GITHUB_ACTIONS=true`, forcing a real yfinance fetch (stale-cache fallback
still applies on failure). The Pages actions
(`upload-pages-artifact`, `deploy-pages`) are pinned to v5.0.0 full commit SHAs
because GitHub's CDN retired the v3 tarballs and v4 was unreliable. If GitHub
disables the scheduled workflow after 60 days of inactivity, re-enable it from the
Actions tab (yellow banner). Investing.com and Myfxbook are Cloudflare-blocked on
GH Actions, so those sources are refreshed locally (`scripts/refresh_investing.py`,
Windows Task Scheduler, daily 9:30 AM) and committed.

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

Targets: `mpmi, spmi, cpi, cpi_history, ppi, cc, jolts, adp, pce, mfx_ppi, cad_retail, core`.
No args = all, in that order. It prints a tailored `git add` line for only what was
refreshed.

## Data sources by indicator (current)

```
Trend / Seasonality:  yfinance (Daily + 4H)
COT:                  CFTC Socrata API (Legacy Non-Commercial)
Crowd Sentiment:      Myfxbook + Forexbenchmark (averaged)
GDP:                  TE (Actual vs Consensus, fallback TEForecast)
mPMI:                 Investing per-currency, Actual vs Forecast (fallback Previous)
sPMI:                 USD = Investing ISM Non-Manufacturing PMI (id 176, Actual vs Forecast);
                      EUR/GBP/AUD/JPY/CAD = Investing Actual vs Forecast (fallback Previous);
                      CHF = TE Swiss Services PMI; NZD = BusinessNZ PSI direct (no
                      forecast published, so falls back to Previous in practice)
Retail Sales:         CAD = Investing Retail Sales MoM (id 260, Actual vs Forecast);
                      AUD = ABS MHSI; other 6 = TE (Actual vs Consensus)
Consumer Conf:        USD = Investing CB Consumer Confidence (Actual vs Forecast);
                      other 7 = TE Actual vs Forecast (Consensus, TEForecast
                      fallback; no forecast -> neutral)
CPI YoY:              Investing per-currency (Actual vs Forecast); JPY = Investing Tokyo
                      Core CPI (id 328); fallback Previous for CHF
PPI YoY:              CHF = Myfxbook Producer & Import Prices YoY (Actual vs Consensus);
                      AUD = Myfxbook Australia PPI YoY (Actual vs Consensus);
                      GBP = Investing PPI Output (id 730, Actual vs Forecast);
                      NZD = Investing PPI Output (id 247, Actual vs Forecast);
                      other 4 (USD/EUR/JPY/CAD) = TE (Actual vs Consensus,
                      fallback TEForecast).
PCE YoY:              USD = Investing Core PCE Price Index YoY (id 905, Actual vs
                      Forecast, fallback Previous); fallback TE. US-only.
NFP / Jobless Claims: TE, US-only
ADP:                  Investing ADP Nonfarm Employment Change (id 1), Actual vs Forecast, US-only
JOLTS:                Investing JOLTS Job Openings (id 1057), Actual vs Forecast, US-only
Interest Rates:       TE rate outlook (TEForecast vs current)
Unemployment Rate:    TE all 8 (down_is_bullish)
```

## Indices (standalone instruments, base ccy + empty quote)

- **NIKKEI** (`NKY`, yfinance `^N225`): Japanese equity index. Growth/jobs/inflation
  reuse JPY's per-currency cells (risk-on); US-only labour cells left blank; rates =
  US 2Y yield vs 8-day SMA, inverted. COT = CME "NIKKEI STOCK AVERAGE YEN DENOM".
- **NASDAQ** (`NDX`, yfinance `^NDX`): US equity index (NASDAQ-100). Risk-on US-macro
  mapping verified against EdgeFinder's NASDAQ Asset Scorecard: growth + jobs (incl.
  the US-only labour cells NFP/ADP/JOLTS/Claims/Unemployment) **mirror USD un-inverted**;
  inflation (CPI/PPI/PCE) **inverted** (hot inflation = bearish equities); rates = US 2Y
  yield vs **21-day** SMA, inverted (EdgeFinder's "2 Yr Yield (21 day SMA)" cell). COT =
  CME "NASDAQ MINI" (E-mini Nasdaq-100) Legacy report; crowd = CFTC non-reportable
  contrarian proxy. Scoring block in `build_currency_scores` (COMMODITY_CCYS loop);
  scorecard fundamentals rows in `build_economic_heatmap.build_all` via `_index_row` /
  `_index_rates_row` (reusing each USD row's `stocks_impact`). NB: NASDAQ uses a 21-day
  yield SMA per EdgeFinder's NASDAQ card; the Nikkei still uses 8-day.

## Recent changes (committed)

00. **Pair-level history + WATCH-flip alerts.**
    - `save_pair_snapshot` in `score_history.py`: records each pair's daily
      score/bias/loc/setup into `score_history.json` alongside the currency entries
      (no key collisions: scorecard uses NKY/NDX/XAU, pairs use NIKKEI/NASDAQ/XAUUSD;
      the IC harness only reads the 8 fiat keys). Purpose: validate whether
      bias+WATCH entries outperform bias+EXT once history accumulates. Forward-only,
      no backfill (lookahead bias).
    - `src/output/notify.py`: after each live run, diffs pair setup states against
      `data/cache/setup_state.json` and pushes an alert when a pair ENTERS watch.
      Channels by env var: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID (Telegram) and/or
      DISCORD_WEBHOOK_URL (Discord webhook); neither set = console print only.
      First run records a baseline silently; exits from watch never alert; backtest
      runs (`--date`) skip notify entirely. `hourly.yml` passes the three secrets
      and commits `setup_state.json` back so the diff survives between runs.
      **Secrets must be added in repo Settings -> Secrets and variables -> Actions
      before alerts fire.**

0. **Location column + Setup ready filter** on the main heatmap (S&D entry confluence).
   `range_position` in `score_technical.py`: last close's position in the 40-session
   high-low range, 0..100. `_setup_state` in `score_pair.py` combines it with bias:
   WATCH (bullish bias + <=35% discount, or bearish + >=65% premium = price pulled back
   to the zone-hunting side), EXT (biased but at the far end of the range), MID
   (directional, mid-range); neutral rows show the bare %, currency rows n/a. Chip styles
   (`.loc-chip`) in `data/vector.css`; "Setup ready" radio filter via `data-setup` attr.
   - **NaN partial-bar fix** (same commit): yfinance appends a partial current-day bar
     with NaN OHLC on cross pairs. A NaN last close made the trend SMAs NaN, every
     comparison False, and `trend_score` a hardcoded -2 for every affected cross
     (intermittent, whenever the partial bar was present at run time). Both
     `trend_score` and `range_position` now dropna first. Fixing this moved several
     pairs out of fake-Neutral (e.g. USDCAD +6 -> +10).
1. **CPI Indicator chart** on the Inflation page (`inflation_template.html`,
   `build_inflation.py`). A1 EdgeFinder-style per-currency bar+line chart: blue bars
   for CPI actual, pink line for CPI forecast (consensus), dashed horizontal line for
   central bank target inflation rate. Country dropdown (8 currencies), date range
   selector (12/18/24 months or All). Data labels on bars, combined tooltip.
   - Forecast data extracted from Investing.com `__NEXT_DATA__` occurrences alongside
     actuals (same `_parse_cpi_occurrences` call, new `forecast` field).
   - Persistent forecast archive at `data/cache/cpi_forecast_archive.json` (same
     merge-never-drop pattern as the actuals archive).
   - Target rates: 2% for all currencies except AUD (2.5%, RBA's 2-3% midband).
   - **Date-mapping fix**: removed the old "splice" code in `build_all` that used CPI
     release dates (e.g. May 20) as chart data points instead of the reference period
     (April). The splice created spurious bars shifted one month forward. Investing
     history + FRED already provide correctly reference-dated data for all currencies.
   - To refresh forecast history: `python scripts\refresh_investing.py cpi_history`.
     NZD/CHF may 403 from Cloudflare; their forecasts accumulate over time from the
     latest-release splice in `cpi_latest`.
2. **US Consumer Confidence -> Investing CB Consumer Confidence (id 48)**, Actual vs
   Forecast (fallback Previous), USD only. New fetcher
   `src/fetchers/investing_consumer_conf.py` (cache `data/cache/investing_consumer_conf.json`).
   Other 7 currencies stay on TE momentum.
3. **US JOLTS -> Investing JOLTS Job Openings (id 1057)**, Actual vs Forecast, USD only.
   New fetcher `src/fetchers/investing_jolts.py` (cache `data/cache/investing_jolts.json`).
4. **US ADP -> Investing ADP Nonfarm Employment Change (id 1)**, Actual vs Forecast,
   USD only. New fetcher `src/fetchers/investing_adp.py` (cache `data/cache/investing_adp.json`).
5. **CHF PPI -> Myfxbook Switzerland Producer & Import Prices YoY**, Actual vs Consensus
   (fallback Previous), CHF only. New fetcher `src/fetchers/myfxbook_ppi.py`
   (cache `data/cache/myfxbook_ppi.json`). NZD stays on Investing, the other 6 on TE.
6. **Targeted-refresh CLI** in `scripts/refresh_investing.py` (registry + arg parsing).
7. **K/M number formatting** on the Economic Heatmap (`build_economic_heatmap.py`, the
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
  CPI/PPI bars + tables + historical CPI line chart + per-currency CPI indicator chart
  (bar+forecast line+target rate, A1 EdgeFinder style). Persistent archives at
  `data/cache/cpi_history_archive.json` (actuals) and `data/cache/cpi_forecast_archive.json`
  (consensus forecasts). Both merge every run, never drop old points.

## Dead endpoints (do not retry)

- `markets.tradingeconomics.com` (old TE chart host): dead, does not resolve.
- TE chart-data CloudFront (`d3ii0wo49og5mi.cloudfront.net/...`): works in browser, sandbox
  can't reach it. Investing `__NEXT_DATA__` is the JPY CPI source instead.
- TE `more-history` AJAX: 403. TE free scraping only gives recent calendar rows.

## Outstanding / next steps

- **CHF/NZD CPI forecast gaps**: Investing.com 403s for CHF and NZD CPI history pages
  (Cloudflare). Their forecast archives only have the latest-release point. Re-running
  `refresh_investing.py cpi_history` periodically will accumulate more points over time
  as Cloudflare lets them through.
- **GBP PPI open question**: Vector uses the `ppi-input-yoy` TE slug for GBP, which is the
  odd one out vs the other currencies' output/headline PPI. Whether EdgeFinder (A1 Trading)
  uses input or output for the UK is unconfirmed; the way to settle it is to read the actual
  UK PPI % number EdgeFinder displays (input ~7.7% vs output ~4.0% as of May 2026). If it's
  output, switch GBP to `ppi-output-yoy`.
- Backtest harness (scaffolded): `python scripts/backtest_ic.py [horizons...]`. Read-only.
  Reads `score_history.json` + `px_*.pkl`, builds an equal-weighted per-currency basket
  return (ccy vs all its fiat crosses, +base / -quote), and reports Spearman IC (mean,
  t-stat, hit rate), return-by-score buckets, and a long-top/short-bottom spread, at
  forward horizons measured in score snapshots. Sample is small until `score_history.json`
  accumulates (~30+ snapshots for a meaningful t-stat). Weekend snapshots self-drop (zero
  forward-return variance). Next steps: filter to trading days; per-sub-score IC
  (trend/COT/fundamentals/sentiment) to attribute edge; then weight/threshold calibration.
- From the original handover, still open: "Delta vs yesterday" column on the main
  heatmap (partially superseded by the WATCH Telegram alerts); delete old probe scripts
  (`scripts\probe_te_tokyo.py`, `scripts\probe_te_chart.py`); optional Australia Monthly CPI
  Indicator for a smoother AUD line.
- Threshold calibration DONE (structural, 2026-06-10): currency rows use
  `currency_bias_thresholds` in `indicators.yaml` (full 4/8 for USD+XAU's 15 active
  cells, reduced 3/5 for other fiat's 10; same per-cell fraction as pair thresholds).
  Scorecard `_sub_bias` got a 0.2 neutral band (one stray cell in a wide section no
  longer reads directional). Revisit both with the IC bucket data in late July.

## User context

Yanaël, aiming to become a Global Macro trader (first step: multi-asset futures prop).
Coaching tennis in NY May-July 2026, moving to France Jan 2027. Preferences: be direct,
no fluff, no hedging, no corporate tone, never use the em-dash character.
