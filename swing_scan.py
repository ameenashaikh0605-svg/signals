"""Daily swing scan for a whole market, sent as one Telegram summary to that market's own bot.

    python swing_scan.py us       S&P 500 stocks (us_stocks.csv), after each US close, buys only
    python swing_scan.py crypto   top coins (crypto_coins.csv), after each daily close (00:00 UTC), buys and sells

Setup (same rule as the swing mode in signal_server.py): the daily Supertrend turns up while price is above
its 200-day average (SELL: turns down while below it). Each setup is sent in two steps:
  1. GET READY: buy above that day's high (sell below its low), valid for the next SETUP_DAYS days.
  2. ENTRY TRIGGERED when price crosses that level. Stop loss 1.5 ATR from the trigger, target at 2x the
     risk (book some profit), then the stop trails 3 ATR from the best close for the rest.
`--live` (run every 30 minutes) sends entry / target / stop alerts as they happen; the daily summary confirms
them. Setups and open trades are kept in <market>_state.json.

Telegram details come from the environment (<MARKET>_TELEGRAM_BOT_TOKEN, and <MARKET>_TELEGRAM_CHAT_ID or
TELEGRAM_CHAT_ID), never from files. Read-only: it never places orders."""
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import signal_server as SS
import strategy as S

HERE = os.path.dirname(os.path.abspath(__file__))
CATCH_UP_DAYS = 5      # after a gap, only act on the last few daily candles (no alert floods)
MAX_DETAILED = 15      # new signals shown in full; the rest are listed by name
SETUP_DAYS = 3         # a GET READY setup expires if price doesn't cross the trigger within this many days
CFG = dict(S.DEFAULTS)

MARKETS = {
    "us": {"title": "🇺🇸 US SWING", "what": "S&P 500 stocks", "list": "us_stocks.csv", "source": "yahoo",
           "sides": (1,), "unit": "shares", "env": "US", "when": "after the US close"},
    "crypto": {"title": "🪙 CRYPTO SWING", "what": "top coins", "list": "crypto_coins.csv", "source": "binance",
               "sides": (1, -1), "unit": "coins", "env": "CRYPTO",
               "when": "twice a day, at 1:30 pm (India session) and 9:30 pm (US session) India time"},
}
MARKETS["main"] = {"title": "📅 SWING", "what": "markets", "list": "config.json", "source": "yahoo",
                   "sides": (1, -1), "unit": "units", "env": "", "cur": "",
                   "when": "after each market's daily close (checked every 30 minutes)"}
CUR = "$"
# Crypto never closes, so it gets two sets of 24-hour candles: one closing at 08:00 UTC (1:30 pm India time)
# and one closing at 16:00 UTC (9:30 pm India time). Each run uses the set that closed most recently.
CRYPTO_SESSIONS = {8: "India session (24h candles to 1:30 pm)", 16: "US session (24h candles to 9:30 pm)"}
IST = 19800


def load_list(m):
    if m["list"] == "config.json":   # the main bot's own markets (Yahoo ones; crypto has its own bot)
        cfg = SS.load_config()
        return [{"symbol": w["code"], "name": w.get("name") or w["code"], "sector": ""} for w in cfg["watch"]
                if SS.guess_source(w["code"]) == "yahoo"]
    with open(os.path.join(HERE, m["list"]), encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r.get("symbol")]


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def retry(fn):
    last = None
    for attempt in range(3):
        try:
            return fn()
        except Exception as e:
            last = e
            time.sleep(2 + 3 * attempt)
    raise RuntimeError(str(last))


