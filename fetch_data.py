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

def fetch_history(symbol, bars=400, timeout=30, resolution="1D"):
    """Return list of (unix_ts, close) bars (daily by default), oldest first."""
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
        ws.send(_frame("create_series", [cs, "s1", "s1", "sym_1", resolution, bars, ""]))
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
    {"ticker": "GOZDE", "name": "Gözde Girişim", "bench": "XUMAL"},
    {"ticker": "PENTA", "name": "Penta Teknoloji", "bench": "XUTEK"},
    {"ticker": "MAKTK", "name": "Makina Takım", "bench": "XMESY"},
]
BENCHMARKS = {
    "XU100": "BIST 100",
    "XGIDA": "BIST Gıda İçecek",
    "XTCRT": "BIST Ticaret",
    "XUMAL": "BIST Mali",
    "XUTEK": "BIST Teknoloji",
    "XMESY": "BIST Metal Eşya Makina",
}
# Valuation peers = constituents of the company's sector index.
# GOZDE's index (BIST Mali) is dominated by banks, so banks are excluded for it.
EXCLUDE_BANKS_FOR = {"GOZDE"}
INTRADAY_BARS = 70  # hourly bars, ~1.5 trading weeks

# Listed direct competitors per company (confirmed by Fatih, 2026-10-08).
PEERS = {
    "ULKER": ["KRVGD", "ELITE", "DURKN", "OYLUM"],
    "BESLR": ["TUKAS", "TATGD", "PETUN", "DARDL", "FRIGO", "PENGD"],
    "SOKM": ["BIMAS", "MGROS", "CRFSA", "GMTAS", "MOPAS", "KIMMR"],
    "BIZIM": ["MGROS", "GMTAS"],
    "GOZDE": ["ISGSY", "BULGS", "HDFGS", "VERTU"],
    "PENTA": ["INDES", "INGRM", "DGATE", "ARENA", "DESPC"],
    "MAKTK": ["MEKAG", "IMASM"],
}
PEER_CAP = 0.40  # max weight of one company in a peer index (BIST-style cap)


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


def capped_weights(mcaps, cap):
    """Market-cap weights with a per-name cap; excess is spread pro rata over the rest."""
    tot = sum(mcaps.values())
    if tot <= 0:
        return {}
    w = {k: v / tot for k, v in mcaps.items()}
    cap = max(cap, 1.0 / len(w))
    for _ in range(20):
        over = [k for k, v in w.items() if v > cap + 1e-9]
        if not over:
            break
        excess = sum(w[k] - cap for k in over)
        for k in over:
            w[k] = cap
        rest = [k for k in w if k not in over]
        rest_sum = sum(w[k] for k in rest)
        if rest_sum <= 0:
            break
        for k in rest:
            w[k] += excess * w[k] / rest_sum
    return w


