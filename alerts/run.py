"""WhatsApp alert bot for the SMC v6 strategy. Runs every 15 minutes in GitHub Actions.

Each run: download candles from Yahoo -> replay v6 per market -> send WhatsApp (CallMeBot) for
events from the last WINDOW minutes that were not sent yet -> save sent event ids in state.json.
  python alerts/run.py            normal run (prints instead of sending if WA_PHONE/WA_APIKEY are not set)
  python alerts/run.py --replay   backtest the last ~60 days and print the trades (sends nothing)
"""
import json
import os
import sys
import time
from pathlib import Path

import pandas as pd
import requests

from strategy import MYT, replay

# Markets: Yahoo symbol, spread (price units), contract size per 1.0 lot and price decimals.
# CHECK spread / contract size against your broker's contract specs (MT5: Market Watch > Specification).
MARKETS = {
    "XAUUSD": dict(yahoo="GC=F", spread=0.30, contract=100, digits=2, tp_off=1.0,
                   note="gold FUTURES price, usually a few $ above your broker's spot price"),
    "USOIL": dict(yahoo="CL=F", spread=0.04, contract=1000, digits=2,
                  note="WTI FUTURES price, may differ a little from your broker's oil price"),
    "EURUSD": dict(yahoo="EURUSD=X", spread=0.00012, contract=100000, digits=5),
    "USDJPY": dict(yahoo="JPY=X", spread=0.015, contract=100000, digits=3, quote_is_jpy=True),
}
STATE = Path(__file__).with_name("state.json")
WINDOW = pd.Timedelta(minutes=90)  # GitHub's 15-min schedule can run late; older events are not re-sent


def candles(symbol, interval, rng):
    """Closed candles from Yahoo's public chart endpoint (no key needed), UTC index."""
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                     params=dict(interval=interval, range=rng), headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame({k: q[k] for k in ("open", "high", "low", "close")},
                      index=pd.to_datetime(res["timestamp"], unit="s", utc=True)).dropna()
    step = pd.Timedelta(interval.replace("m", "min"))
    df.index = df.index.floor(step)                 # Yahoo stamps hourly FX candles at :00 or odd seconds
    df = df[~df.index.duplicated(keep="last")]
    return df[df.index + step <= pd.Timestamp.now(tz="UTC")]  # drop the candle still forming


def lots(mk, st, price, balance, risk_pct):
    """Lot size per order so each risks 1/3 of risk_pct of the balance (min 0.01)."""
    out = []
    for px in st.px:
        loss_per_lot = (abs(st.sl - px) + mk["spread"]) * mk["contract"]
        if mk.get("quote_is_jpy"):
            loss_per_lot /= price  # JPY -> USD
        lot = max(0.01, int(balance * risk_pct / 100 / 3 / loss_per_lot * 100) / 100)
        out.append((lot, lot * loss_per_lot))
    return out


def send_whatsapp(text):
    phone, key = os.environ.get("WA_PHONE"), os.environ.get("WA_APIKEY")
    if not phone or not key:
        print("--- (no WA_PHONE/WA_APIKEY: not sent) ---\n" + text)
        return
    for part in [text[i:i + 1500] for i in range(0, len(text), 1500)]:
        try:
            r = requests.get("https://api.callmebot.com/whatsapp.php",
                             params=dict(phone=phone, text=part, apikey=key), timeout=30)
            print("WhatsApp status", r.status_code)  # never print the URL: it holds the phone number and key
        except requests.RequestException as e:
            print("WhatsApp send failed:", type(e).__name__)
        time.sleep(3)


