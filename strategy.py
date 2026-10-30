"""
Signal rules v2: trend-confirmed entries with stop and target.

Pure Python (standard library only). Used by both backtest.py and
signal_server.py so the live alerts follow exactly the rules that were tested.

A bar is a dict: {"t": epoch seconds, "o", "h", "l", "c", "v", "day": session key}.
Every rule only looks at CLOSED bars, so a signal never changes after it prints.
"""
import math

# Default settings. They are standard textbook values, chosen before testing,
# not tuned to the backtest.
DEFAULTS = {
    "ema_fast": 20,
    "ema_slow": 50,
    "ema_trend": 200,     # long-term regime filter
    "atr_len": 14,
    "rsi_len": 14,
    "adx_len": 14,
    "adx_min": 20,        # below this the market is ranging: no trades
    "vol_len": 20,
    "vol_mult": 1.2,      # breakout volume must be this x average (if volume exists)
    "breakout_len": 20,
    "stop_atr": 1.5,      # initial stop distance in ATRs
    "target_r": 2.0,      # target = this many times the risk
    "trail_atr": 3.0,     # chandelier trailing stop distance in ATRs
    "pullback_lookback": 5,
    "use_vwap": True,     # intraday only: longs above VWAP, shorts below
    "daily_filter": True, # intraday only: trade only in the direction of the daily trend
    "daily_ema": 20,      # daily trend = yesterday's close vs 20-day EMA
    "orb_minutes": 15,    # opening range length for the ORB rule
}


# ───────────────────────── indicators ─────────────────────────

def ema(xs, n):
    out, k, e = [], 2.0 / (n + 1), None
    for x in xs:
        e = x if e is None else e + k * (x - e)
        out.append(e)
    return out


def sma(xs, n):
    out, s = [], 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        out.append(s / min(i + 1, n))
    return out


def wilder(xs, n):
    out, e = [], None
    for x in xs:
        e = x if e is None else e + (x - e) / n
        out.append(e)
    return out


def true_range(bars):
    tr = []
    for i, b in enumerate(bars):
        if i == 0:
            tr.append(b["h"] - b["l"])
        else:
            pc = bars[i - 1]["c"]
            tr.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    return tr


def atr(bars, n):
    return wilder(true_range(bars), n)


def rsi(closes, n):
    gains, losses = [0.0], [0.0]
    for a, b in zip(closes[:-1], closes[1:]):
        gains.append(max(b - a, 0.0))
        losses.append(max(a - b, 0.0))
    ag, al = wilder(gains, n), wilder(losses, n)
    return [50.0 if g + l == 0 else 100.0 * g / (g + l) for g, l in zip(ag, al)]


def adx(bars, n):
    pdm, ndm = [0.0], [0.0]
    for p, b in zip(bars[:-1], bars[1:]):
        up, dn = b["h"] - p["h"], p["l"] - b["l"]
        pdm.append(up if up > dn and up > 0 else 0.0)
        ndm.append(dn if dn > up and dn > 0 else 0.0)
    tr = wilder(true_range(bars), n)
    sp, sn = wilder(pdm, n), wilder(ndm, n)
    dx = []
    for t, p, m in zip(tr, sp, sn):
        pdi = 100 * p / t if t > 0 else 0.0
        ndi = 100 * m / t if t > 0 else 0.0
        dx.append(100 * abs(pdi - ndi) / (pdi + ndi) if pdi + ndi > 0 else 0.0)
    return wilder(dx, n)


def vwap(bars):
    """Session VWAP, reset at each new session ("day" key). Without volume it
    falls back to the session's average typical price."""
    out, pv, vv, day = [], 0.0, 0.0, None
    for b in bars:
        if b.get("day") != day:
            pv, vv, day = 0.0, 0.0, b.get("day")
        w = b["v"] if b["v"] and b["v"] > 0 else 1.0
        pv += w * (b["h"] + b["l"] + b["c"]) / 3
        vv += w
        out.append(pv / vv)
    return out


