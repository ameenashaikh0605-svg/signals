#!/usr/bin/env python3
"""
Buy/Sell signal program v2 (IndMoney panel + Telegram alerts to your phone).

Runs on your own computer and only READS market data. It never places orders.
Every few seconds it downloads candles, and when a candle closes it checks the
v2 rules in strategy.py (the same rules backtest.py tests):

  Trend filter   20/50/200 EMAs agree, ADX shows a trend, daily trend agrees,
                 price on the right side of VWAP (intraday markets)
  Entry          pullback to the 20 EMA that resumes, or a breakout of the
                 last 20 candles with volume
  Risk           stop = 1.5 x ATR, target = 2 x risk (or trailing stop)

It then sends a BUY or SELL alert with entry, stop and target, and follows the
trade until the target, the stop or the end of the session.

Every alert also gets a position size (from your max loss per trade, with a
daily loss limit that blocks new trades) and, a few seconds later, a Time
Machine check: thousands of replays of the trade from the chart's own recent
candles plus the same rule on past candles (tradecheck.py).

Panel colours: GREEN = uptrend confirmed (look for buys only),
RED = downtrend confirmed (look for sells only), BLUE = no clear trend (stay out).

Usage:
  python signal_server.py            run with config.json
  python signal_server.py --demo     fake data, to test the panel and Telegram any time
  python signal_server.py --test-telegram   send one test message to your phone

Data: Binance (crypto, e.g. BTCUSDT) and Yahoo Finance (indices ^NSEI ^BSESN,
Indian stocks RELIANCE.NS, US stocks AAPL, commodities GC=F CL=F). Yahoo can
be delayed for some exchanges; every alert shows the candle time so you can see it.
Standard library only (Python 3.8+).
"""
import csv
import json
import math
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import strategy as S  # noqa: E402
import tradecheck as T
import news as N  # noqa: E402

DEFAULTS = {
    "port": 8765,
    "poll_seconds": 15,
    # Each watched symbol runs in every enabled mode. Rules: supertrend, combo, pullback,
    # breakout, orb (opening range, exchange markets only). Exits: trail or target.
    # Timeframes: 1d, 1h, 30m, 15m, 5m. "crypto_*" settings apply to 24/7 markets (Binance).
    "modes": {
        "swing": {"label": "📅 SWING", "timeframe": "1d", "rule": "supertrend", "exit": "trail"},
        "intraday": {"label": "⚡ INTRADAY (experimental)", "timeframe": "15m", "rule": "orb", "exit": "target",
                     "crypto_timeframe": "1h", "crypto_rule": "breakout", "crypto_exit": "trail"},
        "fast": {"label": "⚡ FAST 1m/5m/20m (experimental)", "timeframe": "1m", "rule": "scalp", "exit": "target"},
    },
    "enabled_modes": ["swing", "intraday", "fast"],
    # FAST lost money on Bitcoin and the Dow with every stop tried (30-day test, 30 Oct 2026); on Nifty a
    # stop 2x the 5-minute ATR did better than 1x. Empty fast_markets = every market.
    "fast_markets": ["^NSEI"],
    "fast_scalp": {"stop_htf_atr": 2.0},
    "intraday_trend_filter": False,   # intraday (15m) alerts only in the direction of the daily Supertrend
    "selected": "ALL",
    "strikes": {},              # option strike step per symbol, e.g. {"AAPL": 5, "RELIANCE.NS": 20}          # "ALL" or a list of symbol codes, e.g. ["^NSEI"]: only these are fetched and alerted

    "strategy": {},             # override any value in strategy.DEFAULTS
    "exit_before_close_min": 15,
    "keep_awake": True,         # Windows: don't let the laptop sleep while this program runs
    "keep_screen_on": True,     # Windows: also keep the screen on; on many laptops a dark screen also cuts Wi-Fi
    "desktop_alerts": True,     # Windows: sound + pop-up in the corner of the screen for every alert
    "telegram": {"bot_token": "", "chat_id": "", "bias_alerts": False},
    "timemachine": dict(T.TM_DEFAULTS),   # odds check sent after each new signal
    "risk": {},                 # position size and daily limits, see tradecheck.RISK_DEFAULTS
    "news": {"enabled": True, "telegram_big": True, "interval_min": 5, "max_alerts_per_hour": 4},
    "watch": [],
}

COST = {"INR": 0.0003, "USD": 0.0002, "crypto": 0.0005}   # per side, same as backtest.py

TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "1d": 86400}
YAHOO_TF = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "60m", "1d": "1d"}
BINANCE_TF = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h", "1d": "1d"}
MAX_BARS = 5000


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    path = os.path.join(HERE, "config.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    for m in cfg["enabled_modes"]:
        if m not in cfg["modes"]:
            sys.exit(f"enabled_modes lists '{m}' but modes has no such entry")
        for k in ("timeframe", "crypto_timeframe"):
            tf = cfg["modes"][m].get(k)
            if tf and tf not in TF_SECONDS:
                sys.exit(f"{m} {k} must be one of {', '.join(TF_SECONDS)}")
    return cfg


def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} from {urllib.parse.urlparse(url).netloc}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot reach {urllib.parse.urlparse(url).netloc}: {e.reason}")


def fmt_px(x):
    if x is None:
        return "-"
    if abs(x) >= 1000:
        return f"{x:,.1f}"
    if abs(x) >= 10:
        return f"{x:,.2f}"
    return f"{x:,.4f}"


# ───────────────────────── data sources ─────────────────────────
# Each source returns (closed_bars, last_price, info). Bars: t,o,h,l,c,v,day.

