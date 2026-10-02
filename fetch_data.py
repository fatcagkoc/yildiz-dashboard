"""
Builds data.json for the Yıldız Holding stock dashboard.

Sources (all free, no API key):
  - TradingView chart websocket  -> daily history for stocks and BIST indices
  - TradingView scanner          -> market cap, P/E, P/B, shares, sector peers
  - Yahoo Finance chart API      -> live USD/TRY (+ fallback history for stocks)

If a source fails, the previous value from the existing data.json is kept,
so the dashboard never goes blank because of one bad run.
"""
import datetime as dt
import json
import os
import sys
import time
import traceback

import requests

# ---------------- TradingView chart websocket ----------------
import random, re, string, ssl
from urllib.parse import urlparse
import websocket

WS_URL = "wss://data.tradingview.com/socket.io/websocket?from=chart"

def _frame(func, params):
    body = json.dumps({"m": func, "p": params}, separators=(",", ":"))
    return f"~m~{len(body)}~m~{body}"

def _split(raw):
    return [p for p in re.split(r"~m~\d+~m~", raw) if p]

def _sid(prefix):
    return prefix + "".join(random.choices(string.ascii_lowercase, k=12))

def fetch_history(symbol, bars=400, timeout=30):
    """Return list of (unix_ts, close) daily bars, oldest first."""
    kw = {}
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        u = urlparse(proxy)
        kw.update(http_proxy_host=u.hostname, http_proxy_port=u.port, proxy_type="http")
    ca = os.environ.get("SSL_CERT_FILE")
    sslopt = {"ca_certs": ca} if ca else {"cert_reqs": ssl.CERT_REQUIRED}
    ws = websocket.create_connection(WS_URL, timeout=timeout,
                                     header=["Origin: https://www.tradingview.com"],
                                     sslopt=sslopt, **kw)
    cs = _sid("cs_")
    try:
        ws.send(_frame("set_auth_token", ["unauthorized_user_token"]))
        ws.send(_frame("chart_create_session", [cs, ""]))
        ws.send(_frame("resolve_symbol", [cs, "sym_1", "=" + json.dumps({"symbol": symbol, "adjustment": "splits"})]))
        ws.send(_frame("create_series", [cs, "s1", "s1", "sym_1", "1D", bars, ""]))
        out = []
        while True:
            raw = ws.recv()
            for part in _split(raw):
                if part.startswith("~h~"):
                    ws.send(f"~m~{len(part)}~m~{part}")  # heartbeat echo
                    continue
                try:
                    msg = json.loads(part)
                except ValueError:
                    continue
                m = msg.get("m")
                if m in ("timescale_update", "du"):
                    s = msg["p"][1].get("s1", {}).get("s", [])
                    for b in s:
                        out.append((int(b["v"][0]), float(b["v"][4])))
                elif m == "symbol_error":
                    raise RuntimeError(f"{symbol}: symbol_error {msg['p']}")
                elif m == "series_error":
                    raise RuntimeError(f"{symbol}: series_error {msg['p']}")
                elif m == "series_completed":
                    d = {}
                    for t, c in out:
                        d[t] = c
                    return sorted(d.items())
    finally:
        ws.close()

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "data.json")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36"}
BARS = 400  # ~1.6 years of trading days

