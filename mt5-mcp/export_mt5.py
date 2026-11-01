"""Export M5 history from your MT5 terminal to CSV for backtest.py (run on the Windows PC with MT5 open).

    python export_mt5.py --symbol XAUUSD --from 2025-01-01 --out XAUUSD_M5.csv
"""
import argparse
from datetime import datetime, timezone

import MetaTrader5 as mt5
import pandas as pd

ap = argparse.ArgumentParser()
ap.add_argument("--symbol", default="XAUUSD")
ap.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD")
ap.add_argument("--to", dest="end", default=datetime.now().strftime("%Y-%m-%d"))
ap.add_argument("--out", default="XAUUSD_M5.csv")
a = ap.parse_args()

if not mt5.initialize():
    raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
mt5.symbol_select(a.symbol, True)
utc = lambda s: datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
rates = mt5.copy_rates_range(a.symbol, mt5.TIMEFRAME_M5, utc(a.start), utc(a.end))
if rates is None or len(rates) == 0:
    raise SystemExit(f"No data: {mt5.last_error()}. Raise Tools > Options > Charts > Max bars in chart, open an M5 chart and scroll back.")
df = pd.DataFrame(rates)
df["time"] = pd.to_datetime(df["time"], unit="s")          # broker server time, as MT5 reports it
df[["time", "open", "high", "low", "close"]].to_csv(a.out, index=False)
print(f"{len(df)} M5 bars  {df.time.iloc[0]} -> {df.time.iloc[-1]}  saved to {a.out}")