def supertrend(bars, n=10, mult=3.0):
    a = atr(bars, n)
    direction, up_band, dn_band, out = 1, None, None, []
    for i, b in enumerate(bars):
        mid = (b["h"] + b["l"]) / 2
        ub, lb = mid + mult * a[i], mid - mult * a[i]
        if i > 0:
            pc = bars[i - 1]["c"]
            lb = max(lb, dn_band) if pc > dn_band else lb
            ub = min(ub, up_band) if pc < up_band else ub
            if direction == -1 and b["c"] > up_band:
                direction = 1
            elif direction == 1 and b["c"] < dn_band:
                direction = -1
        up_band, dn_band = ub, lb
        out.append(direction)
    return out


# ───────────────────────── rule sets ─────────────────────────

def daily_trend(bars, n):
    """For each intraday bar: +1 if the previous session closed above the
    n-session EMA of session closes, -1 if below. Uses only finished sessions.
    None while there are fewer than 5 finished sessions (filter not applied)."""
    out, closes, e, k, day, cur = [], [], None, 2.0 / (n + 1), None, None
    for b in bars:
        if b.get("day") != day:
            if day is not None:
                last = closes[-1]
                e = last if e is None else e + k * (last - e)
                cur = None if len(closes) < 5 else (1 if last > e else -1 if last < e else 0)
            day = b.get("day")
            closes.append(b["c"])
        closes[-1] = b["c"]
        out.append(cur)
    return out


def session_index(bars):
    """Position of each bar inside its session (0 = first bar)."""
    out, day, k = [], None, 0
    for b in bars:
        k = 0 if b.get("day") != day else k + 1
        day = b.get("day")
        out.append(k)
    return out


def indicators(bars, cfg):
    c = [b["c"] for b in bars]
    v = [b["v"] or 0.0 for b in bars]
    return {
        "c": c,
        "ef": ema(c, cfg["ema_fast"]),
        "es": ema(c, cfg["ema_slow"]),
        "et": ema(c, cfg["ema_trend"]),
        "atr": atr(bars, cfg["atr_len"]),
        "rsi": rsi(c, cfg["rsi_len"]),
        "adx": adx(bars, cfg["adx_len"]),
        "vwap": vwap(bars),
        "vavg": sma(v, cfg["vol_len"]),
        "has_vol": sum(1 for x in v if x > 0) > len(v) * 0.5,
        "st": supertrend(bars),
        "dtrend": daily_trend(bars, cfg["daily_ema"]),
        "sidx": session_index(bars),
    }


def trend_dir(ind, i):
    """+1 uptrend, -1 downtrend, 0 no clear trend."""
    c, ef, es, et = ind["c"][i], ind["ef"][i], ind["es"][i], ind["et"][i]
    if ef > es and c > et and es > ind["es"][max(i - 5, 0)]:
        return 1
    if ef < es and c < et and es < ind["es"][max(i - 5, 0)]:
        return -1
    return 0


