#!/usr/bin/env python3
"""
Backtest: old v1 "buyer/seller pressure" signals vs the v2 rules in strategy.py.

Downloads free historical candles (Yahoo Finance, Binance), simulates every
signal with realistic costs, and prints hit rate, average result per trade,
drawdown and trade count. Standard library only.

  python backtest.py                 full test, all instruments and timeframes
  python backtest.py --quick         fewer instruments
  python backtest.py --csv FILE.csv  test your own candles (t,o,h,l,c,v columns)

Results are saved to backtest_results.json and backtest_results.txt.
"""
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import strategy as S

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "data_cache")

# name, source, code, cost per side (fraction), session-based (intraday market)
INSTRUMENTS = [
    ("NIFTY 50", "yahoo", "^NSEI", 0.0003, True),
    ("SENSEX", "yahoo", "^BSESN", 0.0003, True),
    ("BANK NIFTY", "yahoo", "^NSEBANK", 0.0003, True),
    ("RELIANCE", "yahoo", "RELIANCE.NS", 0.0005, True),
    ("BITCOIN", "binance", "BTCUSDT", 0.0005, False),
    ("GOLD", "yahoo", "GC=F", 0.0002, True),
    ("CRUDE OIL", "yahoo", "CL=F", 0.0002, True),
    ("NASDAQ 100 (QQQ)", "yahoo", "QQQ", 0.0002, True),
    ("APPLE", "yahoo", "AAPL", 0.0002, True),
    ("NVIDIA", "yahoo", "NVDA", 0.0002, True),
]
QUICK = {"NIFTY 50", "SENSEX", "BITCOIN", "APPLE"}

# timeframe -> (yahoo interval, yahoo range, binance interval, binance days)
TIMEFRAMES = {
    "5m": ("5m", "60d", "5m", 365),
    "15m": ("15m", "60d", "15m", 730),
    "1h": ("60m", "730d", "1h", 1460),
    "1d": ("1d", "15y", "1d", 3000),
}


# ───────────────────────── data ─────────────────────────

def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def yahoo(code, interval, rng):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(code)}"
           f"?interval={interval}&range={rng}&includePrePost=false")
    res = get_json(url)["chart"]["result"][0]
    off = res["meta"].get("gmtoffset", 0)
    q = res["indicators"]["quote"][0]
    bars = []
    for i, t in enumerate(res.get("timestamp") or []):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        day = datetime.fromtimestamp(t + off, tz=timezone.utc).strftime("%Y-%m-%d")
        bars.append({"t": t, "o": o, "h": h, "l": l, "c": c, "v": q["volume"][i] or 0.0, "day": day})
    return bars


def binance(code, interval, days):
    end = int(time.time() * 1000)
    start = end - days * 86400 * 1000
    bars, cur = [], start
    hosts = ["https://data-api.binance.vision/api/v3", "https://api.binance.com/api/v3"]
    while cur < end:
        rows = None
        for h in hosts:
            try:
                rows = get_json(f"{h}/klines?symbol={code}&interval={interval}&startTime={cur}&limit=1000")
                break
            except Exception as e:
                err = e
        if rows is None:
            raise RuntimeError(f"Binance unreachable: {err}")
        if not rows:
            break
        for r in rows:
            t = r[0] // 1000
            bars.append({"t": t, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
                         "v": float(r[5]), "day": datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")})
        cur = rows[-1][0] + 1
        time.sleep(0.15)
    return bars


def load(name, src, code, tf):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{code.replace('^', '_').replace('=', '_')}_{tf}.json")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 12 * 3600:
        with open(path) as f:
            return json.load(f)
    yi, yr, bi, bd = TIMEFRAMES[tf]
    bars = yahoo(code, yi, yr) if src == "yahoo" else binance(code, bi, bd)
    with open(path, "w") as f:
        json.dump(bars, f)
    return bars


def yahoo_1m(code, days=29):
    """Yahoo serves 1-minute candles for the last ~30 days, 7 days per request."""
    end = int(time.time())
    bars, seen = [], set()
    start = end - days * 86400
    while start < end:
        stop = min(start + 7 * 86400, end)
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(code)}"
               f"?interval=1m&period1={start}&period2={stop}&includePrePost=false")
        res = get_json(url)["chart"]["result"][0]
        off = res["meta"].get("gmtoffset", 0)
        q = res["indicators"]["quote"][0]
        for i, t in enumerate(res.get("timestamp") or []):
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
            if None in (o, h, l, c) or t in seen:
                continue
            seen.add(t)
            bars.append({"t": t, "o": o, "h": h, "l": l, "c": c, "v": q["volume"][i] or 0.0,
                         "day": datetime.fromtimestamp(t + off, tz=timezone.utc).strftime("%Y-%m-%d")})
        start = stop
        time.sleep(0.3)
    bars.sort(key=lambda b: b["t"])
    return bars