COMPANIES = [
    {"ticker": "ULKER", "name": "Ülker Bisküvi", "bench": "XGIDA"},
    {"ticker": "BESLR", "name": "Besler Gıda", "bench": "XGIDA"},
    {"ticker": "SOKM", "name": "ŞOK Marketler", "bench": "XTCRT"},
    {"ticker": "BIZIM", "name": "Bizim Toptan", "bench": "XTCRT"},
    {"ticker": "GOZDE", "name": "Gözde Girişim", "bench": "XHOLD"},
    {"ticker": "PENTA", "name": "Penta Teknoloji", "bench": "XUTEK"},
    {"ticker": "MAKTK", "name": "Makina Takım", "bench": "XMESY"},
]
BENCHMARKS = {
    "XU100": "BIST 100",
    "XGIDA": "BIST Gıda İçecek",
    "XTCRT": "BIST Ticaret",
    "XHOLD": "BIST Holding ve Yatırım",
    "XUTEK": "BIST Teknoloji",
    "XMESY": "BIST Metal Eşya Makina",
}
# Valuation peer groups (TradingView classification). GOZDE sits in "Finance",
# which is dominated by banks, so it is compared with investment companies only.
PEER_OVERRIDE = {
    "GOZDE": {"industries": ["Investment Banks/Brokers", "Financial Conglomerates",
                             "Investment Trusts/Mutual Funds", "Investment Managers"],
              "label": "Yatırım şirketleri"},
}
SECTOR_TR = {
    "Consumer Non-Durables": "Dayanıksız tüketim", "Retail Trade": "Perakende",
    "Finance": "Finans", "Distribution Services": "Dağıtım hizmetleri",
    "Producer Manufacturing": "Üretim / makine", "Electronic Technology": "Elektronik teknoloji",
    "Technology Services": "Teknoloji hizmetleri",
}


def log(*a):
    print(*a, flush=True)


def to_date(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone(dt.timedelta(hours=3))).date().isoformat()


def series(bars):
    return [[to_date(t), round(c, 4)] for t, c in bars]


def retry(fn, *a, tries=3, **kw):
    last = None
    for i in range(tries):
        try:
            return fn(*a, **kw)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 + 3 * i)
    raise last


def yahoo_chart(symbol, rng="2y"):
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                     params={"range": rng, "interval": "1d"}, headers=UA, timeout=30)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    closes = res["indicators"]["quote"][0]["close"]
    bars = [(t, c) for t, c in zip(res.get("timestamp", []), closes) if c is not None]
    return bars, res["meta"]


def tv_scan(payload):
    r = requests.post("https://scanner.tradingview.com/turkey/scan", json=payload, headers=UA, timeout=30)
    r.raise_for_status()
    return r.json()["data"]


def weighted(rows, key):
    """Market-cap weighted multiple: sum(mcap) / sum(mcap / multiple), positives only."""
    pts = [(r["mcap"], r[key]) for r in rows if r.get("mcap") and r.get(key) and r[key] > 0]
    if len(pts) < 3:
        return None
    return round(sum(m for m, _ in pts) / sum(m / x for m, x in pts), 2)


