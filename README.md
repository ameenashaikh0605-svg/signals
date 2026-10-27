# Buy/Sell signals (cloud)

Runs the signal program every 15 minutes on GitHub, so Telegram alerts keep coming with the laptop off.
Read-only: it only reads market prices and news, and never places orders.

- **What it sends:** 📅 swing (daily) and ⚡ intraday (15-minute) signals with entry, stop loss, target, lot size and
  the reasons; a message when the target or stop loss is hit; ⚡ big market news.
- **Not here:** the fast 1-minute signals (they need a program running every second).
- **Telegram details** are stored as repository secrets `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
  (Settings → Secrets and variables → Actions), never in the files.
- **Markets:** edit `watch` in `config.json`.
- **Pause it:** Actions tab → *Buy/Sell signals* → ⋯ → *Disable workflow*.
- `cloud_state.json`, `risk_today.json` and `signals_log.csv` are written by the runs (open trades, today's risk, history).

Not investment advice.