def signal_at(bars, ind, i, cfg, rule, intraday):
    """Returns +1 (buy), -1 (sell) or 0 for the bar that just CLOSED at index i.
    The trade is taken at the next bar's open."""
    if i < max(cfg["ema_trend"], 60):
        return 0, ""
    b, p = bars[i], bars[i - 1]
    td = trend_dir(ind, i)
    vw = ind["vwap"][i]
    vol_ok = (not ind["has_vol"]) or (b["v"] or 0) >= cfg["vol_mult"] * ind["vavg"][i - 1]
    trending = ind["adx"][i] >= cfg["adx_min"]
    vwap_long = (not intraday) or (not cfg["use_vwap"]) or b["c"] > vw
    vwap_short = (not intraday) or (not cfg["use_vwap"]) or b["c"] < vw
    if intraday and cfg["daily_filter"]:
        dt = ind["dtrend"][i]
        vwap_long = vwap_long and dt in (None, 1)
        vwap_short = vwap_short and dt in (None, -1)

    if rule == "orb":
        if not intraday:
            return 0, ""
        k = max(1, round(cfg["orb_minutes"] * 60 / cfg.get("bar_seconds", 300)))
        j = ind["sidx"][i]
        if j < k or j > 4 * k + 12:      # only in the first part of the session
            return 0, ""
        first = i - j
        orh = max(x["h"] for x in bars[first:first + k])
        orl = min(x["l"] for x in bars[first:first + k])
        # only the first close outside the range counts
        prior_out = any(bars[m]["c"] > orh or bars[m]["c"] < orl for m in range(first + k, i))
        if prior_out:
            return 0, ""
        if b["c"] > orh and vwap_long:
            return 1, "Opening range breakout up"
        if b["c"] < orl and vwap_short:
            return -1, "Opening range breakdown"
        return 0, ""

    if rule == "pullback":
        lb = cfg["pullback_lookback"]
        touched_up = any(bars[k]["l"] <= ind["ef"][k] for k in range(i - lb, i))
        touched_dn = any(bars[k]["h"] >= ind["ef"][k] for k in range(i - lb, i))
        if td == 1 and trending and touched_up and b["c"] > p["h"] and b["c"] > ind["ef"][i] \
                and 45 <= ind["rsi"][i] <= 70 and vwap_long:
            return 1, "Uptrend pullback resumed"
        if td == -1 and trending and touched_dn and b["c"] < p["l"] and b["c"] < ind["ef"][i] \
                and 30 <= ind["rsi"][i] <= 55 and vwap_short:
            return -1, "Downtrend pullback resumed"
        return 0, ""

    if rule == "breakout":
        n = cfg["breakout_len"]
        hh = max(x["h"] for x in bars[i - n:i])
        ll = min(x["l"] for x in bars[i - n:i])
        if td == 1 and trending and b["c"] > hh and vol_ok and vwap_long:
            return 1, f"{n}-bar breakout with volume"
        if td == -1 and trending and b["c"] < ll and vol_ok and vwap_short:
            return -1, f"{n}-bar breakdown with volume"
        return 0, ""

    if rule == "supertrend":
        st, stp = ind["st"][i], ind["st"][i - 1]
        if st == 1 and stp == -1 and b["c"] > ind["et"][i] and vwap_long:
            return 1, "Supertrend flipped up, above 200 EMA"
        if st == -1 and stp == 1 and b["c"] < ind["et"][i] and vwap_short:
            return -1, "Supertrend flipped down, below 200 EMA"
        return 0, ""

    if rule == "combo":  # v2 default: pullback OR breakout, both trend-confirmed
        for r in ("pullback", "breakout"):
            s, why = signal_at(bars, ind, i, cfg, r, intraday)
            if s:
                return s, why
        return 0, ""

    raise ValueError(rule)


def bias(bars, ind, i, cfg, intraday):
    """Panel colour for the latest closed bar plus the checklist behind it.
    'up' = all trend checks bullish, 'down' = all bearish, 'none' = mixed."""
    td = trend_dir(ind, i)
    c = ind["c"][i]
    checks, votes = [], []
    checks.append(("✓ " if td else "✗ ") + {1: "EMAs up", -1: "EMAs down", 0: "EMAs mixed"}[td])
    votes.append(td)
    trending = ind["adx"][i] >= cfg["adx_min"]
    checks.append(("✓ " if trending else "✗ ") + f"ADX {ind['adx'][i]:.0f}" + ("" if trending else " (ranging)"))
    if intraday and cfg["use_vwap"]:
        vw = 1 if c > ind["vwap"][i] else -1
        checks.append(f"{'✓' if vw == td else '✗'} {'above' if vw > 0 else 'below'} VWAP")
        votes.append(vw)
    if intraday and cfg["daily_filter"] and ind["dtrend"][i] is not None:
        dt = ind["dtrend"][i]
        checks.append(f"{'✓' if dt == td else '✗'} daily trend {'up' if dt > 0 else 'down' if dt < 0 else 'flat'}")
        votes.append(dt)
    if td != 0 and trending and all(v == td for v in votes):
        return ("up" if td == 1 else "down"), checks
    return "none", checks