def scalp_main(say):
    """Fast test: old v1 panel logic on 1-minute candles (what the first panel did)
    vs the multi-timeframe scalp rule (1m trigger, 5m + 20m trend agree)."""
    cfg = dict(S.DEFAULTS)
    results = {}
    for name, src, code, cost, intraday in [x for x in INSTRUMENTS if x[0] in ("NIFTY 50", "SENSEX", "BANK NIFTY", "BITCOIN")]:
        path = os.path.join(CACHE, f"{code.replace('^', '_')}_1m.json")
        os.makedirs(CACHE, exist_ok=True)
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < 12 * 3600:
            bars = json.load(open(path))
        else:
            try:
                bars = yahoo_1m(code) if src == "yahoo" else binance(code, "1m", 60)
            except Exception as e:
                say(f"{name} 1m: could not download ({e})")
                continue
            json.dump(bars, open(path, "w"))
        days = (bars[-1]["t"] - bars[0]["t"]) / 86400
        old = S.old_signals(bars)
        sig, risk, _ = S.scalp_signals(bars)
        a = S.atr(bars, 14)
        tests = [
            ("OLD v1 panel on 1m: buy BUYERS, sell SELLERS", lambda i: (old[i], ""), "opposite", lambda i: old[i], None),
            ("OLD v1 on 1m + stop/2R target", lambda i: (old[i], ""), "target", None, None),
            ("SCALP 1m+5m+20m, 2R target", lambda i: (sig[i], ""), "target", None, lambda i: risk[i]),
            ("SCALP 1m+5m+20m, trailing stop", lambda i: (sig[i], ""), "trail", None, lambda i: risk[i]),
        ]
        # other trend timeframe sets, same 1m trigger and 2R target
        for mins in [(5, 15), (5, 15, 30), (5, 10, 15, 30), (10, 30), (15, 60)]:
            vs, vr, _ = S.scalp_signals(bars, {"htf_minutes": mins})
            tag = "+".join(f"{m}m" for m in mins)
            tests.append((f"SCALP 1m+{tag}, 2R target", (lambda s_: lambda i: (s_[i], ""))(vs), "target", None,
                          (lambda r_: lambda i: r_[i])(vr)))
        cut = bars[len(bars) // 2]["t"]
        out = {}
        say(f"\n=== {name} ({code})  1m  {len(bars)} bars, {days:.0f} days ===")
        for label, fn, mode, opp, rf in tests:
            tr = simulate(bars, fn, mode, cfg, cost, intraday, opp, rf)
            out[label] = {"all": stats(tr, days), "first_half": stats([t for t in tr if t["t"] < cut], days / 2),
                          "second_half": stats([t for t in tr if t["t"] >= cut], days / 2)}
            say(f"  {label:<46} {fmt(out[label]['all'])}")
            say(f"  {'   first half / second half':<46} {fmt(out[label]['first_half'])}  |  {fmt(out[label]['second_half'])}")
        results[name] = out
    with open(os.path.join(HERE, "scalp_results.json"), "w") as f:
        json.dump(results, f, indent=1)


def stops_main(say):
    """Is the FAST rule's stop the problem? Same 1m signals, different stop distances and targets."""
    cfg = dict(S.DEFAULTS)
    for name, src, code, cost, intraday in [("NIFTY 50", "yahoo", "^NSEI", 0.0003, True),
                                            ("DOW JONES", "yahoo", "^DJI", 0.0002, True),
                                            ("BITCOIN", "binance", "BTCUSDT", 0.0005, False)]:
        try:
            bars = yahoo_1m(code) if src == "yahoo" else binance(code, "1m", 30)
        except Exception as e:
            say(f"{name} 1m: could not download ({e})")
            continue
        days = (bars[-1]["t"] - bars[0]["t"]) / 86400
        sig, risk, _ = S.scalp_signals(bars)
        lb = S.SCALP_DEFAULTS["trigger_lookback"]
        say(f"\n=== {name} 1m, {len(bars)} bars, {days:.0f} days ===")
        say("  NOW = the current setting (stop 1x 5m ATR, target 2R)")
        rows = []
        for mult in (0.5, 1.0, 1.5, 2.0, 3.0):
            for tr_ in (1.0, 1.5, 2.0, 3.0):
                rows.append((f"stop {mult}x 5m ATR, target {tr_}R", "target", tr_,
                             (lambda m_: lambda i: risk[i] and m_ * risk[i])(mult)))
        # stop just beyond the low (high) of the last 10 one-minute candles
        def swing(i):
            if not sig[i]:
                return None
            ref = min(x["l"] for x in bars[i - lb:i + 1]) if sig[i] > 0 else max(x["h"] for x in bars[i - lb:i + 1])
            return abs(bars[i]["c"] - ref) * 1.05
        for tr_ in (1.0, 2.0):
            rows.append((f"stop below last 10-candle low, target {tr_}R", "target", tr_, swing))
        rows.append(("stop 1.5x 5m ATR, trailing stop", "trail", 2.0, lambda i: risk[i] and 1.5 * risk[i]))
        for label, mode, tr_, rf in rows:
            c = dict(cfg, target_r=tr_)
            trades = simulate(bars, lambda i: (sig[i], ""), mode, c, cost, intraday, None, rf)
            st = stats(trades, days)
            quick = sum(1 for t in trades if t["why"] == "stop" and t["bars"] <= 5)
            tag = "NOW " if label == "stop 1.0x 5m ATR, target 2.0R" else "    "
            say(f"  {tag}{label:<44} {fmt(st)}  stopped in <=5 min: {quick}/{len(trades)}")


def fast_lab_main(say):
    """FAST rule: better entries, stops and exits? Same 1m signals; each variant run on Nifty, Dow and Bitcoin,
    reported for the whole month and for each half (a variant that only works in one half is luck)."""
    for name, src, code, cost, intraday in [("NIFTY 50", "yahoo", "^NSEI", 0.0003, True),
                                            ("DOW JONES", "yahoo", "^DJI", 0.0002, True),
                                            ("BITCOIN", "binance", "BTCUSDT", 0.0005, False)]:
        try:
            bars = yahoo_1m(code) if src == "yahoo" else binance(code, "1m", 30)
        except Exception as e:
            say(f"{name} 1m: could not download ({e})")
            continue
        n, days = len(bars), (bars[-1]["t"] - bars[0]["t"]) / 86400
        sig, _, _ = S.scalp_signals(bars)
        v5 = S.htf_view(bars, 5, S.SCALP_DEFAULTS["htf_ema"])
        v20 = S.htf_view(bars, 20, S.SCALP_DEFAULTS["htf_ema"])
        keys = S.session_buckets(bars, 5)
        c5, idx = [], []           # 5-minute candles and, per 1m bar, the index of its 5m candle
        for b, k in zip(bars, keys):
            if not c5 or c5[-1]["k"] != k:
                c5.append({"k": k, "h": b["h"], "l": b["l"]})
            else:
                c5[-1]["h"], c5[-1]["l"] = max(c5[-1]["h"], b["h"]), min(c5[-1]["l"], b["l"])
            idx.append(len(c5) - 1)

        def structure(i, side, a5):   # beyond the last 3 finished 5m candles' low (high), plus a 0.2 ATR cushion
            j = idx[i] - 1
            if j < 3:
                return None
            if side > 0:
                return min(c["l"] for c in c5[j - 2:j + 1]) - 0.2 * a5
            return max(c["h"] for c in c5[j - 2:j + 1]) + 0.2 * a5

        def run(entry="market", stop="atr1", exit_="target2", one_per_leg=False):
            trades, pos, pend, leg_used = [], None, None, {}
            for i in range(30, n - 1):
                b, nxt = bars[i], bars[i + 1]
                end = intraday and nxt["day"] != b["day"]
                a5 = v5[i][1]
                if pos:
                    sd, px_ = pos["side"], None
                    if (sd > 0 and b["l"] <= pos["stop"]) or (sd < 0 and b["h"] >= pos["stop"]):
                        px_ = min(pos["stop"], b["o"]) if sd > 0 else max(pos["stop"], b["o"])
                    elif pos.get("target") and ((sd > 0 and b["h"] >= pos["target"]) or (sd < 0 and b["l"] <= pos["target"])):
                        px_ = max(pos["target"], b["o"]) if sd > 0 else min(pos["target"], b["o"])
                    elif exit_ == "trend" and v5[i][0] == -sd:
                        px_ = b["c"]                      # 5-minute trend turned against the trade
                    elif end:
                        px_ = b["c"]
                    if px_ is not None:
                        r = (sd * (px_ - pos["entry"]) - 2 * cost * pos["entry"]) / pos["risk"]
                        trades.append({"t": pos["t"], "r": r, "ret": r * 0.005})   # 0.5% of capital risked per trade
                        pos = None
                    else:
                        pos["best"] = max(pos["best"], b["c"]) if sd > 0 else min(pos["best"], b["c"])
                        if exit_ in ("trail", "be_trail", "trend") and a5:
                            if exit_ == "be_trail" and sd * (pos["best"] - pos["entry"]) >= pos["risk"]:
                                pos["stop"] = max(pos["stop"], pos["entry"]) if sd > 0 else min(pos["stop"], pos["entry"])
                            if exit_ != "trend" or sd * (pos["best"] - pos["entry"]) >= pos["risk"]:
                                t = pos["best"] - sd * 2.0 * a5
                                pos["stop"] = max(pos["stop"], t) if sd > 0 else min(pos["stop"], t)
                if pos or end or not a5:
                    if end:
                        pend = None
                    continue
                if intraday and any(bars[min(i + k, n - 1)]["day"] != b["day"] for k in range(1, 4)):
                    continue
                # a waiting pullback order: filled when price comes back to the limit
                if pend:
                    sd = pend["side"]
                    if i > pend["until"] or v5[i][0] != sd or v20[i][0] != sd:
                        pend = None
                    elif (sd > 0 and nxt["l"] <= pend["limit"]) or (sd < 0 and nxt["h"] >= pend["limit"]):
                        fill = min(pend["limit"], nxt["o"]) if sd > 0 else max(pend["limit"], nxt["o"])
                        pos = open_pos(sd, fill, i + 1, pend["a5"], nxt["t"], stop, exit_)
                        pend = None
                        continue
                s_ = sig[i]
                if not s_:
                    continue
                if one_per_leg:   # one trade per 20-minute trend leg: skip while the last trade's leg is unchanged
                    if leg_used.get(s_) is not None and all(v20[k][0] == s_ for k in range(leg_used[s_], i + 1)):
                        continue
                    leg_used[s_] = i
                if entry == "market":
                    pos = open_pos(s_, nxt["o"], i + 1, a5, nxt["t"], stop, exit_, i)
                else:   # wait for a pullback of half a 5m candle (or to the structure level), up to 15 minutes
                    pend = {"side": s_, "limit": b["c"] - s_ * 0.5 * a5, "until": i + 15, "a5": a5}
            return trades

        def open_pos(sd, entry, i, a5, t, stop, exit_, sig_i=None):
            k = sig_i if sig_i is not None else i - 1
            if stop.startswith("atr"):
                risk = float(stop[3:]) * a5
            else:
                lvl = structure(k, sd, a5)
                risk = sd * (entry - lvl) if lvl is not None else 1.5 * a5
                risk = min(max(risk, 0.75 * a5), 3 * a5)   # keep it between 0.75 and 3 ATR
            tgt = None
            if exit_.startswith("target"):
                tgt = entry + sd * float(exit_[6:]) * risk
            return {"side": sd, "entry": entry, "risk": risk, "stop": entry - sd * risk, "target": tgt,
                    "best": entry, "t": t}

        variants = [
            ("NOW: market entry, stop 1x ATR, target 2R", dict()),
            ("stop 2x ATR, target 2R", dict(stop="atr2")),
            ("stop under 5m swing, target 2R", dict(stop="swing")),
            ("stop under 5m swing, trail 2x ATR after 1R", dict(stop="swing", exit_="trend")),
            ("stop 2x ATR, breakeven at 1R then trail", dict(stop="atr2", exit_="be_trail")),
            ("stop 2x ATR, trail 2x ATR", dict(stop="atr2", exit_="trail")),
            ("pullback entry, stop 1x ATR, target 2R", dict(entry="pullback")),
            ("pullback entry, stop 2x ATR, target 2R", dict(entry="pullback", stop="atr2")),
            ("pullback entry, 5m swing stop, target 2R", dict(entry="pullback", stop="swing")),
            ("pullback entry, 5m swing stop, trail after 1R", dict(entry="pullback", stop="swing", exit_="trend")),
            ("pullback, 2x ATR stop, breakeven+trail", dict(entry="pullback", stop="atr2", exit_="be_trail")),
            ("one trade per trend leg, 5m swing stop, trail", dict(stop="swing", exit_="trend", one_per_leg=True)),
            ("one per leg, pullback, 5m swing stop, trail", dict(entry="pullback", stop="swing", exit_="trend", one_per_leg=True)),
        ]
        cut = bars[n // 2]["t"]
        say(f"\n=== {name} 1m, {n} bars, {days:.0f} days (0.5% of capital risked per trade) ===")
        for label, kw in variants:
            tr = run(**kw)
            a, h1, h2 = stats(tr, days), stats([t for t in tr if t["t"] < cut], days / 2), stats([t for t in tr if t["t"] >= cut], days / 2)
            say(f"  {label:<48} {fmt(a)}")
            say(f"  {'    1st half | 2nd half':<48} {fmt(h1)} | {fmt(h2)}")


def _daily_dir(daily):
    """day -> supertrend direction of the last FINISHED daily candle before that day."""
    st = S.supertrend(daily)
    days = [b["day"] for b in daily]
    import bisect

    def f(day):
        j = bisect.bisect_left(days, day) - 1
        return st[j] if j >= 0 else 0
    return f


def _htf_adx(bars, minutes, n=14):
    """ADX of the last completed `minutes` candle, for each base bar."""
    keys = S.session_buckets(bars, minutes)
    htf, idx_of = [], []
    for b, k in zip(bars, keys):
        if not htf or htf[-1]["k"] != k:
            htf.append({"k": k, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]})
        else:
            h = htf[-1]
            h["h"], h["l"], h["c"] = max(h["h"], b["h"]), min(h["l"], b["l"]), b["c"]
        idx_of.append(len(htf) - 1)
    a = S.adx(htf, n)
    return [a[j - 1] if j >= 1 else 0.0 for j in idx_of]


def _session_minutes(bars):
    out, day, start = [], None, 0
    for b in bars:
        if b.get("day") != day:
            day, start = b.get("day"), b["t"]
        out.append((b["t"] - start) / 60)
    return out


FILTERS = [
    ("no filter (current)", set()),
    ("T: with daily trend", {"T"}),
    ("M: skip first 15 min + 11:30-1:30", {"M"}),
    ("A: 15m ADX >= 20 (no chop)", {"A"}),
    ("T+M", {"T", "M"}),
    ("T+A", {"T", "A"}),
    ("T+M+A", {"T", "M", "A"}),
]


def filters_main(say):
    """Do simple filters turn the fast (1m) and intraday (15m ORB) rules profitable?"""
    cfg = dict(S.DEFAULTS)
    board = {}
    say("Each line: all data, then first half | second half. A filter only counts if BOTH halves make money.")
    for name, src, code, cost, intraday in [x for x in INSTRUMENTS if x[0] in ("NIFTY 50", "SENSEX", "BANK NIFTY", "BITCOIN")]:
        try:
            daily = load(name, src, code, "1d")
        except Exception as e:
            say(f"{name}: no daily data ({e})")
            continue
        ddir = _daily_dir(daily)
        sets = []
        path = os.path.join(CACHE, f"{code.replace('^', '_')}_1m.json")
        os.makedirs(CACHE, exist_ok=True)
        try:
            if os.path.exists(path) and time.time() - os.path.getmtime(path) < 12 * 3600:
                b1 = json.load(open(path))
            else:
                b1 = yahoo_1m(code) if src == "yahoo" else binance(code, "1m", 60)
                json.dump(b1, open(path, "w"))
            sig, risk, _ = S.scalp_signals(b1)
            sets.append(("FAST 1m/5m/20m", b1, (lambda s_: lambda i: (s_[i], ""))(sig), (lambda r_: lambda i: r_[i])(risk),
                         _htf_adx(b1, 15)))
        except Exception as e:
            say(f"{name} 1m: could not download ({e})")
        if intraday:
            try:
                b15 = load(name, src, code, "15m")
                c15 = dict(cfg, bar_seconds=900)
                ind = S.indicators(b15, c15)
                orb = (lambda bb, ii, cc: lambda i: S.signal_at(bb, ii, i, cc, "orb", True))(b15, ind, c15)
                sets.append(("INTRADAY 15m ORB", b15, orb, None, S.adx(b15, 14)))
            except Exception as e:
                say(f"{name} 15m: could not download ({e})")
        for label, bars, base, rf, adxs in sets:
            mins = _session_minutes(bars)
            step = (bars[1]["t"] - bars[0]["t"]) / 60 if len(bars) > 1 else 1
            days = (bars[-1]["t"] - bars[0]["t"]) / 86400
            cut = bars[len(bars) // 2]["t"]
            c = dict(cfg, bar_seconds=int(step * 60))
            say(f"\n=== {name}  {label}  ({days:.0f} days) ===")
            for fl, need in FILTERS:
                def fn(i, base=base, need=need, bars=bars, mins=mins, adxs=adxs, step=step):
                    side, why = base(i)
                    if not side:
                        return 0, ""
                    if "T" in need and ddir(bars[i]["day"]) != side:
                        return 0, ""
                    if "M" in need and intraday:
                        m = mins[i] + step  # minutes since open when this candle closes
                        if m < 15 or 135 <= m < 255:
                            return 0, ""
                    if "A" in need and (adxs[i] or 0) < 20:
                        return 0, ""
                    return side, why
                tr = simulate(bars, fn, "target", c, cost, intraday, None, rf)
                r = {"all": stats(tr, days), "first_half": stats([t for t in tr if t["t"] < cut], days / 2),
                     "second_half": stats([t for t in tr if t["t"] >= cut], days / 2)}
                say(f"  {fl:<36} {fmt(r['all'])}")
                say(f"  {'   first half | second half':<36} {fmt(r['first_half'])}  |  {fmt(r['second_half'])}")
                ok = all(r[h].get("trades", 0) >= 3 and r[h].get("total_return_pct", 0) > 0 for h in ("first_half", "second_half"))
                board.setdefault((label, fl), []).append((name, ok, r["all"].get("total_return_pct", 0), r["all"].get("trades", 0)))
    say("\n=== SUMMARY: markets where the filter made money in BOTH halves ===")
    for (label, fl), rows in board.items():
        wins = [n for n, ok, _, _ in rows if ok]
        say(f"  {label:<17} {fl:<36} {len(wins)}/{len(rows)}  {', '.join(wins) or '-'}"
            f"   (totals: {', '.join(f'{n} {t:+.1f}% ({k})' for n, _, t, k in rows)})")


def load_csv(path):
    bars = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            r = {k.strip().lower(): v for k, v in r.items()}
            t = r.get("t") or r.get("time") or r.get("date") or r.get("datetime")
            try:
                ts = float(t)
            except ValueError:
                ts = datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
            g = lambda *ks: float(next(r[k] for k in ks if r.get(k) not in (None, "")))
            bars.append({"t": ts, "o": g("o", "open"), "h": g("h", "high"), "l": g("l", "low"),
                         "c": g("c", "close"), "v": float(r.get("v") or r.get("volume") or 0),
                         "day": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")})
    return bars


# ───────────────────────── simulation ─────────────────────────

def simulate(bars, signals_fn, exit_mode, cfg, cost, intraday, opposite=None, risk_fn=None):
    """signals_fn(i) -> (+1/-1/0, reason). Entry at next bar open.
    exit_mode: 'target' (stop + fixed R target), 'trail' (stop + chandelier trail),
               'opposite' (v1 style: hold until the opposite signal).
    Intraday markets are closed out at the end of each session."""
    a = S.atr(bars, cfg["atr_len"])
    trades, pos = [], None
    n = len(bars)
    for i in range(n - 1):
        b, nxt = bars[i], bars[i + 1]
        last_of_session = intraday and nxt["day"] != b["day"]
        if pos:
            side = pos["side"]
            exit_px, why = None, ""
            # stop / target hit inside this bar? (stop assumed first if both)
            if exit_mode != "opposite" or pos.get("stop") is not None:
                if side == 1 and b["l"] <= pos["stop"]:
                    exit_px, why = min(pos["stop"], b["o"]), "stop"
                elif side == -1 and b["h"] >= pos["stop"]:
                    exit_px, why = max(pos["stop"], b["o"]), "stop"
                elif exit_mode == "target" and side == 1 and b["h"] >= pos["target"]:
                    exit_px, why = max(pos["target"], b["o"]), "target"
                elif exit_mode == "target" and side == -1 and b["l"] <= pos["target"]:
                    exit_px, why = min(pos["target"], b["o"]), "target"
            if exit_px is None and exit_mode == "opposite" and opposite and opposite(i) == -side:
                exit_px, why = b["c"], "opposite"
            if exit_px is None and last_of_session:
                exit_px, why = b["c"], "session end"
            if exit_px is None and exit_mode == "trail":
                pos["best"] = max(pos["best"], b["c"]) if side == 1 else min(pos["best"], b["c"])
                trail = pos["best"] - side * cfg["trail_atr"] * a[i]
                pos["stop"] = max(pos["stop"], trail) if side == 1 else min(pos["stop"], trail)
            if exit_px is not None:
                gross = side * (exit_px - pos["entry"]) / pos["entry"]
                net = gross - 2 * cost
                r = (side * (exit_px - pos["entry"]) - 2 * cost * pos["entry"]) / pos["risk"]
                trades.append({"t": pos["t"], "side": side, "ret": net, "r": r, "why": why,
                               "bars": i - pos["i"]})
                pos = None
        if pos is None and not last_of_session:
            # no fresh intraday entries in the last 3 bars of a session
            if intraday and any(bars[min(i + k, n - 1)]["day"] != b["day"] for k in range(1, 4)):
                continue
            s, _ = signals_fn(i)
            if s:
                entry = nxt["o"]
                risk = risk_fn(i) if risk_fn else cfg["stop_atr"] * a[i]
                if not risk or risk <= 0:
                    continue
                pos = {"side": s, "entry": entry, "risk": risk, "i": i + 1, "t": nxt["t"], "best": entry,
                       "stop": entry - s * risk, "target": entry + s * cfg["target_r"] * risk}
                if exit_mode == "opposite":
                    pos["stop"] = None
    return trades


def stats(trades, days):
    n = len(trades)
    if n == 0:
        return {"trades": 0}
    wins = [t for t in trades if t["ret"] > 0]
    gp = sum(t["ret"] for t in wins)
    gl = -sum(t["ret"] for t in trades if t["ret"] <= 0)
    eq, peak, mdd = 1.0, 1.0, 0.0
    for t in trades:
        eq *= 1 + t["ret"]
        peak = max(peak, eq)
        mdd = max(mdd, 1 - eq / peak)
    return {
        "trades": n,
        "per_week": round(n / max(days, 1) * 7, 1),
        "hit_rate": round(100 * len(wins) / n, 1),
        "avg_trade_pct": round(100 * sum(t["ret"] for t in trades) / n, 3),
        "avg_R": round(sum(t["r"] for t in trades) / n, 2),
        "profit_factor": round(gp / gl, 2) if gl > 0 else None,
        "total_return_pct": round(100 * (eq - 1), 1),
        "max_drawdown_pct": round(100 * mdd, 1),
    }


def strategies(bars, cfg, intraday):
    ind = S.indicators(bars, cfg)
    old = S.old_signals(bars)
    cache = {}

    def rule(r):
        def f(i):
            k = (r, i)
            if k not in cache:
                cache[k] = S.signal_at(bars, ind, i, cfg, r, intraday)
            return cache[k]
        return f

    oldf = lambda i: (old[i], "")
    return [
        ("OLD v1: buy on BUYERS, sell on SELLERS", oldf, "opposite", lambda i: old[i]),
        ("OLD v1 + ATR stop/2R target", oldf, "target", None),
        ("v2 pullback, 2R target", rule("pullback"), "target", None),
        ("v2 pullback, trailing stop", rule("pullback"), "trail", None),
        ("v2 breakout, 2R target", rule("breakout"), "target", None),
        ("v2 breakout, trailing stop", rule("breakout"), "trail", None),
        ("v2 supertrend+200EMA, trailing", rule("supertrend"), "trail", None),
        ("v2 opening range breakout, 2R", rule("orb"), "target", None),
        ("v2 COMBO (pullback or breakout), 2R", rule("combo"), "target", None),
        ("v2 COMBO (pullback or breakout), trail", rule("combo"), "trail", None),
    ]


def run_one(bars, cost, intraday_market, tf, cfg):
    if len(bars) < 300:
        return None
    gaps = sorted(b["t"] - a["t"] for a, b in zip(bars[:-1], bars[1:]))
    intraday = intraday_market and tf != "1d" and gaps[len(gaps) // 2] < 20 * 3600
    cfg = dict(cfg, bar_seconds=gaps[len(gaps) // 2])
    days = (bars[-1]["t"] - bars[0]["t"]) / 86400
    half = len(bars) // 2
    out = {"bars": len(bars), "from": datetime.fromtimestamp(bars[0]["t"], tz=timezone.utc).strftime("%Y-%m-%d"),
           "to": datetime.fromtimestamp(bars[-1]["t"], tz=timezone.utc).strftime("%Y-%m-%d"), "results": {}}
    for name, fn, mode, opp in strategies(bars, cfg, intraday):
        tr = simulate(bars, fn, mode, cfg, cost, intraday, opp)
        cut = bars[half]["t"]
        out["results"][name] = {
            "all": stats(tr, days),
            "first_half": stats([t for t in tr if t["t"] < cut], days / 2),
            "second_half": stats([t for t in tr if t["t"] >= cut], days / 2),
        }
    return out


def fmt(r):
    if not r or not r.get("trades"):
        return "no trades"
    pf = r["profit_factor"] if r["profit_factor"] is not None else "-"
    return (f"{r['trades']:>4} trades ({r['per_week']}/wk)  hit {r['hit_rate']:>5}%  "
            f"avg {r['avg_R']:>5}R  PF {pf:>5}  total {r['total_return_pct']:>7}%  maxDD {r['max_drawdown_pct']:>5}%")


def main():
    args = sys.argv[1:]
    cfg = dict(S.DEFAULTS)
    lines, results = [], {}

    def say(s=""):
        print(s)
        lines.append(s)

    if "filters" in args:
        filters_main(say)
        with open(os.path.join(HERE, "filter_results.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        say("\nSaved filter_results.txt")
        return
    if "fastlab" in args:
        fast_lab_main(say)
        return
    if "stops" in args:
        stops_main(say)
        return
    if "scalp" in args:
        scalp_main(say)
        with open(os.path.join(HERE, "scalp_results.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        say("\nSaved scalp_results.txt")
        return
    if "--csv" in args:
        path = args[args.index("--csv") + 1]
        bars = load_csv(path)
        r = run_one(bars, 0.0003, "--24h" not in args, "custom", cfg)
        say(f"{path}: {len(bars)} bars")
        for k, v in r["results"].items():
            say(f"  {k:<42} {fmt(v['all'])}")
        return
    tfs = [a for a in args if a in TIMEFRAMES] or list(TIMEFRAMES)
    insts = [x for x in INSTRUMENTS if "--quick" not in args or x[0] in QUICK]
    for name, src, code, cost, intraday in insts:
        for tf in tfs:
            try:
                bars = load(name, src, code, tf)
            except Exception as e:
                say(f"{name} {tf}: could not download ({e})")
                continue
            r = run_one(bars, cost, intraday, tf, cfg)
            if r is None:
                say(f"{name} {tf}: not enough data ({len(bars)} bars)")
                continue
            results[f"{name}|{tf}"] = r
            say(f"\n=== {name} ({code})  {tf}  {r['from']} to {r['to']}  {r['bars']} bars ===")
            for k, v in r["results"].items():
                say(f"  {k:<42} {fmt(v['all'])}")
    with open(os.path.join(HERE, "backtest_results.json"), "w") as f:
        json.dump(results, f, indent=1)
    with open(os.path.join(HERE, "backtest_results.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    say("\nSaved backtest_results.json and backtest_results.txt")


if __name__ == "__main__":
    main()
