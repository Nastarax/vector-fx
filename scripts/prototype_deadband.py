"""
Prototype harness for the macro-surprise neutral deadband.

Recomputes per-currency macro scores at several deadband values using the
SAME cached inputs that produced data/output.html, then reports pair totals
side by side with EdgeFinder's published values for the 10 overlapping pairs.

Only macro (surprise-vs-forecast) cells move with the deadband; trend,
seasonality, crowd and COT are read straight from the freshly generated
output.html, so the db=0.0 column must reproduce the live board exactly. That
exact-match check (printed as VALIDATION) is what guarantees the recompute is
faithful to production rather than an independent approximation.

Run AFTER `python main.py` so output.html reflects the current caches.
"""
from __future__ import annotations

import sys
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.fetchers import (abs_au, forexfactory, investing, investing_adp,
                          investing_consumer_conf, investing_core, investing_cpi,
                          investing_jolts, investing_ppi, investing_retail_sales,
                          myfxbook_ppi, services_pmi, tradingeconomics)
from src.fetchers.cot import COMMODITY_CCYS
from src.scoring.score_pair import (build_currency_scores, load_indicators_cfg,
                                     load_pairs_cfg)

CCYS = ["USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD"]
DEADBANDS = [0.0, 0.02, 0.03, 0.05, 0.10]

# EdgeFinder totals for the overlapping pairs (from the user's screenshot).
EF = {
    "EURUSD": -5, "GBPUSD": -8, "GBPCHF": -10, "NZDCAD": 5, "AUDNZD": -5,
    "EURNZD": -8, "EURCHF": -8, "GBPJPY": -7, "AUDCHF": -6, "CADCHF": -9,
}


def load_inputs():
    te_history = tradingeconomics.load_history()
    ff_history = forexfactory.load_history()
    investing_cpi_data = dict(investing_cpi.load_cached() or {})
    tokyo = investing_cpi.load_tokyo_core_cpi()
    if tokyo and tokyo.get("actual") is not None:
        investing_cpi_data["JPY"] = tokyo
    return dict(
        macro_data={c: {} for c in CCYS},          # only the keys (ccy list) are used
        cot_data={},                                # COT taken from output.html instead
        ff_history=ff_history,
        te_history=te_history,
        investing_mpmi=investing.load_cached(),
        investing_spmi=services_pmi.load_cached(),
        investing_cpi=investing_cpi_data,
        investing_ppi=investing_ppi.load_cached(),
        myfxbook_ppi=myfxbook_ppi.load_cached(),
        investing_cc=investing_consumer_conf.load_cached(),
        investing_jolts=investing_jolts.load_cached(),
        investing_adp=investing_adp.load_cached(),
        investing_retail_sales=investing_retail_sales.load_cached(),
        rates_outlook=tradingeconomics.load_rates_outlook(),
        investing_core=investing_core.load_cached(),
        abs_au_mhsi=abs_au.load_cached() or {},
    )


def parse_board(html_path: str):
    """Return {symbol: {"total": int, "fixed": int}} for pair rows, where
    fixed = trend+seasonality+crowd+cot (the deadband-invariant cells)."""
    soup = BeautifulSoup(open(html_path, encoding="utf-8").read(), "html.parser")
    out = {}
    for tr in soup.select("tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 8:
            continue
        sym = tds[0].get_text(strip=True)
        if sym in out or sym not in EF:
            continue  # first occurrence only (main matrix)
        try:
            total = int(tds[2].get_text(strip=True))
        except ValueError:
            continue
        vals = [td.get_text(strip=True) for td in tds[3:]]

        def num(i):
            try:
                return int(vals[i])
            except (ValueError, IndexError):
                return 0
        # flat indicator order: trend, seasonality, cot, crowd, ...
        fixed = num(0) + num(1) + num(2) + num(3)
        out[sym] = {"total": total, "fixed": fixed}
    return out


def macro_pair_sum(per_ccy, base, quote, macro_ids):
    """Replicates build_pair_rows currency-diff logic for macro cells only."""
    onesided = ("nfp", "jobless_claims", "adp", "jolts")
    total = 0
    for ind_id in macro_ids:
        if ind_id in ("nfp", "unemployment_rate", "jobless_claims", "adp", "jolts") and base in COMMODITY_CCYS:
            s = per_ccy.get(base, {}).get(ind_id)
            total += s if s is not None else 0
            continue
        base_s = per_ccy.get(base, {}).get(ind_id)
        quote_s = per_ccy.get(quote, {}).get(ind_id)
        if base_s is None or quote_s is None:
            if ind_id in onesided:
                if base_s is not None:
                    total += max(-2, min(2, base_s))
                elif quote_s is not None:
                    total += max(-2, min(2, -quote_s))
            # else contributes 0
        else:
            total += max(-2, min(2, base_s - quote_s))
    return total


def main():
    cfg = load_indicators_cfg()
    flat = [i["id"] for inds in cfg["categories"].values() for i in inds]
    macro_ids = [i for i in flat if i not in ("trend", "seasonality", "crowd", "cot")]

    pairs = {p["symbol"]: (p["base"], p["quote"]) for p in load_pairs_cfg()["pairs"]}
    board = parse_board("data/output.html")
    inputs = load_inputs()

    # Recompute per_ccy at each deadband.
    per_ccy_by_db = {db: build_currency_scores(surprise_deadband=db, **inputs) for db in DEADBANDS}

    print(f"{'PAIR':8}{'EF':>5}", end="")
    for db in DEADBANDS:
        tag = "db=0" if db == 0 else f"{db:.2f}"
        print(f"{tag:>8}", end="")
    print("   (validation db=0 vs live board)")

    ok = True
    for sym in EF:
        base, quote = pairs[sym]
        fixed = board[sym]["fixed"]
        live_total = board[sym]["total"]
        print(f"{sym:8}{EF[sym]:>5}", end="")
        totals = {}
        for db in DEADBANDS:
            t = fixed + macro_pair_sum(per_ccy_by_db[db], base, quote, macro_ids)
            totals[db] = t
            print(f"{t:>8}", end="")
        match = "OK" if totals[0.0] == live_total else f"MISMATCH(live={live_total})"
        if totals[0.0] != live_total:
            ok = False
        print(f"   {match}")

    print("\nVALIDATION:", "all db=0 totals match the live board"
          if ok else "MISMATCH - recompute is not faithful, do not trust other columns")

    # Currency totals (macro-only) for context.
    print("\nPer-currency MACRO-only total by deadband:")
    print(f"{'CCY':5}", end="")
    for db in DEADBANDS:
        tag = "db=0" if db == 0 else f"{db:.2f}"
        print(f"{tag:>8}", end="")
    print()
    for c in CCYS:
        print(f"{c:5}", end="")
        for db in DEADBANDS:
            s = sum(v for k, v in per_ccy_by_db[db][c].items()
                    if k not in ("trend", "seasonality", "crowd", "cot") and v is not None)
            print(f"{s:>8}", end="")
        print()


if __name__ == "__main__":
    main()
