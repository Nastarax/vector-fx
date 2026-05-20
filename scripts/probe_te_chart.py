"""
Probe the TE CloudFront chart-data endpoint for Tokyo Core CPI.

Found via DevTools:
  https://d3ii0wo49og5mi.cloudfront.net/economics/jpntcc?span=10y
No auth token, CORS open, returns JSON. The sandbox can't reach CloudFront,
so run this locally and paste the output so the parser is built correctly.

    python scripts/probe_te_chart.py
"""
import json

try:
    from curl_cffi import requests as rq
    IMP = {"impersonate": "chrome120"}
except ImportError:
    import requests as rq
    IMP = {}

HOST = "https://d3ii0wo49og5mi.cloudfront.net/economics"
HDR = {"Referer": "https://tradingeconomics.com/japan/tokyo-core-cpi"}


def probe(span):
    url = f"{HOST}/jpntcc?span={span}"
    print(f"\n=== GET {url}")
    try:
        r = rq.get(url, timeout=25, headers=HDR, **IMP)
        print(f"status {r.status_code}, length {len(r.text)}")
        try:
            data = r.json()
        except Exception:
            print("NOT JSON. First 500 chars:")
            print(r.text[:500])
            return
        print("top-level type:", type(data).__name__)
        if isinstance(data, dict):
            print("dict keys:", list(data.keys()))
            # dig one level for a series-looking list
            for k, v in data.items():
                if isinstance(v, list) and v:
                    print(f"  '{k}' is a list of {len(v)}; first item: {v[0]}")
                    print(f"     last item: {v[-1]}")
        elif isinstance(data, list):
            print("list length:", len(data))
            print("first item:", json.dumps(data[0]) if data else None)
            print("last item :", json.dumps(data[-1]) if data else None)
    except Exception as e:
        print("ERROR:", e)


if __name__ == "__main__":
    probe("10y")
    probe("MAX")
