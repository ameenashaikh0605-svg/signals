"""Daily swing scan for a whole market, sent as one Telegram summary to that market's own bot.

    python swing_scan.py us       S&P 500 stocks (us_stocks.csv), after each US close, buys only
    python swing_scan.py crypto   top coins (crypto_coins.csv), after each daily close (00:00 UTC), buys and sells

Rule (same as the swing mode in signal_server.py): BUY when the daily Supertrend turns up while price is
above its 200-day average (SELL: turns down while below it). Stop loss 1.5 ATR from entry, then a trailing
stop 3 ATR from the best close. Open trades are kept in <market>_state.json.

Telegram details come from the environment (<MARKET>_TELEGRAM_BOT_TOKEN, and <MARKET>_TELEGRAM_CHAT_ID or
TELEGRAM_CHAT_ID), never from files. Read-only: it never places orders."""
import csv
import json
import os
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import signal_server as SS
import strategy as S

HERE = os.path.dirname(os.path.abspath(__file__))
CATCH_UP_DAYS = 5      # after a gap, only act on the last few daily candles (no alert floods)
MAX_DETAILED = 15      # new signals shown in full; the rest are listed by name
CFG = dict(S.DEFAULTS)

MARKETS = {
    "us": {"title": "🇺🇸 US SWING", "what": "S&P 500 stocks", "list": "us_stocks.csv", "source": "yahoo",
           "sides": (1,), "unit": "shares", "env": "US", "when": "after the US close"},
    "crypto": {"title": "🪙 CRYPTO SWING", "what": "top coins", "list": "crypto_coins.csv", "source": "binance",
               "sides": (1, -1), "unit": "coins", "env": "CRYPTO", "when": "after each daily close (5:30 am India time)"},
}


def load_list(m):
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


