#!/usr/bin/env python3
"""
Trade check: Time Machine odds and position size for every new signal.
Standard library only. Idea taken from the MIT "day-trader-skill" pack (Time Machine,
Budget and Risk managers), rebuilt for Indian sessions (9:15-15:30), 24-hour crypto
and daily swing trades.

Time Machine
  Replays the rest of the trade many times (default 5,000) from the instrument's
  OWN recent candles. Each simulated candle is a real past candle from about the
  same time of day, as ratios to the previous close, scaled to the current price:
    Indian / exchange intraday   candle from the same slot of past sessions (+-2),
                                 replay stops 15 min before the close (your exit rule)
    Crypto intraday (24 h)       candle from the same time of day on past days (+-2),
                                 replay runs a fixed number of candles
    Daily swing                  any past daily candle, replay runs 60 trading days
  Stop is checked before the target in every candle (the cautious call). Trailing
  exits move the stop on each close, like the live program.
  Then the same rule is re-run on all the candles already loaded (history check).
  It is a reality check, not a prediction: news, results and gaps can do what the
  last weeks never did.

Position size
  Sizes the trade so a stop-out loses about your max loss per trade, and blocks new
  trades once today's losses reach your daily limit or you hit max trades per day.
  For CALL/PUT alerts the option loss is estimated with delta 0.5 (an at-the-money
  option moves about half as much as the index). Lot sizes change: check IndMoney.

  python tradecheck.py --selftest      quick check on made-up candles
"""
import json
import math
import os
import random
import time

import strategy as S

HORIZON_24H = {"1m": 120, "5m": 96, "15m": 96, "30m": 48, "1h": 48}
HORIZON_DAILY = 60

TM_DEFAULTS = {"enabled": True, "replays": 5000, "seed": None, "window": 2, "min_sessions": 3}

RISK_DEFAULTS = {
    "enabled": True,
    "INR": {"capital": 100000, "max_loss_per_trade": 1000, "daily_loss_limit": 3000},
    "USD": {"capital": 1000, "max_loss_per_trade": 10, "daily_loss_limit": 30},
    "max_trades_per_day": 6,
    "option_delta": 0.5,
    # contract size per lot; check the current lot size in IndMoney before trading
    "lot_size": {"^NSEI": 65, "^BSESN": 20, "^NSEBANK": 30, "NIFTY_MID_SELECT.NS": 120, "^NSEMDCP50": 120},
}

INR_SUFFIX = (".NS", ".BO")
INR_CODES = {"^NSEI", "^BSESN", "^NSEBANK", "^CNXFIN", "^NSEMDCP50", "NIFTY_MID_SELECT.NS"}


def currency(code):
    up = code.upper()
    return "INR" if up in INR_CODES or up.endswith(INR_SUFFIX) or up.startswith(("NFO_", "BFO_")) else "USD"


def money(x, cur):
    sym = "₹" if cur == "INR" else "$"
    return f"{'-' if x < 0 else ''}{sym}{abs(x):,.0f}" if abs(x) >= 100 else f"{'-' if x < 0 else ''}{sym}{abs(x):,.2f}"


# ───────────────────────── Time Machine ─────────────────────────

