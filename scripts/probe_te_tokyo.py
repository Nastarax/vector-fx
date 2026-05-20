"""
One-off probe: figure out how to pull Tokyo Core CPI from Trading Economics,
for BOTH (a) the inflation line-chart history and (b) JPY CPI scoring
(latest Actual vs Consensus).

The TE interactive chart loads its data from markets.tradingeconomics.com using
a symbol and a daily-rotating guest token embedded in the page. The Cowork
sandbox can't reach that subdomain, so run this locally and paste the output
back so the fetcher can be built against the real response shape.

    python scripts/probe_te_tokyo.py
"""
import re
import sys

try:
    from curl_cffi import requests as rq
    IMP = {"impersonate": "chrome120"}
except ImportError:
    import requests as rq
    IMP = {}

from bs4 import BeautifulSoup

PAGE = "https://tradingeconomics.com/japan/tokyo-core-cpi"
HDR = {"Referer": PAGE,
       "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}


def get(url, **kw):
    return rq.get(url, timeout=25, headers=HDR, **{**IMP, **kw})


def main():
    print("1) Fetching the page to extract symbol + guest token...")
    html = get(PAGE).text
    sym_m = re.search(r"TESymbol\s*=\s*'([A-Z0-9]+)'", html)
    tok_m = re.search(r"(?:Token|TEpapikey|auth|AUTH)\s*[:=]\s*'([0-9]{6,8}:[a-z0-9]+)'", html, re.IGNORECASE)
    symbol = sym_m.group(1) if sym_m else None
    token = tok_m.group(1) if tok_m else None
    if not token:
        any_tok = re.search(r"'(\d{8}:[a-z0-9]+)'", html)
        token = any_tok.group(1) if any_tok else None
    print(f"   symbol = {symbol!r}")
    print(f"   token  = {token!r}")

    # --- (b) SCORING DATA: latest release with consensus ---
    print("\n2) META DESCRIPTION (latest + previous):")
    m = re.search(r'<meta name="description" content="([^"]+)"', html)
    print("   " + (m.group(1)[:300] if m else "(none found)"))

    print("\n3) CALENDAR TABLE rows (for Actual / Consensus / Previous):")
    soup = BeautifulSoup(html, "html.parser")
    target = None
    for table in soup.find_all("table"):
        heads = " ".join(th.get_text(strip=True).lower() for th in table.find_all("th"))
        if "actual" in heads and ("consensus" in heads or "forecast" in heads or "teforecast" in heads):
            target = table
            break
    if target:
        heads = [th.get_text(strip=True) for th in target.find_all("th")]
        print("   HEADERS:", heads)
        body = target.find("tbody") or target
        for i, row in enumerate(body.find_all("tr")[:6]):
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            print(f"   ROW{i}:", cells)
    else:
        print("   (no Actual/Consensus calendar table found)")

    # --- (a) HISTORY: chart endpoints ---
    if not token or not symbol:
        print("\n!! Missing symbol or token; can't probe chart history. Paste page HTML if needed.")
        return
    endpoints = [
        f"https://markets.tradingeconomics.com/chart?s={symbol}&span=10Y&securify=new&url=/japan/tokyo-core-cpi&AUTH={token}&ohlc=0",
        f"https://markets.tradingeconomics.com/chart?s={symbol}&span=MAX&securify=new&url=/japan/tokyo-core-cpi&AUTH={token}&ohlc=0",
    ]
    for i, url in enumerate(endpoints, 1):
        print(f"\n4.{i}) GET {url[:95]}...")
        try:
            r = get(url)
            body = r.text
            print(f"     status {r.status_code}, length {len(body)}")
            print("     FIRST 600 CHARS:")
            print("     " + body[:600].replace("\n", "\n     "))
            print("     ...LAST 300 CHARS:")
            print("     " + body[-300:].replace("\n", "\n     "))
        except Exception as e:
            print(f"     ERROR: {e}")


if __name__ == "__main__":
    main()
