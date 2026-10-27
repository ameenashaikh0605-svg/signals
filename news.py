"""Market news headlines from free RSS feeds, tagged by market and direction.
Read-only. Used by signal_server.py for the dashboard news panel and Telegram alerts on big headlines."""
import html
import re
import time
import threading
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime


def _gnews(q):
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q + " when:1d", "hl": "en-IN", "gl": "IN", "ceid": "IN:en"})


FEEDS = [
    ("Economic Times", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Moneycontrol", "https://www.moneycontrol.com/rss/marketreports.xml"),
    ("Mint", "https://www.livemint.com/rss/markets"),
    ("Google News", _gnews("Sensex OR Nifty OR \"Bank Nifty\" OR RBI")),
    ("Google News", _gnews("\"Wall Street\" OR \"Dow Jones\" OR Fed OR \"crude oil\" OR \"gold price\"")),
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
]

# which market a headline is about (keys match the dashboard market names)
MARKETS = {
    "India": r"\b(nifty|sensex|bank nifty|banknifty|rbi|sebi|dalal street|fii|fpi|dii|rupee|india|indian)\b",
    "Bitcoin": r"\b(bitcoin|btc|crypto|cryptocurrency|ether|ethereum|coinbase|binance)\b",
    "Gold": r"\b(gold|bullion|silver)\b",
    "Crude": r"\b(crude|oil|brent|opec|wti)\b",
    "US": r"\b(dow|wall street|s&p|nasdaq|fed|fomc|powell|treasury|us stocks|u\.s\. stocks)\b",
}

# headlines that can move a whole market: these go to Telegram
BIG = (r"\b(repo rate|rate cut|rate hike|cuts rates|hikes rates|monetary policy|mpc|fomc|powell|fed chair|"
       r"budget|inflation|cpi|gdp|recession|war\b|wars\b|attack|missile|ceasefire|sanction|tariff|crash|plunge|"
       r"plummet|tumble|sell-off|selloff|lower circuit|upper circuit|record high|all-time high|lifetime high|"
       r"etf approv|bans?\b|default|bankrupt|opec\+? (cut|hike)|emergency|shutdown|election result)")

UP = r"\b(surge|surges|soar|soars|rall(y|ies)|jump|jumps|gain|gains|climb|climbs|rise|rises|rebound|rebounds|record high|all-time high|lifetime high|up \d|higher|bull)\w*"
DOWN = r"\b(fall|falls|drop|drops|slip|slips|slide|slides|plunge|plunges|tumble|tumbles|crash|crashes|sink|sinks|decline|declines|lower|sell-off|selloff|down \d|bear|slump|slumps|loss|losses)\w*"


def _text(el, *names):
    for n in names:
        x = el.find(n)
        if x is not None and (x.text or "").strip():
            return x.text.strip()
        if x is not None and x.get("href"):
            return x.get("href")
    return ""


def _when(s):
    if not s:
        return None
    try:
        return parsedate_to_datetime(s).timestamp()
    except (TypeError, ValueError):
        pass
    try:
        from datetime import datetime
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse(xml_bytes, source):
    """RSS or Atom -> list of {title, link, source, t}."""
    root = ET.fromstring(xml_bytes)
    out = []
    atom = "{http://www.w3.org/2005/Atom}"
    for it in root.iter("item"):
        title = html.unescape(_text(it, "title"))
        src = _text(it, "source") or source
        if " - " in title:  # "Headline - Publisher" (Google News and some others)
            head, tail = title.rsplit(" - ", 1)
            if len(tail) <= 35 and len(head) > 20:
                title, src = head, (tail if source == "Google News" else src)
        out.append({"title": title.strip(), "link": _text(it, "link"), "source": src.strip(),
                    "t": _when(_text(it, "pubDate"))})
    for it in root.iter(atom + "entry"):
        out.append({"title": html.unescape(_text(it, atom + "title")).strip(), "link": _text(it, atom + "link"),
                    "source": source, "t": _when(_text(it, atom + "updated", atom + "published"))})
    return [x for x in out if x["title"]]


def tag(item):
    t = item["title"].lower()
    item["markets"] = [m for m, rx in MARKETS.items() if re.search(rx, t)]
    up, down = len(re.findall(UP, t)), len(re.findall(DOWN, t))
    item["mood"] = "up" if up > down else "down" if down > up else ""
    item["big"] = bool(re.search(BIG, t)) and bool(item["markets"])
    return item


def _key(title):
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()[:90]


class NewsDesk:
    """Polls the feeds in the background; keeps the latest headlines; calls `on_big(text)` once per
    new big headline (never for headlines already there when the program starts)."""

    def __init__(self, cfg, on_big):
        self.cfg = cfg
        self.on_big = on_big
        self.items = []
        self.errors = {}
        self.seen = set()
        self.sent = []
        self.updated = None
        self.lock = threading.Lock()

    def fetch(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (signal program news reader)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read()

    def poll(self, first=False):
        found, errors = [], {}
        for name, url in FEEDS:
            try:
                found += [tag(x) for x in parse(self.fetch(url), name)]
            except Exception as e:
                errors[name] = str(e)[:120]
        now = time.time()
        fresh = {}
        for x in found:
            if x["t"] and now - x["t"] > 24 * 3600:
                continue
            k = _key(x["title"])
            if k and k not in fresh:
                x["t"] = x["t"] or now
                fresh[k] = x
        new = [x for k, x in fresh.items() if k not in self.seen]
        self.seen.update(fresh)
        with self.lock:
            self.items = sorted(fresh.values(), key=lambda x: -x["t"])[:80]
            self.errors = errors
            self.updated = now
        if first or not self.cfg.get("telegram_big", True):
            return
        self.sent = [t for t in self.sent if now - t < 3600]
        for x in sorted(new, key=lambda x: x["t"]):
            if not x["big"] or now - x["t"] > 3 * 3600:
                continue
            if len(self.sent) >= self.cfg.get("max_alerts_per_hour", 4):
                break
            self.sent.append(now)
            icon = {"up": "🟢", "down": "🔴"}.get(x["mood"], "📰")
            self.on_big(f"📰 BIG NEWS · {', '.join(x['markets'])}\n{icon} {x['title']}\n"
                        f"{x['source']} · {time.strftime('%H:%M', time.localtime(x['t']))}\n{x['link']}")

    def run(self):
        first = True
        while True:
            try:
                self.poll(first)
                first = False
            except Exception as e:
                print("news error:", e)
            time.sleep(max(2, self.cfg.get("interval_min", 5)) * 60)

    def snapshot(self):
        with self.lock:
            return {"items": self.items, "errors": self.errors, "updated": self.updated}
