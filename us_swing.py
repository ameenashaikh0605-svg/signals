"""US stocks swing scan: once a day after the US close, check every stock in us_stocks.csv (the S&P 500)
with the same daily swing rule as the Indian swing alerts, and send one Telegram summary to a separate bot.

Rule (same as the swing mode in signal_server.py): BUY when the daily Supertrend turns up while price is
above its 200-day average. Stop loss 1.5 ATR below entry, then a trailing stop 3 ATR below the best close.
Long only (US stocks on Indian apps can't be shorted). Open trades are kept in us_state.json.

Telegram details come from the environment (US_TELEGRAM_BOT_TOKEN, and US_TELEGRAM_CHAT_ID or
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
STATE = os.path.join(HERE, "us_state.json")
LIST = os.path.join(HERE, "us_stocks.csv")
LOG = os.path.join(HERE, "us_signals_log.csv")
RISK_USD = float(os.environ.get("US_RISK_PER_TRADE_USD") or 12)   # about ₹1000 per trade, like the Indian alerts
CATCH_UP_DAYS = 5      # after a gap, only act on the last few daily candles (no alert floods)
MAX_DETAILED = 15      # new buys shown in full; the rest are listed by name
CFG = dict(S.DEFAULTS)


def load_stocks():
    with open(LIST, encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r.get("symbol")]


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(tmp, STATE)


def daily_bars(symbol):
    """Closed daily candles. Today's candle counts once the regular session has ended."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
           f"?interval=1d&range=2y&includePrePost=false")
    last_err = None
    for attempt in range(3):
        try:
            res = SS.http_json(url)["chart"]["result"][0]
            break
        except Exception as e:
            last_err = e
            time.sleep(2 + 3 * attempt)
    else:
        raise RuntimeError(str(last_err))
    meta = res.get("meta", {})
    off = meta.get("gmtoffset", 0)
    end = ((meta.get("currentTradingPeriod") or {}).get("regular") or {}).get("end") or 0
    q = (res.get("indicators", {}).get("quote") or [{}])[0]
    ts = res.get("timestamp") or []
    now = time.time()
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
    if bars and now < end and bars[-1]["day"] >= end_day:
        bars.pop()
    return bars


def reasons(ind, i):
    out = ["Supertrend turned up and price is above its 200-day average"]
    if S.trend_dir(ind, i) == 1:
        out.append("20 and 50-day averages point up")
    if ind["adx"][i] >= CFG["adx_min"]:
        out.append(f"strong trend, not choppy (ADX {ind['adx'][i]:.0f})")
    if ind["has_vol"] and ind["vavg"][i - 1] and ind["v"][i] >= 1.2 * ind["vavg"][i - 1]:
        out.append(f"volume {ind['v'][i] / ind['vavg'][i - 1]:.1f}x normal")
    return out


def px(x):
    return f"${x:,.2f}"


def step(stock, bars, row, first, events):
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
            if b["l"] <= tr["stop"]:
                exit_px = min(b["o"], tr["stop"])          # a gap down fills at the open
                r = (exit_px - tr["entry"]) / tr["risk"] if tr["risk"] else 0
                moved = tr["stop"] > tr["entry"]
                events.append({"kind": "exit", "symbol": sym, "name": stock["name"], "entry": tr["entry"],
                               "exit": exit_px, "r": r, "trail": moved, "day": b["day"],
                               "days": len([x for x in bars if tr["day"] < x["day"] <= b["day"]])})
                row["trade"] = None
                continue
            tr["best"] = max(tr["best"], b["c"])
            tr["stop"] = max(tr["stop"], tr["best"] - CFG["trail_atr"] * a)
            continue
        side, why = S.signal_at(bars, ind, j, CFG, "supertrend", False)
        if side != 1:
            continue
        entry = b["c"]
        stop, _, risk = S.levels(entry, 1, a, CFG)
        row["trade"] = {"entry": entry, "stop": stop, "risk": risk, "best": entry, "day": b["day"]}
        events.append({"kind": "buy", "symbol": sym, "name": stock["name"], "sector": stock.get("sector", ""),
                       "entry": entry, "stop": stop, "risk": risk, "adx": ind["adx"][j], "day": b["day"],
                       "why": reasons(ind, j)})
    row["last_t"] = bars[-1]["t"]


def shares(entry, stop):
    per = entry - stop
    return RISK_USD / per if per > 0 else 0