# Option strike spacing. Indian indices use exchange strike gaps; other symbols use the
# "strikes" map in config.json, or a round step of about 0.5% of the price.
STRIKE_STEP = {"^NSEI": ("NIFTY", 50), "^BSESN": ("SENSEX", 100), "^NSEBANK": ("BANKNIFTY", 100),
               "^CNXFIN": ("FINNIFTY", 50), "^NSEMDCP50": ("MIDCPNIFTY", 25),
               "NIFTY_MID_SELECT.NS": ("MIDCPNIFTY", 25),
               "BTCUSDT": ("BTC", 500), "ETHUSDT": ("ETH", 50), "GC=F": ("GOLD (COMEX)", 10),
               "^DJI": ("DOW (DJI)", 100), "^GSPC": ("S&P 500", 5), "^IXIC": ("NASDAQ", 50),
               "SI=F": ("SILVER (COMEX)", 0.5), "CL=F": ("CRUDE (COMEX)", 1), "NG=F": ("NAT GAS (COMEX)", 0.1)}
PRICE_NOTE = {"^DJI": "Dow index points; US options on the Dow trade as DIA (about 1/100 of the index) or YM futures",
              "GC=F": "COMEX $ price; for MCX gold use MCX's own at-the-money strike",
              "SI=F": "COMEX $ price; for MCX silver use MCX's own at-the-money strike",
              "CL=F": "COMEX $ price; for MCX crude use MCX's own at-the-money strike",
              "NG=F": "COMEX $ price; for MCX natural gas use MCX's own at-the-money strike"}
NICE_STEPS = [0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 25, 50, 100, 250, 500, 1000]
CUSTOM_STEPS = {}  # filled from config.json "strikes": {"AAPL": 5}


def option_hint(code, name, side, price, mode):
    """'NIFTY 25,100 CE (at the money), nearest expiry' for any symbol."""
    if not price:
        return None
    up = code.upper()
    label, step = STRIKE_STEP.get(up, (name.upper(), None))
    step = CUSTOM_STEPS.get(code) or CUSTOM_STEPS.get(up) or step
    if not step:
        step = min(NICE_STEPS, key=lambda x: abs(x - price * 0.005))
    strike = round(price / step) * step
    strike_txt = f"{strike:,.0f}" if step >= 1 else f"{strike:,.2f}"
    kind = "CE" if side == 1 else "PE"
    when = "monthly expiry or later (swing trade)" if mode == "swing" else "nearest expiry"
    note = PRICE_NOTE.get(up)
    return f"{label} {strike_txt} {kind} (at the money), {when}" + (f". {note}" if note else "")


def plain_reasons(it, bars, ind, i, side, why, scalp):
    """The reasons behind a signal, in plain words."""
    up = side == 1
    d = "up" if up else "down"
    out = []
    if scalp:
        for m in S.SCALP_DEFAULTS["htf_minutes"]:
            out.append(f"{m}-minute trend is {d}")
        out.append(f"Price is {'above' if up else 'below'} today's average price (VWAP)")
        lb = S.SCALP_DEFAULTS["trigger_lookback"]
        push = why.split(", ", 1)[1].split(";")[0] if ", " in why else ""
        if push.startswith("volume"):
            push = f"on {push.split()[1]} the normal volume"
        elif push.startswith("wide candle"):
            push = f"with a candle {push.split()[2]} bigger than usual"
        out.append(f"{'Buyers' if up else 'Sellers'} just pushed price {'above the high' if up else 'below the low'} "
                   f"of the last {lb} minutes" + (f" ({push})" if push else ""))
    else:
        plain = {"Opening range breakout up": "Price broke above the first 15 minutes' high",
                 "Opening range breakdown": "Price broke below the first 15 minutes' low",
                 "Uptrend pullback resumed": "Price dipped, then turned back up with the trend",
                 "Downtrend pullback resumed": "Price bounced, then turned back down with the trend",
                 "Supertrend flipped up, above 200 EMA": "Supertrend turned up and price is above its 200-day average",
                 "Supertrend flipped down, below 200 EMA": "Supertrend turned down and price is below its 200-day average"}
        out.append(plain.get(why, why.replace("bar breakout", "candle high broken").replace("bar breakdown", "candle low broken")))
        if S.trend_dir(ind, i) == side:
            out.append(f"Moving averages point {d}")
        if ind["adx"][i] >= S.DEFAULTS["adx_min"]:
            out.append(f"The trend is strong, not choppy (ADX {ind['adx'][i]:.0f})")
        if it["info"].get("intraday") and ind["vwap"][i]:
            if (bars[i]["c"] > ind["vwap"][i]) == up:
                out.append(f"Price is {'above' if up else 'below'} today's average price (VWAP)")
    dt = ind["dtrend"][i] if it["tf"] != "1d" else None
    if dt is not None:
        out.append(f"Daily trend is {d} too" if dt == side else
                   f"⚠️ Daily trend is {'up' if dt > 0 else 'down'}, against this trade: be careful")
    return out


def extra_checks(it, bars, ind, i, side, entry, risk):
    """Support/resistance and news checks (same as the swing setups), or '' if they can't run."""
    try:
        import swing_scan as W
        W.CUR = ""
        ind = dict(ind, v=[b["v"] or 0.0 for b in bars])
        checks = W.checklist(bars, ind, i, side, entry, risk)[2:]
        checks = [c for c in checks if "averages" not in c[1] and "ADX" not in c[1]]   # already in the reasons
        mark, heads = W.news_check(f'"{it["name"].title()}"', side)
        checks.append(mark)
        return ("Checks:\n" + "\n".join(f"{mk} {t}" for mk, t in checks)
                + "".join(f"\n📰 {h}" for h in heads) + "\n")
    except Exception as e:
        print("extra checks skipped:", e)
        return ""


def is_indian_index(code):
    up = code.upper()
    return up in ("^NSEI", "^BSESN", "^NSEBANK", "^CNXFIN", "^NSEMDCP50", "NIFTY_MID_SELECT.NS") or up.startswith(("NFO_", "BFO_"))


def guess_source(code):
    up = code.upper()
    if up.endswith(("USDT", "USDC", "FDUSD")) and "=" not in up and "^" not in up:
        return "binance"
    return "yahoo"


