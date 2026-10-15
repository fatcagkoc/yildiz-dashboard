"""
Gün sonu raporu (rule-based, no AI, no cost).

Reads data.json (written by fetch_data.py), builds a short Turkish summary and
  - always writes rapor.html (latest summary, published with the site)
  - e-mails it once per trading day when run between 18:30 and 19:40 Istanbul time AND the
    closing prices are final (see close_final), if the mail secrets exist.
    cron-job.org triggers a run at 18:35 (primary); the regular 18:50 run is the fallback.

Secrets (GitHub > Settings > Secrets and variables > Actions):
  MAIL_USER  Gmail address that sends the mail
  MAIL_PASS  Gmail app password (16 characters)
  MAIL_TO    recipients, comma separated
Manual test: Actions > "Verileri guncelle ve yayinla" > Run workflow > tick "test_report".
"""
import datetime as dt
import json
import os
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data.json")
STATE = os.path.join(HERE, "report_state.json")
PAGE = os.path.join(HERE, "rapor.html")
PANEL = "https://fatcagkoc.github.io/yildiz-dashboard/"
IST = dt.timezone(dt.timedelta(hours=3))
SEND_FROM, SEND_UNTIL = (18, 30), (19, 40)
# "table": wide table, company column frozen while scrolling sideways (works where the mail app supports it:
#          iPhone Mail and the web copy; Outlook/Gmail apps scroll the whole table or shrink it).
# "cards": one card per company, no sideways scrolling at all (works in every mail app).
COMPANY_LAYOUT = "table"
INDEX_HTML = os.path.join(HERE, "index.html")

UP, DOWN, MUTED, LINE, TEXT, BG = "#0b7a35", "#b3261e", "#6b7686", "#e3e6eb", "#0e1726", "#f3f4f6"


# ---------- number formatting (Turkish) ----------
def num(v, d=1):
    s = f"{abs(v):,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return s


def pct(v, d=1):
    if v is None:
        return "–"
    return ("+" if v > 0 else "−" if v < 0 else "") + num(v, d) + "%"


def pts(v):
    if v is None:
        return "–"
    return ("+" if v > 0 else "−" if v < 0 else "") + num(v, 1) + " puan"


def money_tl(v):
    return num(v / 1e9, 1) + " mlr TL" if abs(v) >= 1e9 else num(v / 1e6, 0) + " mn TL"


def ek(n):
    """Turkish possessive suffix for a small integer: 1'i, 2'si, 3'ü, 6'sı, 0'ı."""
    return {0: "'ı", 1: "'i", 2: "'si", 3: "'ü", 4: "'ü", 5: "'i", 6: "'sı", 7: "'si", 8: "'i", 9: "'u"}[n % 10] if n < 10 else "'i"


def color(v):
    return UP if (v or 0) > 0 else DOWN if (v or 0) < 0 else MUTED


def tr_date(iso):
    y, m, d = iso[:10].split("-")
    return f"{d}.{m}.{y}"


# ---------- series helpers ----------
def at_or_before(hist, day):
    v = None
    for d, x in hist:
        if d <= day:
            v = x
        else:
            break
    return v


def shift_month(iso, months):
    y, m, d = map(int, iso[:10].split("-"))
    m0 = m - 1 - months
    y += m0 // 12
    m0 %= 12
    import calendar
    d = min(d, calendar.monthrange(y, m0 + 1)[1])
    return dt.date(y, m0 + 1, d).isoformat()


def returns(hist):
    """1D on the last bar, 1M and YTD like the panel (base = last close on/before the base date)."""
    if not hist or len(hist) < 2:
        return {}
    last_d, last = hist[-1]
    r = {"1D": (last / hist[-2][1] - 1) * 100}
    b = at_or_before(hist, shift_month(last_d, 1))
    r["1M"] = (last / b - 1) * 100 if b else None
    b = at_or_before(hist, f"{int(last_d[:4]) - 1}-12-31")
    r["YTD"] = (last / b - 1) * 100 if b else None
    return r