def yahoo_daily(symbol, live=False):
    """Closed daily candles. Today's candle counts once the regular session has ended.
    live=True keeps today's still-forming candle at the end (its close is the latest price)."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
           f"?interval=1d&range=2y&includePrePost=false")
    res = retry(lambda: SS.http_json(url)["chart"]["result"][0])
    meta = res.get("meta", {})
    off = meta.get("gmtoffset", 0)
    end = ((meta.get("currentTradingPeriod") or {}).get("regular") or {}).get("end") or 0
    q = (res.get("indicators", {}).get("quote") or [{}])[0]
    ts = res.get("timestamp") or []
    bars = []
    for i, t in enumerate(ts):
        o, h, l, c = (q.get(k, [None] * len(ts))[i] for k in ("open", "high", "low", "close"))
        if None in (o, h, l, c):
            continue
        day = datetime.fromtimestamp(t + off, tz=timezone.utc).strftime("%Y-%m-%d")
        bars.append({"t": t, "o": o, "h": h, "l": l, "c": c, "v": (q.get("volume") or [0] * len(ts))[i] or 0.0,
                     "day": day})
    # the candle of the current session is still forming until that session ends
    end_day = datetime.fromtimestamp(end + off, tz=timezone.utc).strftime("%Y-%m-%d") if end else ""
    if bars and time.time() < end and bars[-1]["day"] >= end_day and not live:
        bars.pop()
    return bars


def binance_daily(symbol, live=False):
    """Closed daily candles (UTC days), about 2 years. live=True keeps today's forming candle."""
    rows = retry(lambda: SS.BinanceSource()._get(f"/klines?symbol={symbol}&interval=1d&limit=800"))
    now_ms = int(time.time() * 1000)
    return [{"t": r[0] // 1000, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
             "v": float(r[5]), "day": datetime.fromtimestamp(r[0] // 1000, tz=timezone.utc).strftime("%Y-%m-%d")}
            for r in rows if live or r[6] < now_ms]


def binance_anchored(symbol, anchor_h):
    """Closed 24-hour candles that start and end at anchor_h:00 UTC, built from 4-hour candles (~2 years)."""
    src, rows, start = SS.BinanceSource(), [], int((time.time() - 820 * 86400) * 1000)
    while True:
        chunk = retry(lambda: src._get(f"/klines?symbol={symbol}&interval=4h&startTime={start}&limit=1000"))
        rows += chunk
        if len(chunk) < 1000:
            break
        start = chunk[-1][0] + 1
    now_ms = int(time.time() * 1000)
    groups = {}
    for r in rows:
        t = r[0] // 1000
        groups.setdefault(t - (t - anchor_h * 3600) % 86400, []).append(r)
    bars = []
    for t0 in sorted(groups):
        g = groups[t0]
        if len(g) != 6 or g[-1][6] >= now_ms:   # incomplete or still forming
            continue
        end = datetime.fromtimestamp(t0 + 86400 + IST, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        bars.append({"t": t0, "o": float(g[0][1]), "h": max(float(x[2]) for x in g),
                     "l": min(float(x[3]) for x in g), "c": float(g[-1][4]), "v": sum(float(x[5]) for x in g),
                     "day": end})
    return bars


def binance_since(symbol, since_t):
    """High, low and latest price since `since_t` (unix seconds), from 1-hour candles: one bar dict."""
    rows = retry(lambda: SS.BinanceSource()._get(f"/klines?symbol={symbol}&interval=1h&limit=72"))
    rows = [r for r in rows if r[0] // 1000 >= since_t] or rows[-1:]
    return {"h": max(float(r[2]) for r in rows), "l": min(float(r[3]) for r in rows), "c": float(rows[-1][4])}


def swings(bars, i, kind, lookback=250, w=5):
    """Swing lows (kind='l') or highs ('h') before bar i: (index, price), oldest first."""
    out = []
    for k in range(max(w, i - lookback), i - w + 1):
        v = bars[k][kind]
        win = [bars[x][kind] for x in range(k - w, min(k + w + 1, i + 1))]
        if v == (min(win) if kind == "l" else max(win)):
            out.append((k, v))
    return out


def checklist(bars, ind, i, side, trigger, risk):
    """Every factor behind a setup as (mark, text). ✓ supports the trade, ✗ doesn't, ⚠️ is a warning.
    The first two are the rule itself; the rest are extra checks used to rank setups."""
    up = side == 1
    d = "up" if up else "down"
    a, c = ind["atr"][i], bars[i]["c"]
    out = [("✓", f"Supertrend turned {d}"),
           ("✓", f"price {'above' if up else 'below'} its 200-day average")]
    td = S.trend_dir(ind, i) == side
    out.append(("✓" if td else "✗", f"20 and 50-day averages {'point ' + d if td else 'not lined up yet'}"))
    adx = ind["adx"][i]
    out.append(("✓" if adx >= CFG["adx_min"] else "✗",
                f"{'strong trend' if adx >= CFG['adx_min'] else 'weak/choppy trend'} (ADX {adx:.0f})"))
    if ind["has_vol"] and ind["vavg"][i - 1]:
        vr = ind["v"][i] / ind["vavg"][i - 1]
        out.append(("✓" if vr >= 1.2 else "✗", f"volume {vr:.1f}x normal"))
    # support: did the recent low (high for sells) bounce off an earlier swing low (high)?
    kind = "l" if up else "h"
    recent = range(max(0, i - 15), i + 1)
    r_idx = (min if up else max)(recent, key=lambda k: bars[k][kind])
    lvl = bars[r_idx][kind]
    # an earlier swing low 10-120 candles before, within half a normal day's move of the recent low
    held = [(k, v) for k, v in swings(bars, r_idx - 10, kind, 110) if abs(v - lvl) <= 0.5 * a]
    moved = (c - lvl) * side >= a
    if held and moved:
        k, v = held[-1]
        out.append(("✓", f"bounced off an earlier low near {px(v)} ({bars[k]['day'][:10]}): buyers defended it"
                    if up else f"turned down at an earlier high near {px(v)} ({bars[k]['day'][:10]}): "
                               f"sellers defended it"))
    else:
        out.append(("✗", f"no bounce off an earlier {'support' if up else 'resistance'} level"))
    # room: is there an old high (low) in the way before the target?
    other = "h" if up else "l"
    target = trigger + side * CFG["target_r"] * risk
    # major old highs (lows) only, and ignore ones just above the entry (price is about to clear them)
    block = [v for _, v in swings(bars, i, other, 250, w=10) if (v - trigger) * side > 0.5 * risk]
    if block:
        nxt = min(block) if up else max(block)
        room = (nxt - trigger) * side / risk if risk else 0
        if (nxt - target) * side >= 0:
            out.append(("✓", f"room to the target: next {'resistance' if up else 'support'} {px(nxt)} "
                             f"is beyond it"))
        else:
            out.append(("⚠️", f"{'resistance' if up else 'support'} at {px(nxt)} comes before the target "
                              f"({room:.1f}x the risk away): price may stall there"))
    else:
        out.append(("✓", f"no old {'high' if up else 'low'} in the way (at a {'1-year high' if up else '1-year low'})"))
    return out


def news_check(query, side):
    """Recent headlines (last 3 days) for a stock or coin from Google News, with a mood mark."""
    import news as N
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query + " when:3d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (signal program news reader)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            items = [N.tag(x) for x in N.parse(r.read(), "Google News")]
    except Exception as e:
        return ("–", f"news not checked ({str(e)[:40]})"), []
    items.sort(key=lambda x: -(x["t"] or 0))
    top = items[:2]
    if not items:
        return ("–", "no news in the last 3 days"), []
    ups = sum(1 for x in items[:8] if x["mood"] == "up")
    downs = sum(1 for x in items[:8] if x["mood"] == "down")
    good = ups > downs if side == 1 else downs > ups
    bad = downs > ups if side == 1 else ups > downs
    mark = "✓" if good else "⚠️" if bad else "–"
    mood = "mostly positive" if ups > downs else "mostly negative" if downs > ups else "mixed"
    return (mark, f"news {mood} ({len(items)} stories in 3 days)"), [x["title"] for x in top]


def earnings_dates(days=10):
    """US results (earnings) dates for the next few days from Nasdaq's calendar: {symbol: 'YYYY-MM-DD'}."""
    out = {}
    for n in range(days):
        day = time.strftime("%Y-%m-%d", time.gmtime(time.time() + n * 86400))
        try:
            req = urllib.request.Request(f"https://api.nasdaq.com/api/calendar/earnings?date={day}",
                                         headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                rows = (json.loads(r.read().decode()).get("data") or {}).get("rows") or []
        except Exception as e:
            print("earnings calendar:", day, str(e)[:60])
            continue
        for x in rows:
            out.setdefault(str(x.get("symbol", "")).upper().replace(".", "-"), day)
    return out


def enrich(m, events):
    """Adds news and (US) results-date checks to new setups."""
    setups = [e for e in events if e["kind"] == "setup"]
    if not setups:
        return
    cal = earnings_dates() if m["env"] == "US" else None
    for e in setups:
        q = {"US": f'"{e["name"]}" stock', "CRYPTO": f'"{e["name"]}" crypto'}.get(m["env"], f'"{e["name"].title()}"')
        mark, heads = news_check(q, e["side"])
        e["checks"].append(mark)
        e["headlines"] = heads
        if cal is not None:
            when = cal.get(e["symbol"])
            if when:
                n = (datetime.strptime(when, "%Y-%m-%d") - datetime.utcnow()).days + 1
                e["checks"].append(("⚠️", f"results due {when} (in {max(n, 0)} days): price can jump either way"))
            elif cal:
                e["checks"].append(("✓", "no results due in the next 10 days"))
            else:
                e["checks"].append(("–", "results dates not checked"))
        e["score"] = sum(1 for mk, _ in e["checks"][2:] if mk == "✓")
        e["of"] = len(e["checks"]) - 2


def reasons(ind, i, side):
    d = "up" if side == 1 else "down"
    out = [f"Supertrend turned {d} and price is {'above' if side == 1 else 'below'} its 200-day average"]
    if S.trend_dir(ind, i) == side:
        out.append(f"20 and 50-day averages point {d}")
    if ind["adx"][i] >= CFG["adx_min"]:
        out.append(f"strong trend, not choppy (ADX {ind['adx'][i]:.0f})")
    if ind["has_vol"] and ind["vavg"][i - 1] and ind["v"][i] >= 1.2 * ind["vavg"][i - 1]:
        out.append(f"volume {ind['v'][i] / ind['vavg'][i - 1]:.1f}x normal")
    return out


def px(x):
    if x >= 1:
        return f"{CUR}{x:,.2f}"
    return f"{CUR}{x:.{max(4, 3 - int(f'{x:e}'.split('e')[1]))}f}".rstrip("0")


def target_of(tr):
    return tr.get("target") or tr["entry"] + tr.get("side", 1) * CFG["target_r"] * tr["risk"]


def step(stock, bars, row, first, events, sides, tkey="last_t"):
    """Walk through every daily candle closed since the last run. Updates `row` and appends to `events`.
    `tkey` names the field holding the last candle seen (crypto keeps one per session)."""
    sym = stock["symbol"]
    if len(bars) <= CFG["ema_trend"] + 2:
        return
    last = row.get(tkey)
    if first or last is None:
        row[tkey] = bars[-1]["t"]
        return
    cutoff = bars[-1]["t"] - CATCH_UP_DAYS * 86400
    todo = [j for j, b in enumerate(bars) if b["t"] > last and b["t"] >= cutoff]
    if not todo:
        return
    ind = S.indicators(bars, CFG)
    ind["v"] = [b["v"] or 0.0 for b in bars]
    base = {"symbol": sym, "name": stock["name"], "sector": stock.get("sector", "")}
    for j in todo:
        b, a = bars[j], ind["atr"][j]
        tr, pend = row.get("trade"), row.get("pending")
        if tr:
            side = tr.get("side", 1)
            if (side == 1 and b["l"] <= tr["stop"]) or (side == -1 and b["h"] >= tr["stop"]):
                exit_px = min(b["o"], tr["stop"]) if side == 1 else max(b["o"], tr["stop"])  # gaps fill at the open
                r = side * (exit_px - tr["entry"]) / tr["risk"] if tr["risk"] else 0
                events.append(dict(base, kind="exit", side=side, entry=tr["entry"], exit=exit_px, r=r,
                                   trail=side * (tr["stop"] - tr["entry"]) > 0, day=b["day"],
                                   days=len([x for x in bars if tr["day"] < x["day"] <= b["day"]])))
                row["trade"] = None
                continue
            tgt = target_of(tr)
            if not tr.get("target_hit") and ((side == 1 and b["h"] >= tgt) or (side == -1 and b["l"] <= tgt)):
                tr["target_hit"] = True
                events.append(dict(base, kind="target", side=side, entry=tr["entry"], target=tgt, day=b["day"]))
            tr["best"] = max(tr["best"], b["c"]) if side == 1 else min(tr["best"], b["c"])
            trail = tr["best"] - side * CFG["trail_atr"] * a
            tr["stop"] = max(tr["stop"], trail) if side == 1 else min(tr["stop"], trail)
            continue
        if pend:
            side = pend["side"]
            if (side == 1 and b["h"] >= pend["trigger"]) or (side == -1 and b["l"] <= pend["trigger"]):
                entry = max(b["o"], pend["trigger"]) if side == 1 else min(b["o"], pend["trigger"])  # gap: open
                stop = entry - side * pend["risk"]
                row["trade"] = {"side": side, "entry": entry, "stop": stop, "risk": pend["risk"], "best": entry,
                                "day": b["day"], "target": entry + side * CFG["target_r"] * pend["risk"],
                                "opened_t": b["t"]}
                row["pending"] = None
                events.append(dict(base, kind="entry", side=side, entry=entry, stop=stop,
                                   target=row["trade"]["target"], day=b["day"]))
                continue
            if b["t"] - pend.get("t", b["t"] - 86400) >= SETUP_DAYS * 86400:
                row["pending"] = None
                events.append(dict(base, kind="expired", side=side, trigger=pend["trigger"], day=b["day"]))
            else:
                continue
        side, why = S.signal_at(bars, ind, j, CFG, "supertrend", False)
        if side not in sides:
            continue
        trigger = b["h"] if side == 1 else b["l"]
        risk = CFG["stop_atr"] * a
        row["pending"] = {"side": side, "trigger": trigger, "risk": risk, "day": b["day"], "t": b["t"]}
        events.append(dict(base, kind="setup", side=side, trigger=trigger, stop=trigger - side * risk,
                           target=trigger + side * CFG["target_r"] * risk, risk=risk, adx=ind["adx"][j],
                           day=b["day"], why=reasons(ind, j, side),
                           checks=checklist(bars, ind, j, side, trigger, risk)))
    row[tkey] = bars[-1]["t"]


def size(entry, stop, risk_usd):
    per = abs(entry - stop)
    return risk_usd / per if per > 0 else 0


def qty(x):
    return f"{x:,.0f}" if x >= 100 else f"{x:.2f}" if x >= 0.01 else f"{x:.6f}"


def setup_text(m, e, risk_usd):
    side = e["side"]
    word = "buy above" if side == 1 else "sell/short below"
    pct = lambda x: abs(x - e["trigger"]) / e["trigger"] * 100
    return (f"{e['symbol']} · {e['name']}" + (f" ({e['sector']})" if e.get("sector") else "") + "\n"
            f"▶ Entry: {word} {px(e['trigger'])} ({m.get('bar', "today's")} {'high' if side == 1 else 'low'})\n"
            f"🛑 Stop loss: {px(e['stop'])} ({pct(e['stop']):.1f}% {'below' if side == 1 else 'above'} entry)\n"
            f"🎯 Target: {px(e['target'])} ({pct(e['target']):.1f}%, 2x the risk)\n"
            + size_line(m, e, risk_usd) + "\n"
            + (f"Checks ({e['score']}/{e['of']} extra checks pass):\n" if "score" in e else "Checks:\n")
            + "\n".join(f"{mk} {t}" for mk, t in e.get("checks") or [("✓", w) for w in e["why"]])
            + "".join(f"\n📰 {h}" for h in e.get("headlines") or []))


def size_line(m, e, risk_usd):
    if m["env"]:
        return f"💰 {qty(size(e['trigger'], e['stop'], risk_usd))} {m['unit']} risks about ${risk_usd:g}"
    cfg = SS.load_config()   # main bot: same sizing and option hint as its other alerts
    book = SS.T.RiskBook(cfg.get("risk"), None)
    sz = book.size(e["symbol"], e["side"], e["trigger"], e["stop"], option=False)
    opt = SS.option_hint(e["symbol"], e["name"], e["side"], e["trigger"], "swing")
    return "\n".join(x for x in ((sz or {}).get("text"), f"Option: {opt}" if opt else None) if x)


def trade_line(e):
    word = "BUY" if e["side"] == 1 else "SELL"
    return (f"{e['symbol']} {word} at {px(e['entry'])} · 🛑 stop {px(e['stop'])} · 🎯 target {px(e['target'])}")


def messages(m, events, open_trades, checked, failed, day, risk_usd):
    by = lambda k: [e for e in events if e["kind"] == k]
    setups = sorted(by("setup"), key=lambda e: (-e.get("score", 0), -e["adx"]))
    when = datetime.strptime(day[:10], "%Y-%m-%d").strftime("%a %d %b") if day else "today"
    head = f"{m['title']} · {when} · {m.get('session', 'daily close')}\nChecked {checked} {m['what']}"
    if failed:
        head += f" ({failed} could not be loaded)"
    parts = [head]
    if by("entry"):
        parts.append("✅ ENTRIES TRIGGERED:\n" + "\n".join(trade_line(e) for e in by("entry")))
    if by("target"):
        parts.append("🎯 TARGET HIT (book some profit; the stop now trails for the rest):\n" + "\n".join(
            f"{e['symbol']}: {px(e['entry'])} → {px(e['target'])} "
            f"({e['side'] * (e['target'] / e['entry'] - 1) * 100:+.1f}%)" for e in by("target")))
    if by("exit"):
        lines = ["🔔 STOP LOSS HIT (close these if you hold them):"]
        for e in by("exit"):
            icon = "🔒" if e["trail"] else "❌"
            word = "BUY" if e["side"] == 1 else "SELL"
            lines.append(f"{icon} {e['symbol']} {word}: {px(e['entry'])} → {px(e['exit'])} · "
                         f"{'+' if e['r'] >= 0 else ''}{e['r']:.1f}x the risk "
                         f"({e['side'] * (e['exit'] / e['entry'] - 1) * 100:+.1f}%) after {e['days']} days")
        parts.append("\n".join(lines))
    for side, title in ((1, "🟡 GET READY TO BUY"), (-1, "🟡 GET READY TO SELL")):
        group = [e for e in setups if e["side"] == side]
        if not group:
            continue
        lines = [f"{title} ({len(group)}), most checks passed first. Don't buy yet: enter only if price crosses "
                 f"the entry level in the next {SETUP_DAYS} days. I'll message you when it does."]
        lines += [f"\n{n}. " + setup_text(m, e, risk_usd) for n, e in enumerate(group[:MAX_DETAILED], 1)]
        if len(group) > MAX_DETAILED:
            lines.append("\nAlso set up: " + ", ".join(e["symbol"] for e in group[MAX_DETAILED:]))
        parts.append("\n".join(lines))
    if by("expired"):
        parts.append("⌛ Setups cancelled (price never reached the entry): "
                     + ", ".join(e["symbol"] for e in by("expired")))
    if not any(by(k) for k in ("setup", "entry", "target", "exit")):
        parts.append("Nothing new today.")
    if open_trades:
        parts.append(f"📂 Open swing trades: {len(open_trades)} · " + ", ".join(sorted(open_trades)[:40])
                     + (" …" if len(open_trades) > 40 else ""))
    parts.append(f"Daily candles, checked {m['when']}. Not advice.")
    return split("\n\n".join(parts))


def split(text, limit=3900):
    out, cur = [], ""
    for block in text.split("\n\n"):
        if cur and len(cur) + len(block) + 2 > limit:
            out.append(cur)
            cur = ""
        cur = (cur + "\n\n" + block) if cur else block
    if cur:
        out.append(cur[:4096])
    return out


def write_log(path, events):
    if not events:
        return
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["day", "kind", "symbol", "entry", "stop_or_exit", "r"])
        for e in events:
            word = "BUY" if e["side"] == 1 else "SELL"
            if e["kind"] == "setup":
                w.writerow([e["day"], f"SETUP {word}", e["symbol"], round(e["trigger"], 8), round(e["stop"], 8), ""])
            elif e["kind"] == "entry":
                w.writerow([e["day"], f"ENTRY {word}", e["symbol"], round(e["entry"], 8), round(e["stop"], 8), ""])
            elif e["kind"] == "target":
                w.writerow([e["day"], "TARGET", e["symbol"], round(e["entry"], 8), round(e["target"], 8),
                            CFG["target_r"]])
            elif e["kind"] == "exit":
                w.writerow([e["day"], "EXIT", e["symbol"], round(e["entry"], 8), round(e["exit"], 8), round(e["r"], 2)])
            else:
                w.writerow([e["day"], "EXPIRED", e["symbol"], round(e["trigger"], 8), "", ""])


def send(m, texts):
    pre = f"{m['env']}_" if m["env"] else ""   # the main bot uses plain TELEGRAM_BOT_TOKEN
    token = os.environ.get(f"{pre}TELEGRAM_BOT_TOKEN", "").strip()
    chat = (os.environ.get(f"{pre}TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    for t in texts:
        print("\n" + t + "\n")
    if not token or not chat:
        print(f"{pre}TELEGRAM_BOT_TOKEN / chat id not set: messages printed here only.")
        return "--test-telegram" not in sys.argv   # a test with no bot set up should show as failed
    return all(SS.send_telegram(token, chat, t) for t in texts)


def us_market_open():
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/New_York"))
    return now.weekday() < 5 and (9, 45) <= (now.hour, now.minute) < (16, 0), now.strftime("%Y-%m-%d")


def live_pass(key, m, risk_usd):
    """Every 30 minutes: alert right away when a GET READY setup's entry level is crossed, when an open
    trade reaches its target, and when it breaks its stop. Each alert is sent once; the daily summary
    then records them. US: only while the market is open. Crypto: around the clock."""
    today = None   # main: each market's own latest candle day
    if key == "us":
        is_open, today = us_market_open()
        if not is_open:
            print("US market closed: no live check.")
            return
    elif key == "crypto":
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state_path = os.path.join(HERE, f"{key}_state.json")
    state = load_state(state_path)
    rows = state.get("rows") or {}
    watch = {s for s, r in rows.items() if r.get("trade") or r.get("pending")}
    if not watch:
        print("Live check: no setups or trades to watch.")
        return
    items = [it for it in load_list(m) if it["symbol"] in watch]
    loader = yahoo_daily if m["source"] == "yahoo" else binance_daily

    def since(row):   # crypto: only prices after the setup's candle closed / after the entry count
        if row.get("trade"):
            return max(row["trade"].get("opened_t") or 0, time.time() - 86400)
        return row["pending"]["t"] + 86400

    def fetch(item):
        try:
            if key == "crypto":
                return item, [dict(binance_since(item["symbol"], since(rows[item["symbol"]])), day=today)]
            return item, loader(item["symbol"], live=True)
        except Exception:
            return item, None

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(fetch, items))
    entries, targets, stops = [], [], []
    for it, bars in results:
        row = rows[it["symbol"]]
        if not bars or (today and bars[-1]["day"] != today):
            continue
        b, tr, pend = bars[-1], row.get("trade"), row.get("pending")
        day = today or b["day"]
        if not today and (pend and b["day"] <= pend["day"] or tr and b["day"] < tr.get("day", "")):
            continue   # main: no candle since the setup / entry yet
        name = f"{it['symbol']} · {it['name']}"
        if tr:
            side = tr.get("side", 1)
            word = "bought" if side == 1 else "sold"
            if (side == 1 and b["l"] <= tr["stop"]) or (side == -1 and b["h"] >= tr["stop"]):
                if tr.get("stop_alert_day") == day:
                    continue
                tr["stop_alert_day"] = day
                stops.append(f"{'🔒' if side * (tr['stop'] - tr['entry']) > 0 else '❌'} {name}: price "
                             f"{px(b['c'])} broke the stop {px(tr['stop'])} ({word} at {px(tr['entry'])}, "
                             f"{side * (tr['stop'] / tr['entry'] - 1) * 100:+.1f}% at the stop)")
            elif not tr.get("target_alerted"):
                tgt = target_of(tr)
                if (side == 1 and b["h"] >= tgt) or (side == -1 and b["l"] <= tgt):
                    tr["target_alerted"] = True
                    targets.append(f"🎯 {name}: reached the target {px(tgt)} ({word} at {px(tr['entry'])}, "
                                   f"{side * (tgt / tr['entry'] - 1) * 100:+.1f}%). Book some profit; "
                                   f"keep the rest with the stop at {px(tr['stop'])}.")
        elif pend and pend.get("alert_day") != day and (key != "us" or pend["day"] < today):
            side = pend["side"]
            if (side == 1 and b["h"] >= pend["trigger"]) or (side == -1 and b["l"] <= pend["trigger"]):
                pend["alert_day"] = day
                stop = pend["trigger"] - side * pend["risk"]
                tgt = pend["trigger"] + side * CFG["target_r"] * pend["risk"]
                entries.append(f"✅ {name}: {'BUY' if side == 1 else 'SELL'} triggered at {px(pend['trigger'])} "
                               f"(price now {px(b['c'])})\n🛑 Stop loss {px(stop)} · 🎯 Target {px(tgt)} · 💰 "
                               f"{qty(size(pend['trigger'], stop, risk_usd))} {m['unit']} risks about ${risk_usd:g}")
    parts = []
    if entries:
        parts.append("✅ ENTRY TRIGGERED, enter now:\n\n" + "\n\n".join(entries))
    if targets:
        parts.append("🎯 TARGET HIT:\n" + "\n".join(targets))
    if stops:
        parts.append("🔔 STOP LOSS HIT, exit now:\n" + "\n".join(stops))
    if parts:
        now = datetime.now(timezone.utc).astimezone().strftime("%H:%M")
        send(m, split(f"{m['title']} · LIVE {now} India time\n\n" + "\n\n".join(parts) + "\n\nNot advice."))
    else:
        print("Live check: nothing new.")
    save_state(state_path, state)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    key = args[0] if args else "us"
    m = MARKETS[key]
    risk_usd = float(os.environ.get(f"{m['env']}_RISK_PER_TRADE_USD") or 12)   # about ₹1000 per trade
    global CUR
    CUR = m.get("cur", "$")
    if "--live" in sys.argv:
        return live_pass(key, m, risk_usd)
    if "--test-telegram" in sys.argv:
        ok = send(m, [f"✅ {m['title']} alerts connected. {m['when'][0].upper() + m['when'][1:]} you'll get one summary here: "
                      f"new setups in {m['what']}, then entry, target and stop-loss alerts."])
        sys.exit(0 if ok else 1)
    state_path = os.path.join(HERE, f"{key}_state.json")
    log_path = os.path.join(HERE, f"{key}_signals_log.csv")
    items = load_list(m)
    state = load_state(state_path)
    rows = state.setdefault("rows", {})
    first = not rows
    loader, tkey, day_key = (yahoo_daily if m["source"] == "yahoo" else binance_daily), "last_t", "last_day"
    seed = False
    if key == "crypto":
        anchor = 8 if 8 <= datetime.now(timezone.utc).hour < 16 else 16   # the session that closed last
        loader = lambda sym: binance_anchored(sym, anchor)
        tkey, day_key = f"last_t_{anchor}", f"last_day_{anchor}"
        # a session's first run: start from its latest closed candle, so that candle is checked right away
        seed = not first and not any(tkey in r for r in rows.values())
        m = dict(m, session=CRYPTO_SESSIONS[anchor], bar="the last 24h")

    def fetch(item):
        try:
            return item["symbol"], loader(item["symbol"]), None
        except Exception as e:
            return item["symbol"], None, str(e)[:80]

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = {sym: (bars, err) for sym, bars, err in pool.map(fetch, items)}
    events, failed, latest = [], [], ""
    if seed:
        print("Seeding a new session from its latest candle.")
    for it in items:
        bars, err = results[it["symbol"]]
        if err or not bars:
            failed.append(f"{it['symbol']}: {err or 'no data'}")
            continue
        latest = max(latest, bars[-1]["day"])
        if seed and len(bars) > 1:
            rows.setdefault(it["symbol"], {}).setdefault(tkey, bars[-2]["t"])
        step(it, bars, rows.setdefault(it["symbol"], {}), first, events, m["sides"], tkey)
    if failed:
        print(f"{len(failed)} failed:", "; ".join(failed[:20]))
    open_trades = [s for s, r in rows.items() if r.get("trade")]
    checked = len(items) - len(failed)
    if key == "main":   # checked every 30 minutes: message only when something happened
        if events and not first:
            enrich(m, events)
            write_log(log_path, events)
            send(m, messages(m, events, open_trades, checked, len(failed), latest, risk_usd))
        else:
            print("Main swing: nothing new." if not first else "Main swing: learned history.")
    elif first:
        send(m, [f"{m['title']} scanner is set up: {checked} {m['what']} loaded. "
                 f"From the next session you'll get new setups {m['when']}, then entry, target and stop alerts."])
    elif latest and latest != state.get(day_key):
        enrich(m, events)
        write_log(log_path, events)
        if not send(m, messages(m, events, open_trades, checked, len(failed), latest, risk_usd)):
            print("telegram send failed")
    else:
        print("No new daily candle since the last run: nothing sent.")
    state[day_key] = latest or state.get(day_key)
    state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state_path, state)
    print(f"Done: {checked} checked, {len(failed)} failed, {len(events)} events, {len(open_trades)} open trades.")


if __name__ == "__main__":
    main()