def _pools(bars, kind, bar_seconds, upto):
    """Ratio candles (o, h, l, c) / previous close, grouped by slot. Uses bars[1:upto]."""
    pools, slots_per_day = {}, 1
    if kind == "daily":
        pools[0] = []
    elif kind == "24h":
        slots_per_day = max(1, 86400 // bar_seconds)
    sidx = S.session_index(bars[:upto]) if kind == "session" else None
    if kind == "session":
        # keep full sessions only (half days and the current one are left out)
        count = {}
        for b in bars[:upto]:
            count[b["day"]] = count.get(b["day"], 0) + 1
        full_len = max(sorted(count.values())[len(count) // 2], 1) if count else 1
        good = {d for d, n in count.items() if n >= 0.9 * full_len}
        slots_per_day = full_len
    for k in range(1, upto):
        pc, b = bars[k - 1]["c"], bars[k]
        if not pc:
            continue
        r = (b["o"] / pc, b["h"] / pc, b["l"] / pc, b["c"] / pc)
        if kind == "daily":
            pools[0].append(r)
        elif kind == "24h":
            pools.setdefault((b["t"] % 86400) // bar_seconds, []).append(r)
        elif b["day"] in good and bars[k - 1]["day"] == b["day"]:
            pools.setdefault(sidx[k], []).append(r)
    sessions = len(good) if kind == "session" else None
    return pools, slots_per_day, sessions


def _pick(pools, slot, window, slots_per_day, kind, rnd):
    if kind == "daily":
        return rnd.choice(pools[0])
    for _ in range(4):
        j = slot + rnd.randint(-window, window)
        if kind == "24h":
            j %= slots_per_day
        else:
            j = min(max(j, 1), slots_per_day - 1)
        p = pools.get(j)
        if p:
            return rnd.choice(p)
    p = pools.get(slot) or pools.get(min(pools, key=lambda s: abs(s - slot)))
    return rnd.choice(p)


def time_machine(bars, side, entry, stop, target, atr_now, kind, bar_seconds, exit_mode, trail_atr,
                 bars_left=None, opts=None):
    """kind: 'session' (exchange intraday), '24h' (crypto intraday) or 'daily'.
    bars_left: for 'session', candles until the planned exit time.
    Returns a dict with the odds, or {'error': ...}."""
    o = dict(TM_DEFAULTS, **(opts or {}))
    n = len(bars)
    risk = abs(entry - stop)
    if risk <= 0 or n < 30:
        return {"error": "not enough candles"}
    pools, spd, sessions = _pools(bars, kind, bar_seconds, n)
    if not pools or not any(pools.values()):
        return {"error": "not enough candles"}
    if kind == "session":
        if sessions < o["min_sessions"]:
            return {"error": f"only {sessions} full sessions loaded"}
        last_slot = S.session_index(bars)[-1]
        horizon = bars_left if bars_left is not None else spd - 1 - last_slot
        start = last_slot + 1
    elif kind == "24h":
        horizon = HORIZON_24H.get(_tf_name(bar_seconds), 96)
        start = (bars[-1]["t"] + bar_seconds) % 86400 // bar_seconds
    else:
        horizon = HORIZON_DAILY
        start = 0
    if horizon <= 0:
        return {"error": "session is ending"}
    rnd = random.Random(o["seed"])
    trail = exit_mode == "trail"
    target = None if trail else target
    trail_d = trail_atr * atr_now if trail and atr_now else None
    hit_t = hit_s = 0
    rs = []
    for _ in range(int(o["replays"])):
        p, st, best, res = entry, stop, entry, None
        for k in range(horizon):
            ro, rh, rl, rc = _pick(pools, start + k, o["window"], spd, kind, rnd)
            op, hi, lo, cl = p * ro, p * rh, p * rl, p * rc
            if side == 1:
                if lo <= st:
                    res = (min(st, op) - entry) / risk
                    hit_s += 1
                    break
                if target is not None and hi >= target:
                    res = (max(target, op) - entry) / risk
                    hit_t += 1
                    break
            else:
                if hi >= st:
                    res = (entry - max(st, op)) / risk
                    hit_s += 1
                    break
                if target is not None and lo <= target:
                    res = (entry - min(target, op)) / risk
                    hit_t += 1
                    break
            if trail_d:
                best = max(best, cl) if side == 1 else min(best, cl)
                ns = best - side * trail_d
                st = max(st, ns) if side == 1 else min(st, ns)
            p = cl
        if res is None:
            res = side * (p - entry) / risk
        rs.append(res)
    m = len(rs)
    srt = sorted(rs)
    out = {"replays": m, "horizon": horizon, "kind": kind, "sessions": sessions,
           "target_pct": round(100 * hit_t / m, 1), "stop_pct": round(100 * hit_s / m, 1),
           "open_pct": round(100 * (m - hit_t - hit_s) / m, 1),
           "green_pct": round(100 * sum(1 for r in rs if r > 0) / m, 1),
           "avg_r": round(sum(rs) / m, 2), "median_r": round(srt[m // 2], 2),
           "p10_r": round(srt[int(0.1 * m)], 2), "p90_r": round(srt[int(0.9 * m)], 2),
           "trail": trail}
    out["verdict"] = verdict(out, None if trail else abs(target - entry) / risk)
    return out


def _tf_name(sec):
    return {60: "1m", 300: "5m", 900: "15m", 1800: "30m", 3600: "1h", 86400: "1d"}.get(sec, "")


def verdict(o, target_r):
    """Same scale as the original Time Machine: target share of the replays that hit a side,
    against the share that breaks even at this R (50 % at 1R, 33 % at 2R)."""
    if target_r and o["target_pct"] + o["stop_pct"] > 0:
        share = 100 * o["target_pct"] / (o["target_pct"] + o["stop_pct"])
        gap = share - 100 / (1 + target_r)
        if abs(gap) < 5:
            v = "Close to a coin flip" if abs(target_r - 1) < 0.01 else "Close to break-even"
        elif abs(gap) < 15:
            v = "Leans toward the target" if gap > 0 else "Leans toward the stop"
        else:
            v = "Strongly toward the target" if gap > 0 else "Strongly toward the stop"
        if o["open_pct"] > 40:
            v += f"; {o['open_pct']:.0f}% still open at the exit time"
        if (o["avg_r"] > 0) != (gap > 0) and abs(o["avg_r"]) >= 0.05:
            v += f"; average {o['avg_r']:+.2f}R"
        return v
    a = o["avg_r"]
    if abs(a) < 0.1:
        return "Close to break-even"
    return ("Leans positive" if a < 0.4 else "Strongly positive") if a > 0 else (
        "Leans negative" if a > -0.4 else "Strongly negative")


def history(bars, rule, exit_mode, cfg, intraday, cost):
    """The same rule re-run on all loaded candles (entry next open, like backtest.py)."""
    import backtest as B  # lazy: only needed here
    n = len(bars)
    if n < 300:
        return None
    if rule == "scalp":
        sig, risk, _ = S.scalp_signals(bars)
        cfg = dict(cfg, target_r=S.SCALP_DEFAULTS["target_r"])
        tr = B.simulate(bars, lambda i: (sig[i], ""), exit_mode, cfg, cost, intraday, None, lambda i: risk[i])
    else:
        ind = S.indicators(bars, cfg)
        tr = B.simulate(bars, lambda i: S.signal_at(bars, ind, i, cfg, rule, intraday), exit_mode, cfg, cost, intraday)
    if not tr:
        return {"trades": 0, "days": round((bars[-1]["t"] - bars[0]["t"]) / 86400)}
    wins = sum(1 for t in tr if t["r"] > 0)
    return {"trades": len(tr), "wins": wins, "avg_r": round(sum(t["r"] for t in tr) / len(tr), 2),
            "total_r": round(sum(t["r"] for t in tr), 1), "days": round((bars[-1]["t"] - bars[0]["t"]) / 86400)}


def tm_text(tm, hist, target_r):
    """Plain-language odds message: one verdict line, one reason, one track-record line."""
    if not tm or tm.get("error"):
        return f"🔮 Odds check: skipped ({(tm or {}).get('error', 'off')})"
    if tm["trail"] or not target_r or tm["target_pct"] + tm["stop_pct"] == 0:
        gap, why = tm["avg_r"] * 25, None
    else:
        hit = tm["target_pct"] + tm["stop_pct"]
        gap = 100 * tm["target_pct"] / hit - 100 / (1 + target_r)
        t10 = round(10 * tm["target_pct"] / hit)
        why = f"In past price replays it reached the target before the stop {t10} in 10 times"
    if gap >= 5 and tm["avg_r"] > 0:
        head = "👍 GOOD odds"
    elif gap <= -5 or tm["avg_r"] < -0.1:
        head = "👎 POOR odds: better to skip"
    else:
        head = "🤷 COIN FLIP"
    lines = [f"🔮 {head}"]
    if why:
        lines.append(why + f" (needs about {round(10 / (1 + target_r))} in 10 to make money).")
    if tm["kind"] == "session" and tm["sessions"] is not None and tm["sessions"] < 20:
        lines.append("(Few past days to compare, so this is rough.)")
    if hist and hist.get("trades"):
        res = "made money" if hist["avg_r"] > 0 else "lost money"
        lines.append(f"📜 Last {hist['days']} days: this signal won {hist['wins']} of {hist['trades']} times and {res} overall.")
    return "\n".join(lines)


# ───────────────────────── position size and daily limits ─────────────────────────

class RiskBook:
    """Today's realised result and trade count, per currency (rupee markets and dollar
    markets have separate limits). Losses are estimated as R x the planned loss of each
    trade. Saved to state_path so a restart keeps today's count."""

    def __init__(self, cfg=None, state_path=None):
        self.cfg = merge_risk(cfg)
        self.path = state_path
        self.day = None
        self.pnl = {"INR": 0.0, "USD": 0.0}
        self.trades = {"INR": 0, "USD": 0}
        if state_path and os.path.exists(state_path):
            try:
                with open(state_path, encoding="utf-8") as f:
                    st = json.load(f)
                if st.get("day") == time.strftime("%Y-%m-%d"):
                    self.day = st["day"]
                    self.pnl.update(st.get("pnl") or {})
                    self.trades.update(st.get("trades") or {})
            except (OSError, ValueError, KeyError, TypeError):
                pass

    def _roll(self):
        d = time.strftime("%Y-%m-%d")
        if d != self.day:
            self.day, self.pnl, self.trades = d, {"INR": 0.0, "USD": 0.0}, {"INR": 0, "USD": 0}

    def _save(self):
        if not self.path:
            return
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"day": self.day, "pnl": self.pnl, "trades": self.trades}, f)
        except OSError:
            pass

    def size(self, code, side, entry, stop, option):
        """Returns a dict: qty/lots, estimated loss at the stop, blocked reason (or None), text."""
        c = self.cfg
        if not c.get("enabled", True):
            return None
        self._roll()
        cur = currency(code)
        lim = c[cur]
        pts = abs(entry - stop)
        out = {"currency": cur, "blocked": None}
        if self.pnl[cur] <= -lim["daily_loss_limit"]:
            out["blocked"] = (f"daily loss limit reached ({money(self.pnl[cur], cur)} today, "
                              f"limit {money(-lim['daily_loss_limit'], cur)})")
        elif self.trades[cur] >= c["max_trades_per_day"]:
            mk = "rupee" if cur == "INR" else "dollar"
            out["blocked"] = f"already {self.trades[cur]} trades today in {mk} markets (max {c['max_trades_per_day']})"
        max_loss = max(0.0, min(lim["max_loss_per_trade"], lim["daily_loss_limit"] + min(self.pnl[cur], 0)))
        if out["blocked"]:
            out.update(max_loss=0.0, loss=0.0, text=f"⛔ RISK BLOCK: {out['blocked']}. Skip this signal.")
            return out
        lot = (c.get("lot_size") or {}).get(code) or (c.get("lot_size") or {}).get(code.upper())
        if option:
            per_unit = pts * c["option_delta"]
            unit = "lot"
        else:
            per_unit = pts
            unit = "unit"
        if lot:
            per_lot = per_unit * lot
            n = int(max_loss // per_lot) if per_lot > 0 else 0
            out.update(lots=n, lot_size=lot, loss=n * per_lot, per_lot=per_lot)
            if n == 0:
                out["text"] = (f"💰 Size: 0 lots. One lot ({lot}) risks about {money(per_lot, cur)}, more than your "
                               f"{money(max_loss, cur)} max loss. Skip it, or trade smaller (index fund/ETF).")
            else:
                what = ("lot" if n == 1 else "lots") + (" (options)" if option else "")
                out["text"] = (f"💰 Size: {n} {what} of {lot} → about {money(n * per_lot, cur)} lost if stopped "
                               f"(max {money(max_loss, cur)})" + (f", delta {c['option_delta']:g} estimate" if option else ""))
        else:
            q = max_loss / per_unit if per_unit > 0 else 0
            notional = q * entry
            cap = lim["capital"]
            note = ""
            if notional > cap:
                q = cap / entry
                note = f", capped by your {money(cap, cur)} capital"
            qs = f"{q:.4f}".rstrip("0").rstrip(".") if q < 10 else f"{q:,.0f}"
            if option:
                out["text"] = (f"💰 Size: options covering about {qs} units of the underlying (delta "
                               f"{c['option_delta']:g} estimate) → about {money(q * per_unit, cur)} lost if stopped "
                               f"(max {money(max_loss, cur)}). Check the contract size.")
            else:
                out["text"] = (f"💰 Size: {qs} units (~{money(q * entry, cur)}) → about {money(q * per_unit, cur)} "
                               f"lost if stopped (max {money(max_loss, cur)}{note})")
            out.update(qty=q, loss=q * per_unit)
        out["max_loss"] = max_loss
        return out

    def opened(self, code):
        self._roll()
        self.trades[currency(code)] += 1
        self._save()

    def closed(self, code, r, planned_loss):
        self._roll()
        if planned_loss:
            self.pnl[currency(code)] += r * planned_loss
            self._save()

    def status(self):
        self._roll()
        return {"day": self.day, "pnl": {k: round(v, 2) for k, v in self.pnl.items()}, "trades": self.trades,
                "limits": {k: self.cfg[k] for k in ("INR", "USD", "max_trades_per_day")}}


def merge_risk(cfg):
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in RISK_DEFAULTS.items()}
    for k, v in (cfg or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


# ───────────────────────── self test ─────────────────────────

def _fake(kind, n_days=25, per_day=75, sec=300, seed=1):
    rnd = random.Random(seed)
    bars, p, t0 = [], 20000.0, 1_700_000_000
    for d in range(n_days):
        for k in range(per_day if kind == "session" else 86400 // sec):
            o = p
            c = o * (1 + rnd.gauss(0, 0.001))
            h, l = max(o, c) * (1 + abs(rnd.gauss(0, 0.0005))), min(o, c) * (1 - abs(rnd.gauss(0, 0.0005)))
            t = t0 + d * 86400 + k * sec
            bars.append({"t": t, "o": o, "h": h, "l": l, "c": c, "v": 0.0, "day": f"d{d:03d}"})
            p = c
    return bars


def _selftest():
    bars = _fake("session")
    cut = len(bars) - 40   # 35 candles into the last session
    b = bars[:cut]
    a = S.atr(b, 14)[-1]
    e = b[-1]["c"]
    t0 = time.time()
    tm = time_machine(b, 1, e, e - 1.5 * a, e + 3 * a, a, "session", 300, "target", 3.0, opts={"seed": 19, "replays": 5000})
    print("session:", tm, f"{time.time() - t0:.1f}s")
    assert tm["target_pct"] + tm["stop_pct"] + tm["open_pct"] > 99.8
    # a random walk at 2R should come out near break-even (~33 % target share), not strongly either way
    share = tm["target_pct"] / max(tm["target_pct"] + tm["stop_pct"], 1e-9)
    assert 0.2 < share < 0.47, share
    tm2 = time_machine(b, 1, e, e - 1.5 * a, e + 3 * a, a, "session", 300, "target", 3.0, opts={"seed": 19, "replays": 5000})
    assert tm2 == tm, "same seed must give the same numbers"
    cb = _fake("24h", n_days=10, sec=3600)
    a2 = S.atr(cb, 14)[-1]
    e2 = cb[-1]["c"]
    t0 = time.time()
    tm3 = time_machine(cb, -1, e2, e2 + 1.5 * a2, None, a2, "24h", 3600, "trail", 3.0, opts={"seed": 3})
    print("24h trail:", tm3, f"{time.time() - t0:.1f}s")
    # the pool keeps the sample's own drift (this made-up series fell 4 %), so a SELL leans positive
    drift = cb[-1]["c"] < cb[0]["c"]
    assert (tm3["avg_r"] > 0) == drift
    db = [{"t": 1_600_000_000 + k * 86400, "o": 100, "h": 101, "l": 99, "c": 100 * (1 + 0.0005 * k), "v": 0, "day": str(k)}
          for k in range(400)]
    for k in range(1, 400):
        db[k]["o"] = db[k - 1]["c"]
        db[k]["h"] = max(db[k]["o"], db[k]["c"]) * 1.01
        db[k]["l"] = min(db[k]["o"], db[k]["c"]) * 0.99
    a3 = S.atr(db, 14)[-1]
    e3 = db[-1]["c"]
    tm4 = time_machine(db, 1, e3, e3 - 1.5 * a3, None, a3, "daily", 86400, "trail", 3.0, opts={"seed": 5, "replays": 2000})
    print("daily trail:", tm4)
    print(tm_text(tm, {"trades": 12, "wins": 5, "avg_r": -0.1, "days": 25}, 2.0))
    print(tm_text(tm3, None, 2.0))

    rb = RiskBook({"INR": {"max_loss_per_trade": 1000, "daily_loss_limit": 2000}})
    s = rb.size("^NSEI", 1, 25000, 24970, option=True)   # 30 pts x 0.5 x 65 = 975 per lot
    print(s["text"])
    assert s["lots"] == 1 and not s["blocked"]
    s = rb.size("^NSEI", 1, 25000, 24900, option=True)   # 100 pts -> 3250 per lot: too big
    print(s["text"])
    assert s["lots"] == 0
    s = rb.size("BTCUSDT", -1, 60000, 60600, option=False)   # $10 / $600 = 0.0167 BTC
    print(s["text"])
    assert abs(s["qty"] - 10 / 600) < 1e-9
    rb.opened("^NSEI"); rb.closed("^NSEI", -1, 975); rb.opened("^NSEI"); rb.closed("^NSEI", -1.1, 975)
    s = rb.size("^NSEI", 1, 25000, 24970, option=True)
    print(s["text"])
    assert s["blocked"], s
    s = rb.size("BTCUSDT", 1, 60000, 59400, option=False)
    assert not s["blocked"], "USD book is separate"
    import tempfile
    sp = os.path.join(tempfile.mkdtemp(), "risk_today.json")
    rb = RiskBook(None, sp)
    rb.opened("BTCUSDT"); rb.closed("BTCUSDT", -2, 10)
    rb2 = RiskBook(None, sp)
    assert rb2.pnl["USD"] == -20 and rb2.trades["USD"] == 1, (rb2.pnl, rb2.trades)
    print("selftest OK")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print(__doc__)