class YahooSource:
    name = "yahoo"

    def __init__(self):
        self.loaded = set()

    def candles(self, code, tf):
        if (code, tf) in self.loaded:
            rng = "5d" if tf == "1d" else "1d"
        else:
            rng = {"1d": "3y", "1h": "730d", "1m": "5d"}.get(tf, "60d")
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(code)}"
               f"?interval={YAHOO_TF[tf]}&range={rng}&includePrePost=false")
        res = http_json(url)["chart"]["result"][0]
        meta = res.get("meta", {})
        off = meta.get("gmtoffset", 0)
        q = (res.get("indicators", {}).get("quote") or [{}])[0]
        ts = res.get("timestamp") or []
        sec = TF_SECONDS[tf]
        # Yahoo adds an odd-timed partial candle at the close; keep only aligned candles
        mods = {}
        for t in ts:
            mods[t % sec] = mods.get(t % sec, 0) + 1
        align = max(mods, key=mods.get) if mods else 0
        now = time.time()
        bars = []
        for i, t in enumerate(ts):
            o, h, l, c = (q.get(k, [None] * len(ts))[i] for k in ("open", "high", "low", "close"))
            if None in (o, h, l, c) or t % sec != align or t + sec > now:
                continue
            day = datetime.fromtimestamp(t + off, tz=timezone.utc).strftime("%Y-%m-%d")
            bars.append({"t": t, "o": o, "h": h, "l": l, "c": c, "v": (q.get("volume") or [0] * len(ts))[i] or 0.0,
                         "day": day})
        self.loaded.add((code, tf))
        period = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
        info = {"session_end": period.get("end"), "intraday": tf != "1d", "tz_offset": off,
                "delay_min": meta.get("exchangeDataDelayedBy") or 0, "name": meta.get("shortName")}
        return bars, meta.get("regularMarketPrice"), info