def cut(hist, day):
    return [r for r in hist if r[0] <= day]


def streak(hist):
    n, sign = 0, 0
    for i in range(len(hist) - 1, 0, -1):
        s = 1 if hist[i][1] > hist[i - 1][1] else -1 if hist[i][1] < hist[i - 1][1] else 0
        if s == 0 or (sign and s != sign):
            break
        sign = s
        n += 1
    return n * sign


# ---------- build ----------
def build(D):
    comps = D["companies"]
    bm = D["benchmarks"]
    day = comps[0]["history"][-1][0]                       # report day = last trading day in the data
    rows, flags = [], []
    beat = 0
    g_now = g_prev = 0.0
    for c in comps:
        h = cut(c["history"], day)
        r = returns(h)
        b = returns(cut(bm[c["bench"]]["history"], day))
        pi = c.get("peer_index")
        p = returns(cut(pi["history"], day)) if pi and pi.get("history") else None
        diff = r["1D"] - b["1D"] if r.get("1D") is not None and b.get("1D") is not None else None
        if diff is not None and diff > 0:
            beat += 1
        g_now += h[-1][1] * c["shares"]
        g_prev += h[-2][1] * c["shares"]
        rows.append({"t": c["ticker"], "name": c["name"], "close": h[-1][1], "r": r, "bench": c["bench"], "has_peer": bool(p),
                     "b": b, "diff": diff, "p": p, "mcap": h[-1][1] * c["shares"]})

        # --- highlights (rule-based): one sentence per company, most notable first ---
        facts, w = [], 0
        d1 = r.get("1D")
        last63 = [abs(h[i][1] / h[i - 1][1] - 1) * 100 for i in range(max(1, len(h) - 63), len(h))]
        if d1 is not None and abs(d1) >= 3:
            top = last63 and abs(d1) >= max(last63) - 1e-9
            facts.append(f"günü {pct(d1)} ile kapattı" + (f" (son 3 ayın en büyük günlük {'yükselişi' if d1 > 0 else 'düşüşü'})" if top else ""))
            w = max(w, abs(d1) + 10)
        y1 = [v for d, v in h if d > shift_month(day, 12)]
        if len(y1) > 100:
            if h[-1][1] >= max(y1) - 1e-9:
                facts.append(f"son 52 haftanın en yüksek kapanışında ({num(h[-1][1], 2)} TL)")
                w = max(w, 9)
            elif h[-1][1] <= min(y1) + 1e-9:
                facts.append(f"son 52 haftanın en düşük kapanışında ({num(h[-1][1], 2)} TL)")
                w = max(w, 9)
        if diff is not None and abs(diff) >= 2:
            facts.append(f"sektör endeksini {num(diff, 1)} puan farkla geçti" if diff > 0
                         else f"sektör endeksinin {num(diff, 1)} puan gerisinde kaldı")
            w = max(w, abs(diff) + 2)
        pdiff = (d1 - p["1D"]) if p and d1 is not None and p.get("1D") is not None else None
        if pdiff is not None and abs(pdiff) >= 2:
            facts.append(f"rakip endeksini {num(pdiff, 1)} puan farkla geçti" if pdiff > 0
                         else f"rakip endeksinin {num(pdiff, 1)} puan gerisinde kaldı")
            w = max(w, abs(pdiff) + 1.5)
        s = streak(h)
        if abs(s) >= 4:
            facts.append(f"üst üste {abs(s)} gündür {'yükseliyor' if s > 0 else 'düşüyor'}")
            w = max(w, 5)
        if facts:
            flags.append((w, f"<b>{c['ticker']}</b> " + "; ".join(facts) + "."))
    flags.sort(key=lambda x: -x[0])

    xu = returns(cut(bm["XU100"]["history"], day))
    g1 = (g_now / g_prev - 1) * 100 if g_prev else None
    best = max(rows, key=lambda x: x["r"].get("1D", -1e9))
    worst = min(rows, key=lambda x: x["r"].get("1D", 1e9))
    fxh = cut(D["fx"]["history"], day)
    fx1 = (fxh[-1][1] / fxh[-2][1] - 1) * 100 if len(fxh) > 1 else None

    # Gözde NAV discount (statement equity valid from its KAP date)
    nav = None
    n = (D.get("nav") or {}).get("GOZDE")
    gz = next((c for c in comps if c["ticker"] == "GOZDE"), None)
    if n and gz:
        eff = n.get("equity_effective", {})
        log = sorted((eff.get(k, k), v) for k, v in n.get("equity_log", {}).items())
        def disc(d):
            e = None
            for k, v in log:
                if k <= d:
                    e = v
            p = at_or_before(gz["history"], d)
            return (1 - p * gz["shares"] / e) * 100 if e and p else None
        h = cut(gz["history"], day)
        nav = {"now": disc(day), "prev": disc(h[-2][0]), "m1": disc(shift_month(day, 1))}

    # global food (last common close of US/EU series)
    G = D.get("global") or {}
    glob = None
    ix = G.get("indices", {})
    if "FTXG" in ix and "SX3P" in ix:
        glob = {"ftxg": returns(ix["FTXG"]["history"]), "ftxg_d": ix["FTXG"]["history"][-1][0],
                "sx3p": returns(ix["SX3P"]["history"]), "sx3p_d": ix["SX3P"]["history"][-1][0]}

    return {"day": day, "rows": rows, "flags": [t for _, t in flags[:6]], "xu": xu, "g_now": g_now, "g1": g1,
            "beat": beat, "best": best, "worst": worst, "fx": fxh[-1][1], "fx1": fx1, "nav": nav, "glob": glob,
            "updated_at": D.get("updated_at")}