def messages(events, open_trades, checked, failed, day):
    buys = sorted([e for e in events if e["kind"] == "buy"], key=lambda e: -e["adx"])
    exits = [e for e in events if e["kind"] == "exit"]
    when = datetime.strptime(day, "%Y-%m-%d").strftime("%a %d %b") if day else "today"
    head = f"🇺🇸 US SWING · {when} close\nChecked {checked} S&P 500 stocks"
    if failed:
        head += f" ({failed} could not be loaded)"
    parts = [head]
    if buys:
        lines = [f"🟢 NEW BUYS ({len(buys)}), strongest trend first:"]
        for n, e in enumerate(buys[:MAX_DETAILED], 1):
            sh = shares(e["entry"], e["stop"])
            lines.append(f"\n{n}. {e['symbol']} · {e['name']} ({e['sector']})\n"
                         f"▶ Buy near {px(e['entry'])}  🛑 Stop {px(e['stop'])} "
                         f"({(e['entry'] - e['stop']) / e['entry'] * 100:.1f}% below)\n"
                         f"💰 {sh:.2f} shares risks about ${RISK_USD:g}\n"
                         f"Why: " + "; ".join(e["why"]))
        if len(buys) > MAX_DETAILED:
            lines.append("\nAlso turned up: " + ", ".join(e["symbol"] for e in buys[MAX_DETAILED:]))
        lines.append("\nNo fixed target: the stop follows price up. I'll tell you when a stop is hit.")
        parts.append("\n".join(lines))
    else:
        parts.append("No new buy setups today.")
    if exits:
        lines = ["🔔 STOPS HIT (sell these if you hold them):"]
        for e in exits:
            icon = "🔒" if e["trail"] else "❌"
            lines.append(f"{icon} {e['symbol']}: {px(e['entry'])} → {px(e['exit'])} · "
                         f"{'+' if e['r'] >= 0 else ''}{e['r']:.1f}x the risk "
                         f"({(e['exit'] / e['entry'] - 1) * 100:+.1f}%) after {e['days']} days")
        parts.append("\n".join(lines))
    if open_trades:
        parts.append(f"📂 Open swing trades: {len(open_trades)} · " + ", ".join(sorted(open_trades)[:40])
                     + (" …" if len(open_trades) > 40 else ""))
    parts.append("Daily candles, checked after the US close. Not advice.")
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


def write_log(events):
    if not events:
        return
    new = not os.path.exists(LOG)
    with open(LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["day", "kind", "symbol", "entry", "stop_or_exit", "r"])
        for e in events:
            if e["kind"] == "buy":
                w.writerow([e["day"], "BUY", e["symbol"], round(e["entry"], 4), round(e["stop"], 4), ""])
            else:
                w.writerow([e["day"], "EXIT", e["symbol"], round(e["entry"], 4), round(e["exit"], 4), round(e["r"], 2)])


def send(texts):
    token = os.environ.get("US_TELEGRAM_BOT_TOKEN", "").strip()
    chat = (os.environ.get("US_TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    for t in texts:
        print("\n" + t + "\n")
    if not token or not chat:
        print("US_TELEGRAM_BOT_TOKEN / chat id not set: messages printed here only.")
        return True
    return all(SS.send_telegram(token, chat, t) for t in texts)


def main():
    if "--test-telegram" in sys.argv:
        ok = send(["✅ US swing alerts connected. After each US market close you'll get one summary here: "
                   "new BUY setups in S&P 500 stocks and any stop losses hit."])
        sys.exit(0 if ok else 1)
    stocks = load_stocks()
    state = load_state()
    rows = state.setdefault("rows", {})
    first = not rows
    results = {}

    def fetch(stock):
        try:
            return stock["symbol"], daily_bars(stock["symbol"]), None
        except Exception as e:
            return stock["symbol"], None, str(e)[:80]

    with ThreadPoolExecutor(max_workers=6) as pool:
        for sym, bars, err in pool.map(fetch, stocks):
            results[sym] = (bars, err)
    events, failed, latest = [], [], ""
    for st in stocks:
        bars, err = results[st["symbol"]]
        if err or not bars:
            failed.append(f"{st['symbol']}: {err or 'no data'}")
            continue
        latest = max(latest, bars[-1]["day"])
        step(st, bars, rows.setdefault(st["symbol"], {}), first, events)
    if failed:
        print(f"{len(failed)} stocks failed:", "; ".join(failed[:20]))
    open_trades = [s for s, r in rows.items() if r.get("trade")]
    checked = len(stocks) - len(failed)
    if first:
        send([f"🇺🇸 US swing scanner is set up: {checked} S&P 500 stocks loaded. "
              "From the next US close you'll get one summary a day with new BUY setups and stops hit."])
    elif latest and latest != state.get("last_day"):
        write_log(events)
        if not send(messages(events, open_trades, checked, len(failed), latest)):
            print("telegram send failed")
    else:
        print("No new daily candle since the last run: nothing sent.")
    state["last_day"] = latest or state.get("last_day")
    state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)
    print(f"Done: {checked} checked, {len(failed)} failed, {len(events)} events, {len(open_trades)} open trades.")


if __name__ == "__main__":
    main()
