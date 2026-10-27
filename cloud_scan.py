"""One pass of the signal program, for running on a schedule in the cloud (GitHub Actions) with the laptop off.

Each run: download the latest candles, check every candle that closed since the last run, send Telegram alerts
for new signals and for target / stop-loss hits, check the news for big headlines, then save the state
(open trades, last candle seen, news already seen) to cloud_state.json for the next run.

Telegram details come from the environment (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID), never from files.
Read-only: it never places orders."""
import json
import os
import sys
import time

import signal_server as SS
import news as N

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "cloud_state.json")
CATCH_UP_SECONDS = 3 * 3600   # after a long gap, only look at candles from the last 3 hours (no alert floods)
KEEP = ("trade", "last_signal", "last_bar_t", "bias_sent")


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE)


def make_engine(cfg):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    cfg["telegram"] = dict(cfg.get("telegram") or {}, bot_token=token, chat_id=chat)
    cfg["desktop_alerts"] = False
    cfg["enabled_modes"] = [m for m in cfg["enabled_modes"] if m != "fast"]   # needs a program running every second
    cfg["timemachine"] = dict(cfg.get("timemachine") or {}, enabled=False)
    cfg["selected"] = "ALL"
    engine = SS.Engine(cfg)
    sent = []

    def alert(text, mode=None):  # send now: the run ends right after, so no background threads
        print("\n" + text + "\n")
        if token and chat:
            sent.append(SS.send_telegram(token, chat, text))
    engine.alert = alert
    return engine, sent


def scan(engine, state):
    rows = state.setdefault("rows", {})
    now = time.time()
    for it in engine.items:
        key = f"{it['code']}|{it['mode']}"
        saved = rows.get(key) or {}
        for k in KEEP:
            if k in saved:
                it[k] = saved[k]
        try:
            bars, ltp, info = engine.sources[it["source"]].candles(it["code"], it["tf"])
        except Exception as e:
            print(f"{it['name']} {it['tf']}: {e}")
            continue
        if not bars:
            continue
        it["info"] = info
        sec = SS.TF_SECONDS[it["tf"]]
        last = it.get("last_bar_t")
        if last is None:
            # first run for this market: learn the history, alert nothing
            it["bars"], it["ltp"] = bars, ltp if ltp is not None else bars[-1]["c"]
            engine.on_close(it, first_load=True)
        else:
            fresh = [j for j, b in enumerate(bars) if b["t"] > last and b["t"] + sec >= now - CATCH_UP_SECONDS]
            for j in fresh:   # every candle that closed since the last run, oldest first
                it["bars"], it["ltp"] = bars[:j + 1], bars[j]["c"]
                engine.follow_trade(it)
                engine.on_close(it, first_load=False)
            it["bars"], it["ltp"] = bars, ltp if ltp is not None else bars[-1]["c"]
        engine.follow_trade(it)
        it["last_bar_t"] = bars[-1]["t"]
        rows[key] = {k: it.get(k) for k in KEEP}


def news_pass(cfg, engine, state):
    ncfg = cfg.get("news") or {}
    if not ncfg.get("enabled", True):
        return
    ns = state.setdefault("news", {})
    desk = N.NewsDesk(ncfg, engine.alert)
    desk.seen = set(ns.get("seen", []))
    desk.sent = ns.get("sent", [])
    desk.poll(first=not ns.get("seen"))
    ns["seen"] = list(desk.seen)[-600:]
    ns["sent"] = desk.sent
    if desk.errors:
        print("news feeds that failed:", desk.errors)


def main():
    cfg = SS.load_config()
    engine, sent = make_engine(cfg)
    if not cfg["telegram"]["bot_token"] or not cfg["telegram"]["chat_id"]:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set: alerts are printed here only.")
    if "--test-telegram" in sys.argv:
        engine.alert("✅ Cloud signal program connected. Alerts will now arrive here even with your laptop off.")
        sys.exit(0 if sent and all(sent) else 1)
    state = load_state()
    scan(engine, state)
    try:
        news_pass(cfg, engine, state)
    except Exception as e:
        print("news error:", e)
    state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)
    print(f"Run done: {len(engine.items)} rows, {len(sent)} message(s) sent.")


if __name__ == "__main__":
    main()