# ---------- is today's close final? ----------
def close_final(D, now):
    """True when every company has today's daily bar AND an hourly bar from 18:00 (the closing
    auction ends ~18:10; with the ~15 min data delay it shows up after ~18:25), the daily close
    equals that last hourly close, and it is at least 18:30 Istanbul time."""
    today = now.date().isoformat()
    if (now.hour, now.minute) < (18, 30):
        return False, "saat 18:30'dan önce"
    for c in D["companies"]:
        if not c.get("history") or c["history"][-1][0] != today:
            return False, f"{c['ticker']} için bugünün kapanışı yok"
        last_h = (c.get("intraday") or [[None, None]])[-1]
        if not last_h[0] or not last_h[0].startswith(today + "T18"):
            return False, f"{c['ticker']} için 18:00 sonrası işlem verisi yok"
        if abs(last_h[1] - c["history"][-1][1]) > 0.005:
            return False, f"{c['ticker']} günlük ve saatlik kapanış farklı"
    return True, "kapanış kesinleşti"


# ---------- render (mobile-first, e-mail safe: tables + inline styles, no horizontal scroll) ----------
PANEL_BTN = "#2a6fd0"
CARD = "#ffffff"
SOFT = "#f6f7f9"


def pill(v):
    bg = "#e2f4e8" if (v or 0) > 0 else "#fbe9e7" if (v or 0) < 0 else SOFT
    return (f"<span style='display:inline-block;padding:3px 8px;border-radius:6px;background:{bg};"
            f"color:{color(v)};font-weight:700;font-size:14px;white-space:nowrap'>{pct(v)}</span>")


def lab(label):   # label row (bottom-aligned so the values below line up even when a label wraps)
    return f"<td width='25%' valign='bottom' style='padding:0 4px 2px 0;font-size:10.5px;color:{MUTED};line-height:1.3'>{label}</td>"


def val(value_html):
    return f"<td width='25%' valign='top' style='padding:0 4px 0 0;font-size:13px;font-weight:600;white-space:nowrap'>{value_html}</td>"


def tile(label, value, sub):
    return (f"<td width='33%' valign='top' style='padding:10px 8px;background:{SOFT};border-radius:8px'>"
            f"<div style='font-size:11px;color:{MUTED}'>{label}</div>"
            f"<div style='font-size:17px;font-weight:700;margin-top:2px;white-space:nowrap'>{value}</div>"
            f"<div style='font-size:12px;margin-top:1px'>{sub}</div></td>")