def main():
    prev = {}
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            prev = json.load(f)
    prev_co = {c["ticker"]: c for c in prev.get("companies", [])}
    prev_bm = prev.get("benchmarks", {})
    warnings = []

    # ---- Fundamentals (TradingView scanner) ----
    cols = ["name", "close", "market_cap_basic", "price_earnings_ttm", "price_book_fq",
            "total_shares_outstanding_fundamental", "sector", "industry"]
    fund = {}
    try:
        rows = retry(tv_scan, {"symbols": {"tickers": [f"BIST:{c['ticker']}" for c in COMPANIES]}, "columns": cols})
        for r in rows:
            d = dict(zip(cols, r["d"]))
            fund[d["name"]] = d
        log("scanner: fundamentals ok for", sorted(fund))
    except Exception as e:  # noqa: BLE001
        warnings.append(f"fundamentals: {e}")
        log("scanner FAILED", e)

    # ---- Sector peer multiples ----
    peer_cache = {}

    def peers(sector):
        if sector not in peer_cache:
            data = retry(tv_scan, {
                "filter": [{"left": "sector", "operation": "equal", "right": sector},
                           {"left": "exchange", "operation": "equal", "right": "BIST"},
                           {"left": "type", "operation": "equal", "right": "stock"}],
                "columns": ["name", "market_cap_basic", "price_earnings_ttm", "price_book_fq", "industry"],
                "range": [0, 1000]})
            peer_cache[sector] = [{"name": x["d"][0], "mcap": x["d"][1], "pe": x["d"][2], "pb": x["d"][3],
                                   "industry": x["d"][4]} for x in data]
        return peer_cache[sector]

    # ---- FX ----
    fx = prev.get("fx", {})
    try:
        bars, meta = retry(yahoo_chart, "USDTRY=X")
        fx = {"last": round(meta["regularMarketPrice"], 4), "history": series(bars), "source": "Yahoo"}
        log("fx: yahoo ok", fx["last"], len(fx["history"]))
    except Exception as e:  # noqa: BLE001
        log("fx yahoo failed, trying TradingView", e)
        try:
            bars = retry(fetch_history, "FX_IDC:USDTRY", BARS)
            fx = {"last": round(bars[-1][1], 4), "history": series(bars), "source": "TradingView"}
        except Exception as e2:  # noqa: BLE001
            warnings.append(f"fx: {e2}")

    # ---- Benchmarks ----
    benchmarks = {}
    for code, name in BENCHMARKS.items():
        try:
            bars = retry(fetch_history, f"BIST:{code}", BARS)
            benchmarks[code] = {"name": name, "history": series(bars)}
            log("bench", code, len(bars), bars[-1][1])
        except Exception as e:  # noqa: BLE001
            warnings.append(f"{code}: {e}")
            log("bench FAILED", code, e)
            if code in prev_bm:
                benchmarks[code] = prev_bm[code]

    # ---- Companies ----
    companies = []
    for c in COMPANIES:
        t = c["ticker"]
        old = prev_co.get(t, {})
        item = dict(c)
        try:
            bars = retry(fetch_history, f"BIST:{t}", BARS)
            item["history"] = series(bars)
            item["history_source"] = "TradingView"
        except Exception as e:  # noqa: BLE001
            log("tv history failed for", t, e, "-> Yahoo")
            try:
                bars, _ = retry(yahoo_chart, f"{t}.IS")
                item["history"] = series(bars)
                item["history_source"] = "Yahoo"
            except Exception as e2:  # noqa: BLE001
                warnings.append(f"{t} history: {e2}")
                item["history"] = old.get("history", [])
                item["history_source"] = old.get("history_source", "-")

        f = fund.get(t)
        if f:
            item.update({
                "shares": f["total_shares_outstanding_fundamental"],
                "mcap": f["market_cap_basic"],
                "pe": f["price_earnings_ttm"] if (f["price_earnings_ttm"] or 0) > 0 else None,
                "pb": f["price_book_fq"],
                "tv_sector": f["sector"],
                "tv_industry": f["industry"],
            })
        else:
            for k in ("shares", "mcap", "pe", "pb", "tv_sector", "tv_industry"):
                item[k] = old.get(k)

        # keep market cap consistent with the latest price
        if item.get("shares") and item.get("history"):
            item["mcap"] = item["shares"] * item["history"][-1][1]

        try:
            sector = item.get("tv_sector")
            ov = PEER_OVERRIDE.get(t)
            group = peers(sector) if sector else []
            if ov:
                group = [g for g in group if g["industry"] in ov["industries"]]
                item["peer_label"] = ov["label"]
            else:
                item["peer_label"] = SECTOR_TR.get(sector, sector)
            item["peer_count"] = len(group)
            item["sector_pe"] = weighted(group, "pe")
            item["sector_pb"] = weighted(group, "pb")
        except Exception as e:  # noqa: BLE001
            warnings.append(f"{t} peers: {e}")
            for k in ("peer_label", "peer_count", "sector_pe", "sector_pb"):
                item[k] = old.get(k)
        companies.append(item)
        log("company", t, len(item["history"]), item.get("mcap"), item.get("pe"), item.get("sector_pe"))

    ok = all(c["history"] for c in companies) and "XU100" in benchmarks and fx
    if not ok and prev:
        log("Too much missing data; keeping previous data.json")
        sys.exit(0)

    out = {
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "fx": fx,
        "benchmarks": benchmarks,
        "companies": companies,
        "warnings": warnings,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    log("wrote", OUT, os.path.getsize(OUT), "bytes;", len(warnings), "warnings")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)