def levels(entry, side, atr_now, cfg):
    risk = cfg["stop_atr"] * atr_now
    stop = entry - side * risk
    target = entry + side * cfg["target_r"] * risk
    return stop, target, risk


# ───────────────────────── old v1 logic (for comparison) ─────────────────────────

def old_pressure(bars, smooth=3, vol_len=20, spike=2.0):
    """The v1 'buyer/seller pressure' score rebuilt from OHLCV bars: close
    location in the bar x volume spike, or RSI(14) when there is no volume,
    smoothed with EMA(3)."""
    c = [b["c"] for b in bars]
    v = [b["v"] or 0.0 for b in bars]
    has_vol = sum(1 for x in v if x > 0) > len(v) * 0.5
    vavg = sma(v, vol_len)
    r = rsi(c, 14)
    raw = []
    for i, b in enumerate(bars):
        if has_vol:
            rng = b["h"] - b["l"]
            delta = 2 * ((b["c"] - b["l"]) / rng) - 1 if rng > 0 else 0.0
            ratio = v[i] / vavg[i - 1] if i > 0 and vavg[i - 1] > 0 else 1.0
            raw.append(100 * delta * min(ratio / spike, 1.0))
        else:
            raw.append(2 * (r[i] - 50))
    return ema(raw, smooth)


def old_signals(bars, threshold=40):
    p = old_pressure(bars)
    sig = [0] * len(bars)
    for i in range(1, len(bars)):
        if p[i] >= threshold > p[i - 1]:
            sig[i] = 1
        elif p[i] <= -threshold < p[i - 1]:
            sig[i] = -1
    return sig


# ───────────────────────── fast multi-timeframe "scalp" rule ─────────────────────────
# Trigger on 1-minute candles, only when the 5-minute and 20-minute trends agree
# and buyers (or sellers) are visibly pushing on the 1-minute candle.

SCALP_DEFAULTS = {
    "htf_minutes": (5, 20),   # confirmation timeframes, built from the 1-minute candles
    "htf_ema": 20,
    "trigger_lookback": 10,   # 1-minute close must break the last 10 candles' high (low)
    "push_vol_mult": 1.5,     # push candle volume >= 1.5x average (when volume exists)
    "push_close_loc": 0.7,    # ...and closes in the top 30% of its range (bottom 30% for sells)
    "push_range_atr": 1.0,    # no volume (index): push candle range >= 1 x 1-minute ATR
    "stop_htf_atr": 1.0,      # stop = 1 x ATR(14) of the 5-minute candles
    "stop_mode": "atr",       # "swing": stop beyond the last 3 finished 5m candles' low (high) + 0.2 ATR,
                              # kept between 0.75 and 3 x the 5m ATR (did best on Nifty, 30-day test 30 Oct 2026)
    "target_r": 2.0,
    "skip_first_min": 5,      # no trades in the first 5 minutes of a session
}