def peer_index(peers, dates, cap):
    """Chain-linked, daily-rebalanced, cap-weighted price index (start = 100).

    peers: {ticker: {"shares": n, "history": [[date, close], ...]}}
    dates: trading-day calendar (list of ISO dates)
    Returns (history [[date, value]], latest weights {ticker: w}).
    """
    px = {t: dict(p["history"]) for t, p in peers.items() if p.get("history") and p.get("shares")}
    if not px:
        return [], {}
    start = min(min(h) for h in px.values())
    cal = [d for d in dates if d >= start]
    out, val, last_w, prev = [], 100.0, {}, None
    for d in cal:
        if prev is not None:
            mc = {t: peers[t]["shares"] * px[t][prev] for t in px if prev in px[t] and d in px[t]}
            w = capped_weights(mc, cap)
            if w:
                r = sum(w[t] * (px[t][d] / px[t][prev] - 1) for t in w)
                val *= 1 + r
                last_w = w
        out.append([d, round(val, 4)])
        prev = d
    return out, last_w


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
            "total_shares_outstanding_fundamental", "sector", "industry", "logoid"]
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

    # ---- Sector index constituents ----
    def members(code):
        data = retry(tv_scan, {
            "symbols": {"symbolset": [f"SYML:BIST;{code}"]},
            "columns": ["name", "description", "market_cap_basic", "price_earnings_ttm", "price_book_fq"],
            "range": [0, 1000],
            "sort": {"sortBy": "market_cap_basic", "sortOrder": "desc"}})
        return [{"t": x["d"][0], "name": x["d"][1], "mcap": x["d"][2], "pe": x["d"][3], "pb": x["d"][4]}
                for x in data]

    member_data = {}
    for code in list(BENCHMARKS) + ["XBANK"]:
        if code == "XU100":
            continue
        try:
            member_data[code] = members(code)
            log("members", code, len(member_data[code]))
        except Exception as e:  # noqa: BLE001
            warnings.append(f"{code} members: {e}")
            log("members FAILED", code, e)
    bank_set = {m["t"] for m in member_data.get("XBANK", [])}

    # ---- FX ----
    fx = prev.get("fx", {})
    try:
        bars, meta = retry(yahoo_chart, "USDTRY=X")
        fx = {"last": round(meta["regularMarketPrice"], 4), "history": series(bars), "source": "Yahoo"}
        log("fx: yahoo ok", fx["last"], len(fx["history"]))
        # Yahoo's live FX tick is occasionally noisy; prefer TradingView's latest hourly close.
        try:
            hb = retry(fetch_history, "FX_IDC:USDTRY", 5, resolution="60")
            fx["last"] = round(hb[-1][1], 4)
            log("fx: live rate from TradingView", fx["last"])
        except Exception as e:  # noqa: BLE001
            log("fx live TV failed, keeping Yahoo", e)
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
            if code in member_data:
                benchmarks[code]["members"] = [[m["t"], m["name"], m["mcap"]] for m in member_data[code]]
            elif code in prev_bm and "members" in prev_bm[code]:
                benchmarks[code]["members"] = prev_bm[code]["members"]
        except Exception as e:  # noqa: BLE001
            warnings.append(f"{code}: {e}")
            log("bench FAILED", code, e)
            if code in prev_bm:
                benchmarks[code] = prev_bm[code]

    # ---- Peers (direct listed competitors) ----
    peer_tickers = sorted({t for lst in PEERS.values() for t in lst})
    prev_peers = prev.get("peers", {})
    peers = {}
    try:
        pcols = ["name", "description", "close", "market_cap_basic", "price_earnings_ttm", "price_book_fq",
                 "total_shares_outstanding_fundamental", "logoid"]
        rows = retry(tv_scan, {"symbols": {"tickers": [f"BIST:{t}" for t in peer_tickers]}, "columns": pcols})
        for r in rows:
            d = dict(zip(pcols, r["d"]))
            peers[d["name"]] = {"name": d["description"], "shares": d["total_shares_outstanding_fundamental"],
                                "mcap": d["market_cap_basic"],
                                "pe": d["price_earnings_ttm"] if (d["price_earnings_ttm"] or 0) > 0 else None,
                                "pb": d["price_book_fq"], "logoid": d["logoid"]}
        log("scanner: peers ok", len(peers), "of", len(peer_tickers))
    except Exception as e:  # noqa: BLE001
        warnings.append(f"peer fundamentals: {e}")
        log("peer scanner FAILED", e)
    for t in peer_tickers:
        p = peers.setdefault(t, {k: prev_peers.get(t, {}).get(k) for k in ("name", "shares", "mcap", "pe", "pb", "logoid")})
        try:
            bars = retry(fetch_history, f"BIST:{t}", BARS)
            p["history"] = series(bars)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"peer {t} history: {e}")
            p["history"] = prev_peers.get(t, {}).get("history", [])
        if p.get("shares") and p.get("history"):
            p["mcap"] = p["shares"] * p["history"][-1][1]
        log("peer", t, len(p["history"]), p.get("mcap"))
    calendar = [d for d, _ in benchmarks["XU100"]["history"]] if "XU100" in benchmarks else []

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
                "logoid": f["logoid"],
            })
        else:
            for k in ("shares", "mcap", "pe", "pb", "tv_sector", "tv_industry", "logoid"):
                item[k] = old.get(k)

        # keep market cap consistent with the latest price
        if item.get("shares") and item.get("history"):
            item["mcap"] = item["shares"] * item["history"][-1][1]

        # hourly bars for the 1-week views
        try:
            hb = retry(fetch_history, f"BIST:{t}", INTRADAY_BARS, resolution="60")
            tz = dt.timezone(dt.timedelta(hours=3))
            item["intraday"] = [[dt.datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%dT%H:%M"), round(c, 4)]
                                for ts, c in hb]
        except Exception as e:  # noqa: BLE001
            warnings.append(f"{t} intraday: {e}")
            item["intraday"] = old.get("intraday", [])

        # valuation vs sector index constituents
        group = member_data.get(c["bench"])
        if group is not None:
            label = BENCHMARKS[c["bench"]]
            if t in EXCLUDE_BANKS_FOR:
                group = [g for g in group if g["t"] not in bank_set]
                label += " (bankalar hariç)"
            item["peer_label"] = label
            item["peer_count"] = len(group)
            item["sector_pe"] = weighted(group, "pe")
            item["sector_pb"] = weighted(group, "pb")
            ranked = [m["t"] for m in member_data[c["bench"]]]
            item["index_rank"] = ranked.index(t) + 1 if t in ranked else None
        else:
            for k in ("peer_label", "peer_count", "sector_pe", "sector_pb", "index_rank"):
                item[k] = old.get(k)
        # peer index (XRAKIP)
        plist = PEERS.get(t, [])
        pdata = {p: peers[p] for p in plist if p in peers and peers[p].get("history")}
        hist, w = peer_index(pdata, calendar, PEER_CAP)
        item["peer_index"] = {
            "code": "XRAKIP", "name": f"{c['name']} rakip endeksi", "cap": PEER_CAP,
            "history": hist,
            "members": [[p, peers[p]["name"], peers[p].get("mcap"), round(w.get(p, 0), 4)] for p in plist if p in peers],
        } if hist else old.get("peer_index")
        item["peers"] = plist
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
        "peers": {t: {k: v for k, v in p.items()} for t, p in peers.items()},
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