class BinanceSource:
    name = "binance"
    HOSTS = ["https://data-api.binance.vision/api/v3", "https://api.binance.com/api/v3"]

    def __init__(self):
        self.loaded = set()
        self.host = self.HOSTS[0]

    def _get(self, path):
        last = None
        for h in [self.host] + [x for x in self.HOSTS if x != self.host]:
            try:
                r = http_json(h + path)
                self.host = h
                return r
            except RuntimeError as e:
                last = e
        raise last

    def push(self, code):
        """Buyer vs seller share of traded volume in the last 10 seconds (aggressive orders)."""
        sym = code.upper().replace("/", "").replace("-", "")
        try:
            trades = self._get(f"/aggTrades?symbol={sym}&limit=1000")
        except Exception:
            return None
        cutoff = trades[-1]["T"] - 10_000 if trades else 0
        buy = sum(float(t["q"]) for t in trades if t["T"] >= cutoff and not t["m"])
        sell = sum(float(t["q"]) for t in trades if t["T"] >= cutoff and t["m"])
        return round(100 * buy / (buy + sell)) if buy + sell > 0 else None

    def candles(self, code, tf):
        sym = code.upper().replace("/", "").replace("-", "")
        now_ms = int(time.time() * 1000)
        rows = []
        if (code, tf) not in self.loaded:   # first time: 30 days (daily trend filter) or 400 candles
            days = 30 if TF_SECONDS[tf] >= 300 else 3
            start = now_ms - max(days * 86400, 400 * TF_SECONDS[tf]) * 1000
            while start < now_ms:
                chunk = self._get(f"/klines?symbol={sym}&interval={BINANCE_TF[tf]}&startTime={start}&limit=1000")
                if not chunk:
                    break
                rows += chunk
                start = chunk[-1][0] + 1
                if len(chunk) < 1000:
                    break
            self.loaded.add((code, tf))
        else:
            rows = self._get(f"/klines?symbol={sym}&interval={BINANCE_TF[tf]}&limit=5")
        bars = []
        for r in rows:
            if r[6] >= now_ms:      # candle still open
                continue
            t = r[0] // 1000
            bars.append({"t": t, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
                         "v": float(r[5]), "day": datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")})
        ltp = float(rows[-1][4]) if rows else None
        info = {"intraday": False, "session_end": None, "tz_offset": 0, "delay_min": 0}
        try:  # open-interest change over the last hour (context only)
            oi = http_json(f"https://fapi.binance.com/futures/data/openInterestHist?symbol={sym}&period=5m&limit=13")
            if len(oi) >= 2:
                a, b = float(oi[0]["sumOpenInterest"]), float(oi[-1]["sumOpenInterest"])
                info["oi_change_1h_pct"] = round(100 * (b - a) / a, 2) if a else None
        except Exception:
            pass
        return bars, ltp, info


class DemoSource:
    """Fake trending/ranging market, one candle per call, to test the panel any time."""
    name = "demo"

    def __init__(self):
        self.s = {}

    def candles(self, code, tf):
        sec = TF_SECONDS[tf]
        st = self.s.get((code, tf))
        if st is None:
            st = self.s[(code, tf)] = {"px": random.uniform(100, 25000), "t": int(time.time() // sec) * sec - 400 * sec,
                                 "regime": 1, "left": 60, "first": True}
        n = 400 if st["first"] else 1
        st["first"] = False
        bars = []
        for _ in range(n):
            if st["left"] <= 0:
                st["regime"], st["left"] = random.choice([-1, 0, 1]), random.randint(40, 120)
            st["left"] -= 1
            o = st["px"]
            c = o * (1 + st["regime"] * 0.0012 + random.gauss(0, 0.002))
            h, l = max(o, c) * (1 + abs(random.gauss(0, 0.001))), min(o, c) * (1 - abs(random.gauss(0, 0.001)))
            st["t"] += sec
            bars.append({"t": st["t"], "o": o, "h": h, "l": l, "c": c, "v": random.uniform(1e3, 3e3),
                         "day": datetime.fromtimestamp(st["t"] // 20000 * 20000, tz=timezone.utc).strftime("%Y-%m-%d %H")})
            st["px"] = c
        return bars, st["px"], {"intraday": False, "session_end": None, "tz_offset": 0, "delay_min": 0}


# ───────────────────────── engine ─────────────────────────

class Engine:
    def __init__(self, cfg, demo=False):
        self.cfg = cfg
        self.demo = demo
        self.scfg = dict(S.DEFAULTS, **cfg.get("strategy", {}))
        self.sources = {"demo": DemoSource()} if demo else {"yahoo": YahooSource(), "binance": BinanceSource()}
        self.lock = threading.Lock()
        self.items = []
        self.errors = {}
        self.updated = None
        # today's trades and losses survive a restart (risk_today.json)
        self.book = T.RiskBook(cfg.get("risk"), None if demo else os.path.join(HERE, "risk_today.json"))
        for w in cfg["watch"]:
            self._add(w)

    def scalp_target(self):
        return dict(S.SCALP_DEFAULTS, **(self.cfg.get("fast_scalp") or {}))["target_r"]

    def _add(self, w):
        """Adds one row per enabled mode for this symbol. Returns the first row, or None."""
        code = str(w.get("code", "")).strip()
        if not code or any(it["code"] == code for it in self.items):
            return None
        src = "demo" if self.demo else (w.get("source") or guess_source(code))
        if src not in self.sources:
            raise ValueError(f"unknown source '{src}' (use yahoo or binance)")
        crypto = src == "binance"
        first = None
        for m in self.cfg["enabled_modes"]:
            if m == "fast" and self.cfg.get("fast_markets") and code not in self.cfg["fast_markets"]:
                continue
            mc = self.cfg["modes"][m]
            pick = lambda k: (mc.get("crypto_" + k) if crypto else None) or mc[k]
            it = {"name": (w.get("name") or code).strip(), "code": code, "source": src, "mode": m,
                  "label": mc.get("label", m), "tf": pick("timeframe"), "rule": pick("rule"), "exit": pick("exit"),
                  "bars": [], "ltp": None, "info": {}, "trade": None, "last_signal": None, "bias": None,
                  "checks": [], "error": None, "last_bar_t": None, "bias_sent": None}
            self.items.append(it)
            first = first or it
        return first

    def add_watch(self, w):
        with self.lock:
            it = self._add(w)
        if it is None:
            raise ValueError("symbol missing or already watched")
        self.save()
        return {"name": it["name"], "code": it["code"], "source": it["source"]}

    def remove_watch(self, code):
        with self.lock:
            before = len(self.items)
            self.items = [it for it in self.items if it["code"] != code]
            removed = len(self.items) < before
        if removed:
            self.save()
        return removed

    def save(self):
        if self.demo:
            return
        path = os.path.join(HERE, "config.json")
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        with self.lock:
            seen, watch = set(), []
            for it in self.items:
                if it["code"] not in seen:
                    seen.add(it["code"])
                    watch.append({"name": it["name"], "code": it["code"], "source": it["source"]})
            data["watch"] = watch
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    # ── per symbol update ──
    def update(self, it):
        bars, ltp, info = self.sources[it["source"]].candles(it["code"], it["tf"])
        known = {b["t"] for b in it["bars"][-50:]}
        new = [b for b in bars if b["t"] not in known and (not it["bars"] or b["t"] > it["bars"][-1]["t"])]
        it["bars"] = (it["bars"] + new)[-MAX_BARS:]
        it["ltp"] = ltp if ltp is not None else (it["bars"][-1]["c"] if it["bars"] else None)
        it["info"] = info
        it["error"] = None
        if new:
            self.on_close(it, first_load=it["last_bar_t"] is None)
            it["last_bar_t"] = it["bars"][-1]["t"]
        self.follow_trade(it)

    def on_close(self, it, first_load):
        bars, cfg = it["bars"], dict(self.scfg, bar_seconds=TF_SECONDS[it["tf"]])
        intraday = it["info"].get("intraday", False)
        i = len(bars) - 1
        if i < cfg["ema_trend"]:
            it["bias"], it["checks"] = "warming", [f"{i + 1}/{cfg['ema_trend'] + 1} candles loaded"]
            return
        ind = S.indicators(bars, cfg)
        a = ind["atr"][i]
        scalp = None
        if it["rule"] == "scalp":
            scalp = S.scalp_signals(bars, self.cfg.get("fast_scalp"))
            views = [S.htf_view(bars, m, S.SCALP_DEFAULTS["htf_ema"])[i][0] for m in S.SCALP_DEFAULTS["htf_minutes"]]
            names = [f"{m}m" for m in S.SCALP_DEFAULTS["htf_minutes"]]
            checks = [f"{'↑' if v > 0 else '↓' if v < 0 else '–'} {n} trend" for v, n in zip(views, names)]
            checks.append(f"{'above' if bars[i]['c'] > ind['vwap'][i] else 'below'} VWAP")
            bias = "up" if all(v == 1 for v in views) else "down" if all(v == -1 for v in views) else "none"
        else:
            bias, checks = S.bias(bars, ind, i, cfg, intraday)
        it["bias"], it["checks"], it["atr"] = bias, checks, a
        # trailing stop moves only on candle close
        tr = it["trade"]
        if tr and it["exit"] == "trail":
            side = tr["side"]
            tr["best"] = max(tr["best"], bars[i]["c"]) if side == 1 else min(tr["best"], bars[i]["c"])
            trail = tr["best"] - side * cfg["trail_atr"] * a
            tr["stop"] = max(tr["stop"], trail) if side == 1 else min(tr["stop"], trail)
        if first_load:
            return  # never alert on old candles at start-up
        if it["bias_sent"] != bias and self.cfg["telegram"].get("bias_alerts") and bias in ("up", "down"):
            self.alert(f"{'🟩' if bias == 'up' else '🟥'} {it['name']}: {'uptrend' if bias == 'up' else 'downtrend'} "
                       f"confirmed, wait for a {'BUY' if bias == 'up' else 'SELL'} setup", mode=it["mode"])
        it["bias_sent"] = bias
        if tr:
            return
        if intraday and self.near_close(it):
            return
        if scalp:
            side, why = scalp[0][i], scalp[2][i]
            cfg = dict(cfg, stop_atr=1.0, target_r=self.scalp_target())
            a = scalp[1][i] or a
        else:
            side, why = S.signal_at(bars, ind, i, cfg, it["rule"], intraday)
        if side and it["mode"] == "intraday" and intraday and self.cfg.get("intraday_trend_filter"):
            if self.daily_supertrend(it["code"], bars[i].get("day")) == -side:
                return  # against the daily trend: skipped (tested 2026-10-25, see filter_results.txt)
        if not side:
            return
        entry = it["ltp"] or bars[i]["c"]
        stop, target, risk = S.levels(entry, side, a, cfg)
        tr = {"side": side, "entry": entry, "stop": stop, "target": target if it["exit"] == "target" else None,
              "risk": risk, "opened": time.time(), "bar_t": bars[i]["t"], "why": why, "best": entry, "status": "open"}
        sz = self.book.size(it["code"], side, entry, stop, option=it["mode"] != "swing")
        if sz:
            tr["size"], tr["planned_loss"], tr["blocked"] = sz["text"], sz.get("loss") or 0.0, sz["blocked"]
            if sz["blocked"]:
                tr["why"] = "[risk block] " + why
            else:
                self.book.opened(it["code"])
        it["trade"] = tr
        it["last_signal"] = dict(tr)
        self.log(it, "ENTRY", tr, entry)
        word, icon = ("BUY", "🟢") if side == 1 else ("SELL", "🔴")
        if it["mode"] != "swing":
            word = "BUY CALL (CE)" if side == 1 else "BUY PUT (PE)"
        bar_time = self.local_time(bars[i]["t"] + TF_SECONDS[it["tf"]], it)
        tgt = (f"Target {fmt_px(target)} ({cfg['target_r']:g}R)" if tr["target"] is not None
               else f"Trailing stop {cfg['trail_atr']:g} ATR, no fixed target")
        delay = it["info"].get("delay_min") or 0
        opt = option_hint(it["code"], it["name"], side, entry, it["mode"])
        tr["option"] = opt
        it["last_signal"]["option"] = opt
        idx = ""
        reasons = plain_reasons(it, bars, ind, i, side, why, scalp)
        extra = "" if scalp else extra_checks(it, bars, ind, i, side, entry, risk)
        tgt_line = (f"🎯 Target {fmt_px(target)}" if tr["target"] is not None
                    else "🎯 No fixed target: the stop follows price, exit when it is hit")
        self.alert(f"{icon} {word} {it['name']}  ·  {it['label']}\n"
                   + (f"Option: {opt}\n" if opt else "")
                   + f"▶ Entry ~ {fmt_px(entry)}\n🛑 Stop loss {fmt_px(stop)}\n{tgt_line}\n"
                   + ("(These are " + it['name'] + " levels, not option prices.)\n" if opt else "")
                   + (tr["size"] + "\n" if tr.get("size") else "")
                   + "\nWhy the trend looks good:\n" + "\n".join("• " + r for r in reasons) + "\n"
                   + extra + "\n"
                   + f"Candle closed {bar_time}{f' · data delayed {delay} min' if delay else ''}. "
                   + "I'll message you when the target or stop loss is hit. Not advice.", mode=it["mode"])
        if self.cfg["timemachine"].get("enabled", True):
            threading.Thread(target=self.time_machine, args=(it, tr, list(bars), a, dict(cfg)), daemon=True).start()

    def time_machine(self, it, tr, bars, a, cfg):
        """Odds check for a new trade, sent as a second message (it takes a few seconds)."""
        try:
            sec = TF_SECONDS[it["tf"]]
            intraday = it["info"].get("intraday", False)
            kind = "daily" if it["tf"] == "1d" else "session" if intraday else "24h"
            left = None
            end = it["info"].get("session_end")
            if kind == "session" and end:
                left = int((end - self.cfg["exit_before_close_min"] * 60 - (bars[-1]["t"] + sec)) // sec)
            tm = T.time_machine(bars, tr["side"], tr["entry"], tr["stop"], tr["target"], a, kind, sec,
                                it["exit"], cfg["trail_atr"], bars_left=left, opts=self.cfg["timemachine"])
            cur = "crypto" if it["source"] == "binance" else T.currency(it["code"])
            try:
                hist = T.history(bars, it["rule"], it["exit"], cfg, intraday, COST[cur])
            except Exception as e:
                print(f"{it['name']} history check: {e}")
                hist = None
            target_r = self.scalp_target() if it["rule"] == "scalp" else cfg["target_r"]
            text = T.tm_text(tm, hist, target_r)
        except Exception as e:
            tm, text = {"error": str(e)}, f"🔮 Odds check: skipped ({e})"
        if not tm.get("error"):
            tr["odds"] = (f"{tm['verdict']} · " + (f"avg {tm['avg_r']:+.2f}R" if tm["trail"] else
                          f"target {tm['target_pct']:.0f}% / stop {tm['stop_pct']:.0f}%"))
            if it["last_signal"] is not None and it["last_signal"].get("bar_t") == tr["bar_t"]:
                it["last_signal"]["odds"] = tr["odds"]
        if not self.cfg["timemachine"].get("send_message", False):
            return
        word = "BUY" if tr["side"] == 1 else "SELL"
        self.alert(f"{it['label']}\nOdds for the {word} {it['name']} ({it['tf']}) alert above:\n{text}", mode=it["mode"])

    def daily_supertrend(self, code, day):
        """Supertrend direction of the last finished daily candle before `day`, from the swing row's
        daily candles (+1 up, -1 down, 0 unknown)."""
        sw = next((x for x in self.items if x["code"] == code and x["tf"] == "1d" and x["bars"]), None)
        if not sw or not day:
            return 0
        done = [b for b in sw["bars"] if b.get("day") and b["day"] < day]
        if len(done) < 20:
            return 0
        return S.supertrend(done)[-1]

    def follow_trade(self, it):
        tr, px = it["trade"], it["ltp"]
        if not tr or px is None:
            return
        side = tr["side"]
        last = it["bars"][-1] if it["bars"] else None
        hi = max(px, last["h"]) if last and last["t"] >= tr["bar_t"] + TF_SECONDS[it["tf"]] else px
        lo = min(px, last["l"]) if last and last["t"] >= tr["bar_t"] + TF_SECONDS[it["tf"]] else px
        result = None
        if (side == 1 and lo <= tr["stop"]) or (side == -1 and hi >= tr["stop"]):
            moved = (tr["stop"] - tr["entry"]) * side > 0
            result = ("🔒 Trailing stop hit" if moved else "❌ Stop hit", tr["stop"])
        elif tr["target"] is not None and ((side == 1 and hi >= tr["target"]) or (side == -1 and lo <= tr["target"])):
            result = ("✅ Target hit", tr["target"])
        elif it["info"].get("intraday") and self.near_close(it):
            result = ("⏰ Session ending, close the trade", px)
        if result:
            label, exit_px = result
            r = side * (exit_px - tr["entry"]) / tr["risk"] if tr["risk"] else 0
            tr["status"] = label
            it["last_signal"] = dict(tr, exit=exit_px, r=round(r, 2), closed=time.time())
            it["trade"] = None
            if not tr.get("blocked"):
                self.book.closed(it["code"], r, tr.get("planned_loss"))
            self.log(it, "EXIT", tr, exit_px, label, r)
            word = "BUY" if side == 1 else "SELL"
            if it["mode"] != "swing":
                word = "CALL" if side == 1 else "PUT"
            head = {"❌ Stop hit": "❌ STOP LOSS HIT", "✅ Target hit": "✅ TARGET HIT",
                    "🔒 Trailing stop hit": "🔒 TRAILING STOP HIT"}.get(label, label.upper())
            pl = tr.get("planned_loss") or 0
            cur = T.currency(it["code"]) if it["source"] != "binance" else "USD"
            money_txt = f" · about {'+' if r >= 0 else ''}{T.money(r * pl, cur)} at the suggested size" if pl and not tr.get("blocked") else ""
            self.alert(f"{head}: {it['name']} {word}  ·  {it['label']}\n"
                       f"Entry {fmt_px(tr['entry'])} → exit {fmt_px(exit_px)}\n"
                       f"Result {'+' if r >= 0 else ''}{r:.1f}x the risk{money_txt}", mode=it["mode"])

    def near_close(self, it):
        end = it["info"].get("session_end")
        return bool(end) and 0 <= end - time.time() <= self.cfg["exit_before_close_min"] * 60

    def local_time(self, t, it):
        return time.strftime("%d %b %H:%M", time.localtime(t))

    # ── outputs ──
    def alert(self, text, mode="news"):
        print("\n" + text + "\n")
        if self.cfg.get("desktop_alerts") and not self.demo:
            threading.Thread(target=desktop_alert, args=(text,), daemon=True).start()
        tg = self.cfg["telegram"]
        only = tg.get("only_modes")  # e.g. ["fast"] when GitHub sends the other alerts
        if only and mode not in only:
            return
        if tg.get("bot_token") and tg.get("chat_id"):
            threading.Thread(target=send_telegram, args=(tg["bot_token"], tg["chat_id"], text), daemon=True).start()

    def log(self, it, kind, tr, px, label="", r=None):
        if self.demo:
            return
        path = os.path.join(HERE, "signals_log.csv")
        new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "symbol", "timeframe", "event", "side", "price", "stop", "target", "result", "R", "reason"])
            w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), it["name"], it["tf"], kind, "BUY" if tr["side"] == 1 else "SELL",
                        round(px, 4), round(tr["stop"], 4), round(tr["target"], 4) if tr["target"] else "",
                        label, "" if r is None else round(r, 2), tr["why"]])

    def is_selected(self, it):
        sel = self.cfg.get("selected", "ALL")
        return sel == "ALL" or it["code"] in sel

    def select(self, codes):
        """codes: "ALL" or a list of symbol codes. Only selected symbols are fetched and alerted."""
        if codes != "ALL":
            known = {it["code"] for it in self.items}
            codes = [c for c in codes if c in known] or "ALL"
        self.cfg["selected"] = codes
        if not self.demo:
            path = os.path.join(HERE, "config.json")
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = {}
            data["selected"] = codes
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        return codes

    def tick(self):
        with self.lock:
            items = [it for it in self.items if self.is_selected(it)]
        for it in items:
            try:
                self.update(it)
                if it["mode"] == "fast" and hasattr(self.sources[it["source"]], "push"):
                    it["info"]["push10s"] = self.sources[it["source"]].push(it["code"])
                self.errors.pop(it["source"], None)
            except Exception as e:  # one market failing must not stop the others
                it["error"] = str(e)
                self.errors[it["source"]] = str(e)
                print(f"{it['name']}: {e}")
        with self.lock:
            self.updated = time.time()

    def snapshot(self):
        out = []
        with self.lock:
            items = list(self.items)
        for it in items:
            if not self.is_selected(it):
                continue
            tr = it["trade"]
            out.append({
                "name": it["name"], "code": it["code"], "source": it["source"], "tf": it["tf"],
                "mode": it["mode"], "label": it["label"], "rule": it["rule"], "exit": it["exit"],
                "ltp": it["ltp"], "bias": it["bias"] or ("no_data" if it["error"] else "loading"),
                "checks": it["checks"], "error": it["error"],
                "trade": None if not tr else {k: tr.get(k) for k in ("side", "entry", "stop", "target", "why", "opened", "option",
                                                        "size", "odds", "blocked")},
                "last": None if not it["last_signal"] else {k: it["last_signal"].get(k) for k in
                                                            ("side", "entry", "stop", "target", "status", "r", "opened", "exit", "option")},
                "oi_change_1h_pct": it["info"].get("oi_change_1h_pct"),
                "push10s": it["info"].get("push10s"),
                "cepe": it["mode"] != "swing",
                "delay_min": it["info"].get("delay_min"),
                "last_bar": it["bars"][-1]["t"] + TF_SECONDS[it["tf"]] if it["bars"] else None,
            })
        markets, seen = [], set()
        for it in items:
            if it["code"] not in seen:
                seen.add(it["code"])
                markets.append({"code": it["code"], "name": it["name"]})
        return {"updated": self.updated, "errors": dict(self.errors), "demo": self.demo, "items": out,
                "markets": markets, "info": self.market_info([it for it in items if self.is_selected(it)]), "selected": self.cfg.get("selected", "ALL"),
                "risk": self.book.status()}

    def market_info(self, items):
        """Per symbol: last price, change vs the previous daily close, today's high/low."""
        out = {}
        for it in items:
            m = out.setdefault(it["code"], {"code": it["code"], "name": it["name"], "ltp": None, "prev_close": None,
                                            "day_high": None, "day_low": None, "_tf": None})
            if it["ltp"] is not None:
                m["ltp"] = it["ltp"]
            bars = it["bars"]
            if not bars:
                continue
            if it["tf"] == "1d" and len(bars) >= 1:
                m["prev_close"] = bars[-1]["c"]
            elif m["_tf"] is None or TF_SECONDS[it["tf"]] < m["_tf"]:
                m["_tf"] = TF_SECONDS[it["tf"]]
                day = bars[-1]["day"]
                today = [b for b in bars[-1500:] if b["day"] == day]
                m["day_high"] = max(b["h"] for b in today)
                m["day_low"] = min(b["l"] for b in today)
        res = []
        for m in out.values():
            m.pop("_tf")
            if m["ltp"] is not None:
                if m["day_high"] is not None:
                    m["day_high"], m["day_low"] = max(m["day_high"], m["ltp"]), min(m["day_low"], m["ltp"])
                if m["prev_close"]:
                    m["change_pct"] = round(100 * (m["ltp"] - m["prev_close"]) / m["prev_close"], 2)
            m["strike_step"] = (STRIKE_STEP.get(m["code"].upper(), (None, None))[1]
                                or CUSTOM_STEPS.get(m["code"]))
            res.append(m)
        return res

    def stats(self):
        """Results so far from signals_log.csv: per symbol and timeframe, plus the latest events."""
        path = os.path.join(HERE, "signals_log.csv")
        rows = []
        if os.path.exists(path):
            with open(path, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        board = {}
        for r in rows:
            if r.get("event") != "EXIT" or r.get("R") in (None, ""):
                continue
            k = (r["symbol"], r["timeframe"])
            b = board.setdefault(k, {"symbol": k[0], "timeframe": k[1], "trades": 0, "wins": 0, "total_r": 0.0})
            b["trades"] += 1
            b["wins"] += float(r["R"]) > 0
            b["total_r"] = round(b["total_r"] + float(r["R"]), 2)
        return {"board": sorted(board.values(), key=lambda b: -b["trades"]), "recent": rows[-40:][::-1]}

    def run(self):
        while True:
            try:
                self.tick()
            except Exception as e:
                print("error:", e)
            time.sleep(1 if self.demo else self.cfg["poll_seconds"])


def desktop_alert(text):
    """Sound plus a Windows notification-area pop-up. Does nothing on other systems."""
    if os.name != "nt":
        return
    try:
        import subprocess
        import winsound
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        lines = [l for l in text.splitlines() if l.strip()]
        title = (lines[1] if len(lines) > 1 else lines[0])[:63]
        body = "\n".join(lines[2:6])[:250] or title
        q = lambda x: "'" + x.replace("'", "''") + "'"
        ps = ("Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
              "$n = New-Object System.Windows.Forms.NotifyIcon; $n.Icon = [System.Drawing.SystemIcons]::Information; "
              f"$n.Visible = $true; $n.ShowBalloonTip(15000, {q(title)}, {q(body)}, 'Info'); "
              "Start-Sleep -Seconds 16; $n.Dispose()")
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        print("desktop alert error:", e)


GROUP_NOTE = "\n\nℹ️ Educational only, not investment advice. Do your own research."


def group_chat_id():
    """TELEGRAM_GROUP_CHAT_ID; group ids are always negative, so a missing minus sign is added back."""
    g = os.environ.get("TELEGRAM_GROUP_CHAT_ID", "").strip()
    return "-" + g if g.isdigit() else g


def send_telegram(bot_token, chat_id, text):
    return _send_telegram(bot_token, chat_id, text)


def send_group(bot_token, text):
    """A copy for the friends' group (TELEGRAM_GROUP_CHAT_ID), when one is set and this bot is in it."""
    group = group_chat_id()
    return _send_telegram(bot_token, group, text + GROUP_NOTE) if group else False


def _send_telegram(bot_token, chat_id, text):
    try:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{bot_token}/sendMessage", data=data, timeout=10).read()
        return True
    except Exception as e:
        why = ""
        try:
            why = json.loads(e.read()).get("description", "")   # Telegram says why, e.g. "bot was kicked"
        except Exception:
            pass
        print("telegram error:", e, why)
        return False


# ───────────────────────── local web server ─────────────────────────

ALLOWED_ORIGINS = ("https://www.indmoney.com", "https://indmoney.com", "https://www.indstocks.com")

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Buy/Sell signals</title><style>body{font:14px system-ui;background:#111;color:#eee;margin:16px}
td{padding:6px 10px;border-bottom:1px solid #333;vertical-align:top}.dot{display:inline-block;width:12px;height:12px;border-radius:50%}
small{color:#999}</style>
<h3>Buy/Sell signals v2</h3><div id=s></div><table id=t></table>
<p><input id=c placeholder="Symbol: BTCUSDT, ^NSEI, AAPL, GC=F"> <button onclick=add()>Add</button></p><script>
async function add(){const r=await fetch('/watch',{method:'POST',headers:{'X-BS-Panel':'1','Content-Type':'application/json'},body:JSON.stringify({code:c.value.trim()})});
const j=await r.json();if(j.error)alert(j.error);c.value='';f()}
const C={up:'#00c853',down:'#ff1744',none:'#2979ff'};
const p=x=>x==null?'-':Number(x).toLocaleString(undefined,{maximumFractionDigits:2});
function tr(i){const t=i.trade;if(!t)return i.last?`<small>Last: ${i.last.side>0?'BUY':'SELL'} ${p(i.last.entry)} → ${i.last.status||''} ${i.last.r!=null?i.last.r+'R':''}</small>`:'';
return `<b style="color:${t.side>0?'#00c853':'#ff1744'}">${t.side>0?'BUY':'SELL'} ${p(t.entry)}</b> stop ${p(t.stop)} target ${p(t.target)}<br><small>${t.why}</small>`}
async function f(){try{const d=await (await fetch('/signals')).json();
const er=Object.entries(d.errors||{}).map(([k,v])=>k+': '+v).join(' | ');
s.textContent=(d.demo?'DEMO DATA · ':'')+(d.updated?'Updated '+new Date(d.updated*1000).toLocaleTimeString():'starting')+(er?' · '+er:'');
t.innerHTML=d.items.map(i=>`<tr><td><span class=dot style="background:${C[i.bias]||'#555'}"></span></td><td>${i.name}<br><small>${i.tf}</small></td><td>${p(i.ltp)}</td><td>${tr(i)}<br><small>${(i.checks||[]).join(' · ')}</small></td></tr>`).join('')}catch(e){s.textContent='offline'}}
f();setInterval(f,3000)</script>"""


def make_handler(engine):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            origin = self.headers.get("Origin", "")
            if origin in ALLOWED_ORIGINS:
                self.send_header("Access-Control-Allow-Origin", origin)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/signals"):
                self._send(200, json.dumps(engine.snapshot()).encode(), "application/json")
            elif self.path.startswith("/news"):
                desk = getattr(engine, "news", None)
                snap = desk.snapshot() if desk else {"items": [], "errors": {"news": "turned off in config.json"}, "updated": None}
                self._send(200, json.dumps(snap).encode(), "application/json")
            elif self.path.startswith("/stats"):
                self._send(200, json.dumps(engine.stats()).encode(), "application/json")
            elif self.path in ("/", "/index.html"):
                dash = os.path.join(HERE, "dashboard.html")
                if os.path.exists(dash):
                    with open(dash, "rb") as f:
                        self._send(200, f.read(), "text/html; charset=utf-8")
                else:
                    self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            # custom header blocks other websites from changing your watch list
            if self.headers.get("X-BS-Panel") != "1":
                return self._send(403, b'{"error":"forbidden"}', "application/json")
            try:
                n = int(self.headers.get("Content-Length", 0) or 0)
                body = json.loads(self.rfile.read(min(n, 10000)) or b"{}")
                if self.path.startswith("/watch"):
                    res = {"ok": True, "added": engine.add_watch(body)}
                elif self.path.startswith("/select"):
                    res = {"ok": True, "selected": engine.select(body.get("codes", "ALL"))}
                elif self.path.startswith("/unwatch"):
                    res = {"ok": engine.remove_watch(str(body.get("code", "")))}
                else:
                    return self._send(404, b'{"error":"not found"}', "application/json")
                self._send(200, json.dumps(res).encode(), "application/json")
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")

        def log_message(self, *args):
            pass

    return Handler


def main():
    argv = sys.argv[1:]
    cfg = load_config()
    if "--test-alert" in argv:
        desktop_alert("test\n🔔 Test alert\nAlerts are on. Signals will pop up like this.")
        time.sleep(3)
        return
    if "--test-telegram" in argv:
        tg = cfg["telegram"]
        if not tg.get("bot_token") or not tg.get("chat_id"):
            sys.exit("Put telegram bot_token and chat_id in config.json first.")
        ok = send_telegram(tg["bot_token"], tg["chat_id"], "✅ Signal program connected. Alerts will arrive here.")
        print("Sent, check your phone." if ok else "Failed, see the error above.")
        return
    demo = "--demo" in argv
    if demo:
        cfg["watch"] = [{"name": "DEMO NIFTY", "code": "DEMO1"}, {"name": "DEMO BTC", "code": "DEMO2"},
                        {"name": "DEMO GOLD", "code": "DEMO3"}]
    CUSTOM_STEPS.update(cfg.get("strikes") or {})
    if os.name == "nt" and cfg.get("keep_awake", True):
        # stop Windows going to sleep while the program runs (the screen can still turn off)
        import ctypes
        flags = 0x80000000 | 0x00000001  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        if cfg.get("keep_screen_on", True):
            flags |= 0x00000002  # ES_DISPLAY_REQUIRED: no screen-off, so no lock and no low-power Wi-Fi cut
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
        print("Keeping this laptop awake" + (" with the screen on" if flags & 2 else "")
              + " while the program runs. Closing the lid can still put it to sleep.")
    engine = Engine(cfg, demo=demo)
    threading.Thread(target=engine.run, daemon=True).start()
    engine.news = None
    if cfg["news"].get("enabled", True) and not demo:
        engine.news = N.NewsDesk(cfg["news"], engine.alert)
        threading.Thread(target=engine.news.run, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", cfg["port"]), make_handler(engine))
    modes = ", ".join(f"{m} ({cfg['modes'][m]['timeframe']} {cfg['modes'][m]['rule']})" for m in cfg["enabled_modes"])
    print(f"Signal program v2 running{' (DEMO DATA)' if demo else ''}. {len(engine.items)} row(s); modes: {modes}.")
    print(f"Dashboard: http://127.0.0.1:{cfg['port']} (any browser). Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped")


if __name__ == "__main__":
    main()