def binance_daily(symbol):
    """Closed daily candles (UTC days), about 2 years."""
    rows = retry(lambda: SS.BinanceSource()._get(f"/klines?symbol={symbol}&interval=1d&limit=800"))
    now_ms = int(time.time() * 1000)
    return [{"t": r[0] // 1000, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
             "v": float(r[5]), "day": datetime.fromtimestamp(r[0] // 1000, tz=timezone.utc).strftime("%Y-%m-%d")}
            for r in rows if r[6] < now_ms]


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
        return f"${x:,.2f}"
    return f"${x:.{max(4, 3 - int(f'{x:e}'.split('e')[1]))}f}".rstrip("0")


def step(stock, bars, row, first, events, sides):
    """Walk through every daily candle closed since the last run. Updates `row` and appends to `events`."""
    sym = stock["symbol"]
    if len(bars) <= CFG["ema_trend"] + 2:
        return
    last = row.get("last_t")
    if first or last is None:
        row["last_t"] = bars[-1]["t"]
        return
    cutoff = bars[-1]["t"] - CATCH_UP_DAYS * 86400
    todo = [j for j, b in enumerate(bars) if b["t"] > last and b["t"] >= cutoff]
    if not todo:
        return
    ind = S.indicators(bars, CFG)
    ind["v"] = [b["v"] or 0.0 for b in bars]
    for j in todo:
        b, a = bars[j], ind["atr"][j]
        tr = row.get("trade")
        if tr:
            side = tr.get("side", 1)
            if (side == 1 and b["l"] <= tr["stop"]) or (side == -1 and b["h"] >= tr["stop"]):
                exit_px = min(b["o"], tr["stop"]) if side == 1 else max(b["o"], tr["stop"])  # gaps fill at the open
                r = side * (exit_px - tr["entry"]) / tr["risk"] if tr["risk"] else 0
                events.append({"kind": "exit", "symbol": sym, "name": stock["name"], "side": side,
                               "entry": tr["entry"], "exit": exit_px, "r": r,
                               "trail": side * (tr["stop"] - tr["entry"]) > 0, "day": b["day"],
                               "days": len([x for x in bars if tr["day"] < x["day"] <= b["day"]])})
                row["trade"] = None
                continue
            tr["best"] = max(tr["best"], b["c"]) if side == 1 else min(tr["best"], b["c"])
            trail = tr["best"] - side * CFG["trail_atr"] * a
            tr["stop"] = max(tr["stop"], trail) if side == 1 else min(tr["stop"], trail)
            continue
        side, why = S.signal_at(bars, ind, j, CFG, "supertrend", False)
        if side not in sides:
            continue
        entry = b["c"]
        stop, _, risk = S.levels(entry, side, a, CFG)
        row["trade"] = {"side": side, "entry": entry, "stop": stop, "risk": risk, "best": entry, "day": b["day"]}
        events.append({"kind": "signal", "symbol": sym, "name": stock["name"], "sector": stock.get("sector", ""),
                       "side": side, "entry": entry, "stop": stop, "risk": risk, "adx": ind["adx"][j],
                       "day": b["day"], "why": reasons(ind, j, side)})
    row["last_t"] = bars[-1]["t"]


def size(entry, stop, risk_usd):
    per = abs(entry - stop)
    return risk_usd / per if per > 0 else 0


def qty(x):
    return f"{x:,.0f}" if x >= 100 else f"{x:.2f}" if x >= 0.01 else f"{x:.6f}"


def messages(m, events, open_trades, checked, failed, day, risk_usd):
    sig = sorted([e for e in events if e["kind"] == "signal"], key=lambda e: -e["adx"])
    exits = [e for e in events if e["kind"] == "exit"]
    when = datetime.strptime(day, "%Y-%m-%d").strftime("%a %d %b") if day else "today"
    head = f"{m['title']} · {when} daily close\nChecked {checked} {m['what']}"
    if failed:
        head += f" ({failed} could not be loaded)"
    parts = [head]
    for side, title in ((1, "🟢 NEW BUYS"), (-1, "🔴 NEW SELLS")):
        group = [e for e in sig if e["side"] == side]
        if not group:
            continue
        lines = [f"{title} ({len(group)}), strongest trend first:"]
        for n, e in enumerate(group[:MAX_DETAILED], 1):
            word = "Buy" if side == 1 else "Sell/short"
            gap = abs(e["entry"] - e["stop"]) / e["entry"] * 100
            lines.append(f"\n{n}. {e['symbol']} · {e['name']} ({e['sector']})\n"
                         f"▶ Entry: {word.lower()} near {px(e['entry'])}\n"
                         f"🛑 Stop loss: {px(e['stop'])} ({gap:.1f}% {'below' if side == 1 else 'above'} entry)\n"
                         f"💰 {qty(size(e['entry'], e['stop'], risk_usd))} {m['unit']} risks about ${risk_usd:g}\n"
                         f"Why: " + "; ".join(e["why"]))
        if len(group) > MAX_DETAILED:
            lines.append("\nAlso turned: " + ", ".join(e["symbol"] for e in group[MAX_DETAILED:]))
        parts.append("\n".join(lines))
    if sig:
        parts.append("No fixed target: the stop follows price. I'll tell you when a stop is hit.")
    else:
        parts.append("No new setups today.")
    if exits:
        lines = ["🔔 STOPS HIT (close these if you hold them):"]
        for e in exits:
            icon = "🔒" if e["trail"] else "❌"
            word = "BUY" if e["side"] == 1 else "SELL"
            lines.append(f"{icon} {e['symbol']} {word}: {px(e['entry'])} → {px(e['exit'])} · "
                         f"{'+' if e['r'] >= 0 else ''}{e['r']:.1f}x the risk "
                         f"({e['side'] * (e['exit'] / e['entry'] - 1) * 100:+.1f}%) after {e['days']} days")
        parts.append("\n".join(lines))
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
            if e["kind"] == "signal":
                w.writerow([e["day"], "BUY" if e["side"] == 1 else "SELL", e["symbol"], round(e["entry"], 8),
                            round(e["stop"], 8), ""])
            else:
                w.writerow([e["day"], "EXIT", e["symbol"], round(e["entry"], 8), round(e["exit"], 8), round(e["r"], 2)])


def send(m, texts):
    token = os.environ.get(f"{m['env']}_TELEGRAM_BOT_TOKEN", "").strip()
    chat = (os.environ.get(f"{m['env']}_TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    for t in texts:
        print("\n" + t + "\n")
    if not token or not chat:
        print(f"{m['env']}_TELEGRAM_BOT_TOKEN / chat id not set: messages printed here only.")
        return "--test-telegram" not in sys.argv   # a test with no bot set up should show as failed
    return all(SS.send_telegram(token, chat, t) for t in texts)


def us_market_open():
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/New_York"))
    return now.weekday() < 5 and (9, 45) <= (now.hour, now.minute) < (16, 0), now.strftime("%Y-%m-%d")


def live_pass(key, m, risk_usd):
    """During US market hours: alert right away when an open trade's stop is hit, and when a stock's
    daily Supertrend is turning up on today's price (an early BUY, confirmed or cancelled at the close).
    Each stock alerts at most once per day for each kind. The after-close summary still runs as before."""
    is_open, today = us_market_open()
    if not is_open:
        print("US market closed: no live check.")
        return
    state_path = os.path.join(HERE, f"{key}_state.json")
    state = load_state(state_path)
    rows = state.get("rows") or {}
    if not rows:
        return
    items = load_list(m)

    def fetch(item):
        try:
            return item, yahoo_daily(item["symbol"], live=True)
        except Exception:
            return item, None

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(fetch, items))
    early, stops = [], []
    for it, bars in results:
        row = rows.get(it["symbol"])
        if not bars or row is None or bars[-1]["day"] != today or len(bars) <= CFG["ema_trend"] + 2:
            continue
        b, tr = bars[-1], row.get("trade")
        if tr:
            if b["l"] <= tr["stop"] and row.get("stop_alert_day") != today:
                row["stop_alert_day"] = today
                stops.append(f"❌ {it['symbol']} · {it['name']}: price {px(b['c'])} broke the stop {px(tr['stop'])} "
                             f"(bought {px(tr['entry'])}, {(b['c'] / tr['entry'] - 1) * 100:+.1f}%)")
            continue
        if row.get("early_day") == today:
            continue
        ind = S.indicators(bars, CFG)
        ind["v"] = [x["v"] or 0.0 for x in bars]
        i = len(bars) - 1
        side, _ = S.signal_at(bars, ind, i, CFG, "supertrend", False)
        if side != 1:
            continue
        row["early_day"] = today
        stop, _, _ = S.levels(b["c"], 1, ind["atr"][i], CFG)
        early.append((ind["adx"][i], f"{it['symbol']} · {it['name']}\n▶ Entry: buy near {px(b['c'])} (price now)\n"
                      f"🛑 Stop loss: {px(stop)} ({(b['c'] - stop) / b['c'] * 100:.1f}% below entry)\n"
                      f"💰 {qty(size(b['c'], stop, risk_usd))} shares "
                      f"risks about ${risk_usd:g}\nWhy: " + "; ".join(reasons(ind, i, 1)[1:] or ["trend turning up"])))
    parts = []
    if stops:
        parts.append("🔔 STOP LOSS HIT NOW (sell if you hold it):\n" + "\n".join(stops))
    if early:
        early.sort(key=lambda x: -x[0])
        parts.append(f"🟡 EARLY BUY SETUPS ({len(early)}): the daily Supertrend is turning up on today's price, "
                     "above the 200-day average. Not confirmed until the close: it can still fail if price drops "
                     "back. Tonight's summary confirms or cancels each one.\n\n"
                     + "\n\n".join(f"{n}. {t}" for n, (_, t) in enumerate(early[:MAX_DETAILED], 1))
                     + (f"\n\nAlso turning up: " + ", ".join(t.split(" ·")[0] for _, t in early[MAX_DETAILED:])
                        if len(early) > MAX_DETAILED else ""))
    if parts:
        now_et = datetime.now(timezone.utc).astimezone().strftime("%H:%M")
        send(m, split(f"{m['title']} · LIVE {now_et} India time\n\n" + "\n\n".join(parts) + "\n\nNot advice."))
    else:
        print("Live check: nothing new.")
    save_state(state_path, state)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    key = args[0] if args else "us"
    m = MARKETS[key]
    risk_usd = float(os.environ.get(f"{m['env']}_RISK_PER_TRADE_USD") or 12)   # about ₹1000 per trade
    if "--live" in sys.argv:
        return live_pass(key, m, risk_usd)
    if "--test-telegram" in sys.argv:
        ok = send(m, [f"✅ {m['title']} alerts connected. {m['when'][0].upper() + m['when'][1:]} you'll get one summary here: "
                      f"new swing setups in {m['what']} and any stop losses hit."])
        sys.exit(0 if ok else 1)
    state_path = os.path.join(HERE, f"{key}_state.json")
    log_path = os.path.join(HERE, f"{key}_signals_log.csv")
    items = load_list(m)
    state = load_state(state_path)
    rows = state.setdefault("rows", {})
    first = not rows
    loader = yahoo_daily if m["source"] == "yahoo" else binance_daily

    def fetch(item):
        try:
            return item["symbol"], loader(item["symbol"]), None
        except Exception as e:
            return item["symbol"], None, str(e)[:80]

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = {sym: (bars, err) for sym, bars, err in pool.map(fetch, items)}
    events, failed, latest = [], [], ""
    for it in items:
        bars, err = results[it["symbol"]]
        if err or not bars:
            failed.append(f"{it['symbol']}: {err or 'no data'}")
            continue
        latest = max(latest, bars[-1]["day"])
        step(it, bars, rows.setdefault(it["symbol"], {}), first, events, m["sides"])
    if failed:
        print(f"{len(failed)} failed:", "; ".join(failed[:20]))
    open_trades = [s for s, r in rows.items() if r.get("trade")]
    checked = len(items) - len(failed)
    if first:
        send(m, [f"{m['title']} scanner is set up: {checked} {m['what']} loaded. "
                 f"From the next daily close you'll get one summary a day with new setups and stops hit."])
    elif latest and latest != state.get("last_day"):
        write_log(log_path, events)
        texts = messages(m, events, open_trades, checked, len(failed), latest, risk_usd)
        confirmed = {e["symbol"] for e in events if e["kind"] == "signal"}
        failed_early = sorted(s for s, r in rows.items() if r.get("early_day") == latest and s not in confirmed)
        if failed_early:
            texts[-1] += "\n\n🚫 Early setups that did NOT confirm at the close (skip them): " + ", ".join(failed_early)
        if not send(m, texts):
            print("telegram send failed")
    else:
        print("No new daily candle since the last run: nothing sent.")
    state["last_day"] = latest or state.get("last_day")
    state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state_path, state)
    print(f"Done: {checked} checked, {len(failed)} failed, {len(events)} events, {len(open_trades)} open trades.")


if __name__ == "__main__":
    main()
