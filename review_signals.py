"""Print how every open or waiting swing signal is doing right now (US stocks and crypto). Read-only: sends nothing."""
import json
import time

import swing_scan as W


def show(name, side, entry, stop, risk, price, hi, lo, note):
    r = (price - entry) * side / risk if risk else 0
    best = ((hi - entry) if side > 0 else (entry - lo)) / risk if risk else 0
    worst = ((lo - entry) if side > 0 else (entry - hi)) / risk if risk else 0
    print(f"{name:10} {'BUY' if side > 0 else 'SELL':4} {note:9} entry {entry:.4g} stop {stop:.4g} now {price:.4g} "
          f"= {r:+.2f}R (best {best:+.2f}R, worst {worst:+.2f}R)")


def us():
    rows = json.load(open("us_state.json"))["rows"]
    for sym, row in sorted(rows.items()):
        tr, pend = row.get("trade"), row.get("pending")
        if not (tr or pend):
            continue
        try:
            bars = W.yahoo_daily(sym, live=True)
        except Exception as e:
            print(sym, "no data", e)
            continue
        if tr:
            since = [b for b in bars if b["day"] > tr["day"]] or bars[-1:]
            show(sym, tr["side"], tr["entry"], tr["stop"], tr["risk"], bars[-1]["c"],
                 max(b["h"] for b in since), min(b["l"] for b in since), "open")
        else:
            since = [b for b in bars if b["day"] > pend["day"]]
            hi = max((b["h"] for b in since), default=None)
            print(f"{sym:10} SETUP buy above {pend['trigger']:.4g}: now {bars[-1]['c']:.4g}, high since {hi}, "
                  f"{'TRIGGERED' if hi and hi > pend['trigger'] else 'not triggered'}")


def crypto():
    rows = json.load(open("crypto_state.json"))["rows"]
    for sym, row in sorted(rows.items()):
        tr, pend = row.get("trade"), row.get("pending")
        if tr:
            t0 = int(time.mktime(time.strptime(tr["day"][:10], "%Y-%m-%d"))) + 86400
            b = W.binance_since(sym, t0)
            show(sym, tr["side"], tr["entry"], tr["stop"], tr["risk"], b["c"], b["h"], b["l"], "open")
        elif pend:
            b = W.binance_since(sym, pend["t"] + 86400)
            hit = b["l"] < pend["trigger"] if pend["side"] < 0 else b["h"] > pend["trigger"]
            print(f"{sym:10} SETUP {'sell below' if pend['side'] < 0 else 'buy above'} {pend['trigger']:.4g}: "
                  f"now {b['c']:.4g}, range {b['l']:.4g}-{b['h']:.4g}, {'TRIGGERED' if hit else 'not triggered'}")


if __name__ == "__main__":
    W.CUR = ""
    print("== US"); us()
    print("== CRYPTO"); crypto()