def render(R, now, final, logo_src, final_note):
    day = tr_date(R["day"])
    title = f"Gün sonu · {day}" if final else f"Gün içi durum · {day}"
    ce = lambda v: f"<span style='color:{color(v)}'>{pct(v)}</span>"
    tiles = (tile("Grup piyasa değeri", money_tl(R["g_now"]), ce(R["g1"]) + " bugün")
             + "<td width='8' style='font-size:0'>&nbsp;</td>"
             + tile("BIST 100", ce(R["xu"].get("1D")), f"1 ay {ce(R['xu'].get('1M'))}")
             + "<td width='8' style='font-size:0'>&nbsp;</td>"
             + tile("Sektörünü geçen", f"{R['beat']} / {len(R['rows'])}", "şirket"))
    lead = (f"Günün en iyi performansı <b>{R['best']['t']}</b> ({ce(R['best']['r']['1D'])}), "
            f"en düşük performansı <b>{R['worst']['t']}</b> ({ce(R['worst']['r']['1D'])}).")
    flags = "".join(f"<tr><td valign='top' style='padding:0 8px 8px 0;color:{MUTED}'>•</td>"
                    f"<td style='padding:0 0 8px;font-size:14px;line-height:1.45'>{f}</td></tr>" for f in R["flags"]) or \
        f"<tr><td style='font-size:14px;color:{MUTED}'>Bugün belirlenen eşikleri aşan bir hareket yok.</td></tr>"
    cards = ""
    pv = lambda v: f"<span style='color:{color(v)}'>{pts(v).replace(' puan', ' p')}</span>" if v is not None else f"<span style='color:{MUTED}'>–</span>"
    # --- table layout: frozen company column ---
    fz = f"position:sticky;left:0;z-index:1;background:{CARD};box-shadow:1px 0 0 {LINE};"
    thc = f"padding:8px 7px;font-size:11px;line-height:1.25;color:{MUTED};font-weight:600;text-align:right;vertical-align:bottom;border-bottom:1px solid {LINE};background:{CARD}"
    tdc = f"padding:10px 7px;text-align:right;white-space:nowrap;border-bottom:1px solid {LINE};font-size:13.5px"
    trs = ""
    for x in R["rows"]:
        d1 = x["r"].get("1D")
        pdiff = (d1 - x["p"]["1D"]) if x["p"] and d1 is not None and x["p"].get("1D") is not None else None
        trs += (f"<tr><td style='{fz}padding:10px 10px 10px 0;border-bottom:1px solid {LINE};min-width:96px'>"
                f"<div style='font-size:14px;font-weight:700'>{x['t']}</div><div style='font-size:11.5px;color:{MUTED};white-space:nowrap'>{x['name']}</div></td>"
                f"<td style='{tdc}'>{num(x['close'], 2)}</td>"
                f"<td style='{tdc}'>{pill(d1)}</td>"
                f"<td style='{tdc}'>{pv(x['diff'])}<div style='font-size:11px;color:{MUTED}'>{x['bench']} {pct(x['b'].get('1D'))}</div></td>"
                f"<td style='{tdc}'>{pv(pdiff)}</td>"
                f"<td style='{tdc}'>{ce(x['r'].get('1M'))}</td>"
                f"<td style='{tdc}'>{ce(x['r'].get('YTD'))}</td></tr>")
    table_html = (f"<div style='overflow-x:auto;-webkit-overflow-scrolling:touch'>"
                  f"<table cellspacing='0' cellpadding='0' role='presentation' style='border-collapse:separate;border-spacing:0;width:100%;min-width:560px'>"
                  f"<tr><th style='{fz}{thc};text-align:left;padding-left:0'>Şirket</th><th style='{thc}'>Kapanış (TL)</th><th style='{thc}'>Bugün</th>"
                  f"<th style='{thc}'>Sektöre göre</th><th style='{thc}'>Rakiplere göre</th><th style='{thc}'>1 ay</th><th style='{thc}'>Yılbaşından beri</th></tr>"
                  f"{trs}</table></div>"
                  f"<div style='font-size:11.5px;color:{MUTED};margin-top:6px'>Telefonda tabloyu yana kaydırabilirsiniz.</div>")
    for x in R["rows"]:
        d1 = x["r"].get("1D")
        pdiff = (d1 - x["p"]["1D"]) if x["p"] and d1 is not None and x["p"].get("1D") is not None else None
        cards += f"""<tr><td style='padding:12px 0;border-top:1px solid {LINE}'>
<table width='100%' cellspacing='0' cellpadding='0' role='presentation'><tr>
<td valign='middle'><div style='font-size:15px;font-weight:700'>{x['t']}</div><div style='font-size:12px;color:{MUTED}'>{x['name']}</div></td>
<td valign='middle' align='right' style='white-space:nowrap'><span style='font-size:14px;margin-right:8px'>{num(x['close'], 2)} TL</span>{pill(d1)}</td>
</tr></table>
<table width='100%' cellspacing='0' cellpadding='0' role='presentation' style='margin-top:6px'>
<tr>{lab(f"Sektöre göre ({x['bench']})")}{lab("Rakiplere göre")}{lab("1 ay")}{lab("Yılbaşından beri")}</tr>
<tr>{val(pv(x['diff']))}{val(pv(pdiff))}{val(ce(x['r'].get('1M')))}{val(ce(x['r'].get('YTD')))}</tr>
</table></td></tr>"""
    other = []
    if R["glob"]:
        g = R["glob"]
        other.append(("Global gıda (son kapanış)",
                      f"ABD gıda ve içecek fonu {ce(g['ftxg'].get('1D'))} <span style='color:{MUTED}'>({tr_date(g['ftxg_d'])})</span><br>"
                      f"Avrupa gıda ve içecek endeksi {ce(g['sx3p'].get('1D'))} <span style='color:{MUTED}'>({tr_date(g['sx3p_d'])})</span>"))
    if R["nav"] and R["nav"]["now"] is not None:
        nv = R["nav"]
        ch = nv["now"] - nv["prev"] if nv["prev"] is not None else None
        m1 = nv["now"] - nv["m1"] if nv["m1"] is not None else None
        parts = []
        if ch is not None and abs(ch) >= 0.05:
            parts.append(f"bugün {num(ch, 1)} puan {'açıldı' if ch > 0 else 'daraldı'}")
        if m1 is not None:
            parts.append(f"son 1 ayda {num(m1, 1)} puan {'açıldı' if m1 > 0 else 'daraldı'}")
        other.append(("Gözde NAD iskontosu", f"<b>%{num(nv['now'], 0)}</b>" + (" · " + " · ".join(parts) if parts else "")))
    other.append(("USD/TRY", f"<b>{num(R['fx'], 2)}</b> {ce(R['fx1'])}".replace(pct(R['fx1']), pct(R['fx1'], 2))))
    other_html = "".join(f"<tr><td style='padding:10px 0;border-top:1px solid {LINE}'><div style='font-size:11.5px;color:{MUTED}'>{k}</div>"
                         f"<div style='font-size:14px;margin-top:2px;line-height:1.5'>{v}</div></td></tr>" for k, v in other)
    upd = dt.datetime.fromisoformat(R["updated_at"]).astimezone(IST).strftime("%d.%m.%Y %H:%M") if R.get("updated_at") else "–"
    stale = ""
    if R.get("updated_at") and (now - dt.datetime.fromisoformat(R["updated_at"])).total_seconds() > 2 * 3600:
        stale = (f"<p style='background:#fbe9e7;color:{DOWN};padding:8px 10px;border-radius:6px;font-size:13px;margin:0 0 12px'>"
                 f"Veri en son {upd}'de güncellendi; son otomatik güncelleme çalışmamış olabilir.</p>")
    status = (f"<div style='display:inline-block;margin-top:6px;font-size:12px;color:{UP};background:#e2f4e8;padding:3px 8px;border-radius:6px'>✓ {final_note}</div>"
              if final else f"<div style='display:inline-block;margin-top:6px;font-size:12px;color:#8a5a00;background:#fff2d9;padding:3px 8px;border-radius:6px'>Gün içi veri · kapanış henüz kesinleşmedi</div>")
    h2 = lambda t: f"<div style='font-size:12px;font-weight:700;letter-spacing:.08em;color:{MUTED};margin:22px 0 8px;text-transform:uppercase'>{t}</div>"
    body = f"""<div style="background:{BG};padding:16px 10px;font-family:-apple-system,'Segoe UI',Roboto,Arial,sans-serif;color:{TEXT}">
<table align="center" width="100%" cellspacing="0" cellpadding="0" role="presentation" style="max-width:600px;margin:0 auto;background:{CARD};border:1px solid {LINE};border-radius:12px;table-layout:fixed">
<tr><td style="padding:18px 16px 20px">
<table cellspacing="0" cellpadding="0" role="presentation"><tr>
<td valign="middle" style="padding-right:10px"><img src="{logo_src}" width="32" height="32" alt="Yıldız" style="display:block;border:0"></td>
<td valign="middle"><div style="font-size:11px;letter-spacing:.12em;color:{MUTED};font-weight:600">YILDIZ HOLDİNG · BORSA PANELİ</div>
<div style="font-size:20px;font-weight:700;line-height:1.25">{title}</div></td></tr></table>
{status}
<div style="height:14px"></div>{stale}
<table width="100%" cellspacing="0" cellpadding="0" role="presentation"><tr>{tiles}</tr></table>
<p style="font-size:15px;line-height:1.5;margin:14px 0 0">{lead}</p>
{h2("Dikkat çekenler")}
<table cellspacing="0" cellpadding="0" role="presentation">{flags}</table>
{h2("Şirketler")}
{table_html if COMPANY_LAYOUT == "table" else f'<table width="100%" cellspacing="0" cellpadding="0" role="presentation">{cards}</table>'}
<p style="font-size:11.5px;color:{MUTED};margin:6px 0 0;line-height:1.45">p = puan. Sektöre / rakiplere göre: şirketin bugünkü getirisi ile sektör endeksinin / rakip endeksinin getirisi arasındaki fark. Ülker ve Makina Takım için rakip endeksi yok.</p>
{h2("Diğer")}
<table width="100%" cellspacing="0" cellpadding="0" role="presentation">{other_html}</table>
<table cellspacing="0" cellpadding="0" role="presentation" style="margin-top:20px"><tr><td style="background:{PANEL_BTN};border-radius:8px">
<a href="{PANEL}" style="display:inline-block;padding:11px 18px;color:#ffffff;text-decoration:none;font-weight:700;font-size:14px">Paneli aç</a></td></tr></table>
<p style="font-size:11.5px;color:{MUTED};margin:18px 0 0;line-height:1.5">Bu rapor panelin robotu tarafından kapanış verisiyle otomatik hazırlanır; yapay zekâ kullanılmaz, metinler sabit kurallarla üretilir. Veri {upd} itibarıyla (TradingView, Yahoo Finance). Bilgi amaçlıdır, yatırım tavsiyesi değildir.</p>
</td></tr></table></div>"""
    subject = f"Yıldız borsa özeti · {day} · Grup {pct(R['g1'])} · BIST 100 {pct(R['xu'].get('1D'))}"
    text = (f"{title}\n\nGrup piyasa değeri {money_tl(R['g_now'])} ({pct(R['g1'])}), BIST 100 {pct(R['xu'].get('1D'))}.\n"
            f"Günün en iyi performansı {R['best']['t']} ({pct(R['best']['r']['1D'])}), en düşük performansı {R['worst']['t']} ({pct(R['worst']['r']['1D'])}).\n\n"
            + "\n".join(f"{x['t']}: {num(x['close'], 2)} TL, {pct(x['r'].get('1D'))} (sektöre göre {pts(x['diff'])})" for x in R["rows"])
            + f"\n\nPanel: {PANEL}\nBilgi amaçlıdır, yatırım tavsiyesi değildir.")
    return subject, body, text


