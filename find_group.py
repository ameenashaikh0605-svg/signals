"""Print the Telegram groups each bot has been added to (name and chat id), so the group id can be saved as the
TELEGRAM_GROUP_CHAT_ID secret. With --test, the main bot (India swing alerts) says hello in the saved group. Never prints tokens."""
import json
import os
import sys
import urllib.request

import signal_server as SS

BOTS = {"Main": "TELEGRAM_BOT_TOKEN", "US": "US_TELEGRAM_BOT_TOKEN", "Crypto": "CRYPTO_TELEGRAM_BOT_TOKEN"}


def groups(token):
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15) as r:
        updates = json.load(r).get("result", [])
    found = {}
    for u in updates:
        for k in ("message", "my_chat_member", "channel_post", "edited_message"):
            chat = (u.get(k) or {}).get("chat") or {}
            if chat.get("type") in ("group", "supergroup"):
                found[chat["id"]] = chat.get("title", "")
    return found


ok = True
group = SS.group_chat_id()
for name, env in BOTS.items():
    token = os.environ.get(env, "").strip()
    if not token:
        print(f"{name} bot: not set up")
        continue
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getMe", timeout=15) as r:
            me = json.load(r)["result"]
        print(f"{name} bot is '{me.get('first_name')}' (@{me.get('username')})")
    except Exception as e:
        print(f"{name} bot: could not read its name ({e})")
    try:
        found = groups(token)
    except Exception as e:
        print(f"{name} bot: could not read updates ({e})")
        found = {}
    for cid, title in found.items():
        print(f"{name} bot sees group '{title}': chat id {cid}")
    if not found:
        print(f"{name} bot: no group seen yet (add it to the group, then send any message there)")
    if "--test" in sys.argv and group and name == "Main":
        sent = SS._send_telegram(token, group, "✅ Connected. India swing trade alerts (Nifty, Sensex, Bank Nifty, Midcap) will be posted here." + SS.GROUP_NOTE)
        print(f"{name} bot test to group: {'sent' if sent else 'FAILED'}")
        ok = ok and sent
if "--test" in sys.argv and not group:
    print("TELEGRAM_GROUP_CHAT_ID is not set yet.")
    ok = False
sys.exit(0 if ok else 1)