def main():
    replay_only = "--replay" in sys.argv
    now = pd.Timestamp.now(tz="UTC")
    balance = float(os.environ.get("ACCOUNT_BALANCE") or 100)
    risk_pct = float(os.environ.get("RISK_PCT") or 20)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    sent = set(state.get("sent", []))
    msgs, status, all_trades = [], [], []

    for sym, mk in MARKETS.items():
        try:
            m15 = candles(mk["yahoo"], "15m", "60d")
            h1 = candles(mk["yahoo"], "60m", "1y")
        except (requests.RequestException, KeyError, IndexError, TypeError) as e:
            status.append(f"{sym}: price data failed ({type(e).__name__})")
            continue
        events, trades, st = replay(m15, h1, mk["spread"], mk.get("tp_off"), mk["digits"])
        price = m15["close"].iloc[-1]
        all_trades += [dict(t, market=sym) for t in trades]
        for ev in events:
            eid = f"{sym}|{ev.kind}|{ev.time.isoformat()}"
            if replay_only or eid in sent or ev.time < now - WINDOW:
                continue
            text = f"{sym} {ev.text}"
            if ev.kind in ("SETUP", "MOVED"):
                head = "NEW SETUP" if ev.kind == "SETUP" else "SETUP MOVED: delete the old orders, place these"
                target = balance * risk_pct / 100 / 3
                sizes = "\n".join(f"Lots order {k + 1}: {lot:.2f} (risk ${r:.2f})" + (" - MIN LOT RISKS MORE THAN YOUR SETTING" if r > target * 1.05 else "")
                                  for k, (lot, r) in enumerate(lots(mk, ev.setup, price, balance, risk_pct)))
                text = f"{sym} {head}\n{ev.text}\n{sizes}" + (f"\nNote: {mk['note']}" if mk.get("note") else "")
            msgs.append(text)
            sent.add(eid)
        if st is None:
            status.append(f"{sym}: no setup")
        elif not st.open_orders():
            status.append(f"{sym}: pending {'BUY' if st.dir == 1 else 'SELL'} setup, order 1 at {st.px[0]:.{mk['digits']}f}, SL {st.sl:.{mk['digits']}f}")
        else:
            status.append(f"{sym}: IN TRADE ({'BUY' if st.dir == 1 else 'SELL'}), orders open {[k + 1 for k in st.open_orders()]}, "
                          f"SL {st.sl:.{mk['digits']}f}, TP1 {st.tp1:.{mk['digits']}f}")

    if replay_only:
        print_replay(all_trades)
        print("\n".join(status))
        return

    # daily check at the first run after 07:00 MYT (Mon-Fri), or when started by hand with SUMMARY=true
    myt = now.tz_convert(MYT)
    today = myt.strftime("%Y-%m-%d")
    if os.environ.get("SUMMARY") == "true" or (myt.hour >= 7 and myt.weekday() < 5 and state.get("daily") != today):
        msgs.append("Daily check (bot is running)\n" + "\n".join(status) + f"\nBalance setting ${balance:.0f}, risk {risk_pct:.0f}%")
        state["daily"] = today
    if msgs:
        send_whatsapp("\n\n".join(msgs))
    state["sent"] = sorted(sent)[-500:]
    STATE.write_text(json.dumps(state, indent=1) + "\n")


def print_replay(trades):
    trades = sorted(trades, key=lambda t: t["exit"])
    for t in trades:
        print(f"{t['market']:7s} {'BUY ' if t['dir'] == 1 else 'SELL'} filled {t['entry'].tz_convert(MYT):%m-%d %H:%M} "
              f"exit {t['exit'].tz_convert(MYT):%m-%d %H:%M} {t['how']:4s} {t['R']:+.2f}R")
    for risk in (10, 20):
        bal = peak = 100.0
        dd = 0.0
        for t in trades:
            bal *= 1 + t["R"] * risk / 100
            peak = max(peak, bal)
            dd = max(dd, 1 - bal / peak)
        print(f"risk {risk}%: {len(trades)} trades, net {sum(t['R'] for t in trades):+.2f}R, $100 -> ${bal:.2f}, max drawdown {dd * 100:.1f}%")


if __name__ == "__main__":
    main()