# ---------- logo (the dashboard's Yıldız star) ----------
def logo_png():
    """PNG bytes of the Yıldız star, taken from index.html (apple-touch-icon, else favicon)."""
    import base64, re
    try:
        s = open(INDEX_HTML, encoding="utf-8").read()
        m = re.search(r'rel="apple-touch-icon"[^>]*href="data:image/png;base64,([^"]+)"', s) or \
            re.search(r'rel="icon"[^>]*href="data:image/png;base64,([^"]+)"', s)
        return base64.b64decode(m.group(1)) if m else None
    except Exception:  # noqa: BLE001
        return None


def page(title, body, png):
    import base64
    ico = f"<link rel='icon' type='image/png' href='data:image/png;base64,{base64.b64encode(png).decode()}'>" if png else ""
    return (f"<!doctype html><html lang='tr'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title>{ico}<meta name='robots' content='noindex'></head><body style='margin:0'>{body}</body></html>")


def send(subject, html, text, png, test=False):
    from email.mime.image import MIMEImage
    user, pw, to = os.environ.get("MAIL_USER"), os.environ.get("MAIL_PASS"), os.environ.get("MAIL_TO")
    if not (user and pw and to):
        print("mail secrets missing -> not sending")
        return False
    rcpt = [a.strip() for a in to.split(",") if a.strip()]
    msg = MIMEMultipart("related")
    msg["Subject"] = ("[TEST] " if test else "") + subject
    msg["From"] = formataddr(("Yıldız Borsa Paneli", user))
    msg["To"] = ", ".join(rcpt)
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text, "plain", "utf-8"))
    alt.attach(MIMEText(html, "html", "utf-8"))
    msg.attach(alt)
    if png:   # inline logo (cid:) so it shows without "load remote images"
        img = MIMEImage(png, "png")
        img.add_header("Content-ID", "<yildizlogo>")
        img.add_header("Content-Disposition", "inline", filename="yildiz.png")
        msg.attach(img)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, pw.replace(" ", ""))
        s.sendmail(user, rcpt, msg.as_string())
    print("mail sent to", len(rcpt), "recipient(s)")
    return True


