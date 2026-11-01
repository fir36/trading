# MT5 gold strategy MCP server

Top-down structure strategy (W1/D1/H4 bias → H4/H1 zone → M15/M5 CHoCH or engulfing), enforced in code.
The AI client can only call the tools below; it cannot pass its own price, SL, TP or lot size.

## Setup (Windows, same PC as the MT5 terminal)

1. Install MT5, log in to a **demo** account, enable Algo Trading.
2. `pip install -r requirements.txt` (the `MetaTrader5` package only exists on Windows).
3. Edit `strategy.yaml`: set `symbol` to your broker's gold name (XAUUSD, XAUUSDm, GOLD...).
4. Add to Claude Desktop config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "mt5-smc": {
      "command": "python",
      "args": ["C:\\path\\to\\mt5-mcp\\server.py"]
    }
  }
}
```

Optional env vars: `MT5_PATH` (terminal64.exe), `MT5_LOGIN` / `MT5_PASSWORD` / `MT5_SERVER` (only if the terminal is not already logged in; never put these in a file you share).

Written for `mcp` 2.x. On `mcp` 1.x change the import to `from mcp.server.fastmcp import FastMCP as MCPServer`.

## Tools

| Tool | What it does |
|---|---|
| `check_setup` | Runs every rule, returns TRADE / NO_TRADE, the result of each rule, zones, and the computed entry / SL / TP / lots. Sends nothing. |
| `place_trade(confirm)` | Re-runs all rules; if TRADE, opens the position with the computed values. |
| `manage_positions` | At +1R closes half and moves the stop to break-even. Call it regularly, there is no background loop. |
| `account_status`, `get_rules` | Info only. |

## Rules enforced (all editable in `strategy.yaml`)

1. Limits: max 1 open position, max 2 entries per rolling 24h.
2. Time: 21:00-01:00 MYT only, no weekends, no Friday after 22:00, news blackout ±30 min for events you list.
3. Spread under 60 points.
4. Bias: W1, D1 and H4 must all be bearish (sell-only) or all bullish (buy-only). Otherwise no trade.
5. Zones: unbroken swing-high zones on H4/H1 above price (buys: swing-low zones below).
6. Price must have entered a zone in the last 3h and not closed beyond it.
7. Confirmation on M15 or M5, fresh (last 2 bars): bearish CHoCH or bearish engulfing (bullish for buys).
8. Stop = highest high since the zone touch + 0.25 ATR, between 1 and 4 ATR(M15). Wider = skip.
9. Target = nearest H1 swing low (buy: high). Reward must be at least 2R, else skip.
10. Lot size from 1% equity risk (hard cap 2%). If the minimum lot risks more, skip.

## Safety defaults

- `execution.dry_run: true`: nothing is sent until you set it to `false`.
- When live, `confirm=true` is required, and a non-demo account is refused unless `allow_live: true`.
- Orders carry SL and TP from the first moment.

## Not implemented

- Chart patterns (double top, head and shoulders) as zone or confirmation. Only CHoCH and engulfing.
- Limit entry at 50% of the engulfing candle (entries are market).
- Live news calendar. You list the day's events in `strategy.yaml`.
- Anything is optimised or backtested. The numbers are the ones from our discussion, not tuned values.

## Backtest (same rules as live)

1. On the MT5 PC: `python export_mt5.py --symbol XAUUSD --from 2025-01-01 --out XAUUSD_M5.csv`
   (raise Tools > Options > Charts > Max bars in chart first; export at least 12-18 months, because W1/D1 bias needs warm-up).
2. `python backtest.py --csv XAUUSD_M5.csv --broker-utc-offset 3 --from 2025-09-01`
   (offset = broker server time minus UTC, e.g. 3 in summer, 2 in winter for most brokers; the session filter uses MYT).

It builds W1/D1/H4/H1/M15 from the M5 file, runs `smc.evaluate` at every M15 close inside your sessions, walks each trade forward on M5
(partial at 1R, stop to break-even), and prints win rate, expectancy in R, profit factor, max drawdown, and which rule blocked how many setups.
Trades are saved to `trades.csv`.

Assumptions that flatter or hurt results: constant spread (`--spread`), no slippage, same-bar stop/target conflicts resolve against you,
a constant offset (wrong by 1h for part of the year around clock changes), one trade at a time. Treat results as an estimate, and
beware over-tuning `strategy.yaml` to one period: tune on one half of the data, check on the other.

## Tests

`python test_smc.py`, `python test_server.py`, `python test_backtest.py` (synthetic data and a fake MT5, no terminal needed).
The real MT5 calls (`order_send`, `order_check`, filling modes, stop levels, history) are untested here:
run on a demo account in dry-run first, then live-demo with the smallest lot.

Trading gold is high risk and this is not financial advice.
