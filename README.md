# SMC v6 trading strategy + Telegram / WhatsApp alerts

Supply/demand zone strategy (D1 trend, H4 filter, fresh H4/H1 zones, 3 limit orders, TP1 at 1.5R + runner to
the next zone), backtested in TradingView, plus a bot that sends Telegram (and optionally WhatsApp) alerts so
the trades can be placed by hand in MT5 (e.g. on Android). The bot never connects to a broker account.

| Folder | What |
|---|---|
| `pine/smc_sd_v6.pine` | The strategy (TradingView Pine v6). Other `.pine` files are earlier versions and experiments. |
| `alerts/` | Alert bot: `strategy.py` = v6 rules in Python, `run.py` = data, messaging, state. |
| `.github/workflows/alerts.yml` | Runs the bot every 15 minutes on GitHub Actions. |
| `mt5-mcp/` | MCP server + Python backtest for MT5 (needs MT5 on Windows). |

Markets: XAUUSD, EURUSD, USDJPY. WTI (USOIL) is switched off in `alerts/run.py` after a losing replay.

## Setup (from a phone or any browser)

1. **Telegram bot:** in Telegram open **@BotFather**, send `/newbot`, pick a name and a username ending in `bot`.
   BotFather replies with a **token** (looks like `123456789:AAH...`). Open your new bot and press **Start**
   (a bot can only message you after you have messaged it once).
2. **Your chat id:** open **@userinfobot**, press Start; it replies with your numeric **Id**.
3. **Secrets:** repo **Settings > Secrets and variables > Actions > Secrets**, add
   - `TG_TOKEN` = the BotFather token
   - `TG_CHAT_ID` = your Id
   - optional WhatsApp via CallMeBot (when its free bot accepts new users): `WA_PHONE` (`+60...`) and `WA_APIKEY`
4. **Variables** (same page, *Variables* tab, optional): `ACCOUNT_BALANCE` (default 100), `RISK_PCT` (default 20).
   Update the balance from time to time so lot sizes stay right.
5. **Actions** tab > *smc-alerts* > **Run workflow**: you should get a "Daily check" message within a minute or two.
   After that it runs every 15 minutes (Sun 22:00 - Fri 22:00 UTC) and sends a daily check after 07:00 MYT.

Never put the token, chat id or phone number in a file: this repo is public; secrets are hidden.
Anyone with the token can send messages as your bot: if it leaks, revoke it in @BotFather (`/revoke`).

## Alerts

| Alert | What you do in MT5 |
|---|---|
| NEW SETUP (zone, 3 limit prices, SL, TP1, TP2, lots) | Place the 3 pending orders with SL/TP |
| SETUP MOVED | Delete the old pending orders, place the new ones |
| setup cancelled | Delete the pending orders |
| order 2 + 3 filled. Change TP of orders 1+2 to X | Edit the TP of orders 1 and 2 |
| TP1 hit: move order 3 SL to X | Edit order 3's SL (break-even) |
| 12h without TP1: CLOSE ALL | Close the open orders, delete pending ones |
| TP2 / break-even / stop loss | Nothing (MT5 closed it), just information |

## Limits - read before trading real money

- **Data:** free Yahoo prices, not your broker's. Gold is a **futures** price: usually a few
  dollars above spot, so shift every level by (your MT5 price - "data price at setup"). Forex is close.
- **Timing:** GitHub's schedule can start runs several minutes late (sometimes more), so alerts are not instant.
- **Same rules as the backtest, two changes for manual trading:** levels are frozen when a setup is found
  (with a steadier 24h ATR for the SL buffer) and pending orders are not removed around news.
- **Alert volume:** about 4-7 alerts per market per week in the last 2 months, many of them "setup moved", some at night.
- **Replay on Yahoo data (Aug 31 - Oct 31 2026, gold + EURUSD + USDJPY):** 13 trades, +12.8R; at 20% risk
  $100 -> $693 with a **35% worst drop** (10% risk: $300, 18% drop). WTI lost -1.2R over 4 trades, so it is off.
  Two months of data is not proof: start on a demo account.
- **$100 accounts:** the 0.01 minimum lot on gold can risk more than 20%; the alert warns when that happens.

## Run locally

```
pip install -r alerts/requirements.txt
python alerts/run.py --replay   # backtest the last ~60 days, prints trades and $100 results
python alerts/run.py            # one normal run; prints the messages if no Telegram/WhatsApp secrets are set
```