def session_buckets(bars, minutes):
    """Bucket number of each bar for candles of `minutes`, counted from the start
    of its session (so 20-minute candles line up with the market open)."""
    sec = minutes * 60
    out, day, start = [], None, 0
    for b in bars:
        if b.get("day") != day:
            day, start = b.get("day"), b["t"]
        out.append((day, int((b["t"] - start) // sec)))
    return out


def htf_view(bars, minutes, ema_len, atr_len=14):
    """For each base bar: (bias, atr) from the last COMPLETED higher-timeframe candle.
    bias +1 = close above a rising EMA, -1 = below a falling EMA, 0 otherwise."""
    keys = session_buckets(bars, minutes)
    htf, idx_of = [], []
    for b, k in zip(bars, keys):
        if not htf or htf[-1]["k"] != k:
            htf.append({"k": k, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]})
        else:
            h = htf[-1]
            h["h"], h["l"], h["c"] = max(h["h"], b["h"]), min(h["l"], b["l"]), b["c"]
        idx_of.append(len(htf) - 1)
    e = ema([h["c"] for h in htf], ema_len)
    a = atr(htf, atr_len)
    out = []
    for i in range(len(bars)):
        j = idx_of[i] - 1           # last completed higher-timeframe candle
        if j < ema_len + 3:
            out.append((0, None))
            continue
        c, rising = htf[j]["c"], e[j] > e[j - 3]
        bias = 1 if c > e[j] and rising else -1 if c < e[j] and not rising else 0
        out.append((bias, a[j]))
    return out


def scalp_signals(bars, scfg=None):
    """Returns (signals, risks): per 1-minute bar, +1 BUY (call), -1 SELL (put) or 0,
    and the stop distance to use for that bar."""
    p = dict(SCALP_DEFAULTS, **(scfg or {}))
    n = len(bars)
    views = [htf_view(bars, m, p["htf_ema"]) for m in p["htf_minutes"]]
    vw = vwap(bars)
    a1 = atr(bars, 14)
    v = [b["v"] or 0.0 for b in bars]
    has_vol = sum(1 for x in v if x > 0) > n * 0.5
    vavg = sma(v, 20)
    sidx = session_index(bars)
    lb = p["trigger_lookback"]
    sig, risk, why = [0] * n, [None] * n, [""] * n
    c5, i5 = [], []   # 5-minute candles (for the swing stop) and, per 1m bar, its 5m candle
    if p["stop_mode"] == "swing":
        for b, k in zip(bars, session_buckets(bars, 5)):
            if not c5 or c5[-1]["k"] != k:
                c5.append({"k": k, "h": b["h"], "l": b["l"]})
            else:
                c5[-1]["h"], c5[-1]["l"] = max(c5[-1]["h"], b["h"]), min(c5[-1]["l"], b["l"])
            i5.append(len(c5) - 1)
    for i in range(max(lb, 30), n):
        b = bars[i]
        if sidx[i] < p["skip_first_min"] or sidx[i] < lb:
            continue
        biases = [vw_[i][0] for vw_ in views]
        if not all(x == biases[0] for x in biases) or biases[0] == 0:
            continue
        side = biases[0]
        if (side == 1 and b["c"] <= vw[i]) or (side == -1 and b["c"] >= vw[i]):
            continue
        hh = max(x["h"] for x in bars[i - lb:i])
        ll = min(x["l"] for x in bars[i - lb:i])
        if (side == 1 and b["c"] <= hh) or (side == -1 and b["c"] >= ll):
            continue
        rng = b["h"] - b["l"]
        loc = (b["c"] - b["l"]) / rng if rng > 0 else 0.5
        if side == -1:
            loc = 1 - loc
        if loc < p["push_close_loc"]:
            continue
        if has_vol:
            if v[i] < p["push_vol_mult"] * vavg[i - 1]:
                continue
            push = f"volume {v[i] / vavg[i - 1]:.1f}x"
        else:
            if rng < p["push_range_atr"] * a1[i - 1]:
                continue
            push = f"wide candle {rng / a1[i - 1]:.1f}x ATR"
        htf_atr = views[0][i][1]
        if not htf_atr:
            continue
        sig[i] = side
        risk[i] = p["stop_htf_atr"] * htf_atr
        j = i5[i] - 1 if c5 else -1
        if j >= 3:
            last3 = c5[j - 2:j + 1]
            lvl = (min(x["l"] for x in last3) - 0.2 * htf_atr if side == 1
                   else max(x["h"] for x in last3) + 0.2 * htf_atr)
            risk[i] = min(max(side * (b["c"] - lvl), 0.75 * htf_atr), 3 * htf_atr)
        tfs = "/".join(f"{m}m" for m in p["htf_minutes"])
        why[i] = (f"{'Buyers' if side == 1 else 'Sellers'} pushing: 1m broke {lb}-candle {'high' if side == 1 else 'low'}, "
                  f"{push}; {tfs} trend {'up' if side == 1 else 'down'}; {'above' if side == 1 else 'below'} VWAP")
    return sig, risk, why
