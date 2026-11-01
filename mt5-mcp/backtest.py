"""Replay the live rules (smc.evaluate) over M5 history exported from MT5.

    python backtest.py --csv XAUUSD_M5.csv --broker-utc-offset 3 --from 2025-06-01

Every higher timeframe is built from the M5 file. Trades are walked forward bar by bar on M5.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import timedelta

import numpy as np
import pandas as pd
import yaml

import smc

BARS = {"W1": 120, "D1": 250, "H4": 400, "H1": 600, "M15": 400, "M5": 400}
RULES = {"M15": "15min", "H1": "1h", "H4": "4h", "D1": "1D"}
SPEC = dict(point=0.01, digits=2, tick_size=0.01, tick_value=1.0, volume_min=0.01, volume_max=100.0, volume_step=0.01)


def load_m5(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["time"]).set_index("time").sort_index()
    return df[["open", "high", "low", "close"]][~df.index.duplicated()]


def build_frames(m5: pd.DataFrame) -> dict[str, pd.DataFrame]:
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    f = {"M5": m5}
    for tf, rule in RULES.items():
        f[tf] = m5.resample(rule).agg(agg).dropna()
    f["W1"] = m5.resample("W-SUN", closed="left", label="left").agg(agg).dropna()  # MT5 weeks start Sunday
    return f


def simulate(setup: dict, bars: pd.DataFrame, spread: float, frac: float = 0.5) -> dict:
    """Walk M5 bars after entry. Result in R. Same-bar conflicts resolve against us (stop first).
    Sells exit at the ask (bar price + spread). Partial at tp1 then stop to break-even."""
    sell, e = setup["side"] == "sell", setup["entry"]
    sl, tp, tp1 = setup["sl"], setup["tp"], setup["tp1"]
    risk = abs(e - sl)
    r_of = (lambda x: (e - x) / risk) if sell else (lambda x: (x - e) / risk)
    stop, realized, left, partial = sl, 0.0, 1.0, False
    for ts, o, h, l, c in bars[["open", "high", "low", "close"]].itertuples():
        hi, lo = (h + spread, l + spread) if sell else (h, l)          # ask-adjusted for sells
        adverse, favour = (hi, lo) if sell else (lo, hi)
        if (adverse >= stop) if sell else (adverse <= stop):
            return {"exit_time": ts, "r": realized + left * r_of(stop), "outcome": "BE" if partial else "SL"}
        hit_tp = (favour <= tp) if sell else (favour >= tp)
        hit_tp1 = (favour <= tp1) if sell else (favour >= tp1)
        if (hit_tp1 or hit_tp) and not partial:
            realized, left, partial = frac * r_of(tp1), 1 - frac, True
            stop = e
            if (adverse >= stop) if sell else (adverse <= stop):       # came back to entry in the same bar
                return {"exit_time": ts, "r": realized, "outcome": "BE"}
        if hit_tp:
            return {"exit_time": ts, "r": realized + left * r_of(tp), "outcome": "TP"}
    last = bars.close.iloc[-1] + (spread if sell else 0)
    return {"exit_time": bars.index[-1], "r": realized + left * r_of(last), "outcome": "OPEN"}


def run(m5: pd.DataFrame, cfg: dict, offset: float, spread: float, start=None, end=None, equity=10000.0):
    fr = build_frames(m5)
    dur = {tf: pd.Timedelta(minutes=smc.TF_MINUTES[tf]) for tf in fr}
    ends = {tf: (df.index + dur[tf]).values for tf, df in fr.items()}
    start, end = (pd.Timestamp(start) if start else None), (pd.Timestamp(end) if end else None)
    trades, why, next_free, entries = [], Counter(), pd.Timestamp.min, []
    for ts in fr["M15"].index:
        t = ts + dur["M15"]
        if t < next_free or (start is not None and t < start) or (end is not None and t > end):
            continue
        now = (t.to_pydatetime() + timedelta(hours=8 - offset)).replace(tzinfo=smc.MYT)
        if not smc.time_ok(now, cfg)[0]:
            continue
        t64 = np.datetime64(t)
        k5 = int(np.searchsorted(ends["M5"], t64, side="right"))
        if k5 == 0 or t - fr["M5"].index[k5 - 1] > pd.Timedelta(minutes=30):
            continue                                                      # stale / gap in data
        data = {}
        for tf in fr:
            k = int(np.searchsorted(ends[tf], t64, side="right"))
            data[tf] = fr[tf].iloc[max(0, k - BARS[tf]):k].reset_index(drop=True)
        bid = float(fr["M5"].close.iloc[k5 - 1])
        n24 = sum(1 for e in entries if t - e < pd.Timedelta(hours=24))
        try:
            res = smc.evaluate(data, cfg, SPEC, equity, {"bid": bid, "ask": bid + spread}, now, 0, n24)
        except (KeyError, ValueError, IndexError):
            continue                                                      # not enough history yet
        if res["decision"] != "TRADE":
            why[res["checks"][-1]["rule"]] += 1
            continue
        s = res["setup"]
        out = simulate(s, fr["M5"].iloc[k5:], spread, cfg["target"]["partial_fraction"])
        entries.append(t)
        next_free = out["exit_time"]
        trades.append({"entry_time": t, "side": s["side"], "entry": s["entry"], "sl": s["sl"], "tp": s["tp"], "planned_rr": s["rr"],
                       "zone_tf": s["zone"]["tf"], "confirm": "+".join(s["confirmations"]), "confirm_tf": s["confirm_tf"], **out})
    return pd.DataFrame(trades), why


def report(tr: pd.DataFrame, why: Counter, risk_pct: float) -> str:
    if tr.empty:
        return "No trades. Rules that blocked setups: " + str(dict(why))
    r = tr.r
    wins, losses = r[r > 0].sum(), -r[r < 0].sum()
    dd = (r.cumsum().cummax() - r.cumsum()).max()
    lines = [f"Period        {tr.entry_time.min():%Y-%m-%d} -> {tr.exit_time.max():%Y-%m-%d}",
             f"Trades        {len(tr)}  (sell {int((tr.side == 'sell').sum())}, buy {int((tr.side == 'buy').sum())})",
             f"Win rate      {(r > 0).mean():.0%}   (outcomes: {({k: int(v) for k, v in tr.outcome.value_counts().items()})})",
             f"Expectancy    {r.mean():+.2f} R per trade",
             f"Total         {r.sum():+.1f} R   | at {risk_pct}% risk, compounded: {np.prod(1 + r * risk_pct / 100) - 1:+.1%}",
             f"Profit factor {wins / losses:.2f}" if losses else "Profit factor n/a (no losing trades)",
             f"Max drawdown  {dd:.1f} R",
             f"Best / worst  {r.max():+.2f} R / {r.min():+.2f} R",
             "Setups blocked by rule (evaluated points): " + str(dict(why.most_common()))]
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="M5 export: time,open,high,low,close (broker time)")
    ap.add_argument("--broker-utc-offset", type=float, required=True, help="broker server time minus UTC, in hours (e.g. 3)")
    ap.add_argument("--spread", type=float, default=0.30, help="constant spread in price units")
    ap.add_argument("--from", dest="start"); ap.add_argument("--to", dest="end")
    ap.add_argument("--config", default="strategy.yaml"); ap.add_argument("--out", default="trades.csv")
    a = ap.parse_args()
    cfg = yaml.safe_load(open(a.config))
    trades, why = run(load_m5(a.csv), cfg, a.broker_utc_offset, a.spread, a.start, a.end)
    trades.to_csv(a.out, index=False)
    print(report(trades, why, cfg["risk"]["per_trade_pct"]))
    print(f"\nTrade list saved to {a.out}")