def main():
    import base64
    D = json.load(open(DATA, encoding="utf-8"))
    now = dt.datetime.now(IST)
    R = build(D)
    ok_final, why = close_final(D, now)
    final = R["day"] < now.date().isoformat() or ok_final
    final_note = "Kapanış fiyatları kesinleşti (BIST kapanış seansı 18:10)" if final else why
    png = logo_png()
    web_logo = f"data:image/png;base64,{base64.b64encode(png).decode()}" if png else ""
    subject, body, text = render(R, now, final, web_logo, final_note)
    with open(PAGE, "w", encoding="utf-8") as f:
        f.write(page(subject, body, png))
    print("wrote rapor.html for", R["day"], "| close final:", ok_final, why)

    test = str(os.environ.get("REPORT_TEST", "")).lower() == "true"
    in_window = now.weekday() < 5 and SEND_FROM <= (now.hour, now.minute) <= SEND_UNTIL
    state = {}
    if os.path.exists(STATE):
        try:
            state = json.load(open(STATE, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            state = {}
    mail_body = render(R, now, final, "cid:yildizlogo" if png else "", final_note)[1]
    if test:
        send(subject, mail_body, text, png, test=True)
    elif in_window and ok_final and state.get("last_sent") != R["day"]:
        if send(subject, mail_body, text, png):
            state["last_sent"] = R["day"]
            json.dump(state, open(STATE, "w", encoding="utf-8"))
    else:
        print("not sending:", {"in_window": in_window, "close_final": ok_final, "why": why, "last_sent": state.get("last_sent")})


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        sys.exit(0)   # never break the data pipeline because of the report
