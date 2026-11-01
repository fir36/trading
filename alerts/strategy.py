"""SMC v6 rules (pine/smc_sd_v6.pine) in Python, used by the WhatsApp alert bot.

The strategy is replayed candle by candle on M15 data, like the TradingView backtest:
- H4/H1 zones and D1/H4 structure only use higher-timeframe candles that have already closed.
- Orders decided at a candle's close can only fill from the next candle on.
One difference on purpose: entry/SL/TP levels are frozen when a setup is first found
(you place them by hand once) instead of drifting a little with ATR on every candle, and pending
orders are kept outside trading hours (only cancelled when the zone is no longer valid).
"""
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# v6 inputs (same values as the Pine script; the max-entries-per-24h limit is off, as chosen after the B/C test)
P = dict(tp1R=1.5, minRR=2.0, timeStopH=12, swD1=3, basePct=0.5, maxBase=3, legATR=1.0,
         slBufATR=0.5, maxSprPct=10.0, maxATR=4.0, breakFrom=500, breakTo=700, fridayCut=2200,
         newsTimes=(2030, 2200, 200), newsMin=30)
MYT = "Asia/Kuala_Lumpur"
M15 = pd.Timedelta(minutes=15)


# ---------- indicators ----------
def atr(df, n=14):
    """Pine ta.atr: RMA of the true range, seeded with the SMA of the first n values."""
    h, l, c = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    pc = np.r_[np.nan, c[:-1]]
    tr = np.where(np.isnan(pc), h - l, np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc))))
    out = np.full(len(tr), np.nan)
    if len(tr) >= n:
        out[n - 1] = tr[:n].mean()
        for i in range(n, len(tr)):
            out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


def structure(df, n):
    """Pine f_struct: 1 = higher high + higher low, -1 = lower high + lower low, 0 = mixed.
    A pivot is only known n candles after it forms (no lookahead)."""
    h, l = df["high"].to_numpy(), df["low"].to_numpy()
    hPrev = hLast = lPrev = lLast = math.nan
    out = np.zeros(len(h), dtype=int)
    for i in range(len(h)):
        p = i - n
        if p - n >= 0:
            if h[p] > h[p - n:p].max() and h[p] >= h[p + 1:i + 1].max():
                hPrev, hLast = hLast, h[p]
            if l[p] < l[p - n:p].min() and l[p] <= l[p + 1:i + 1].min():
                lPrev, lLast = lLast, l[p]
        out[i] = -1 if (hLast < hPrev and lLast < lPrev) else 1 if (hLast > hPrev and lLast > lPrev) else 0
    return out


def zones(df, bPct, mBase, lATR):
    """Pine f_zones on one timeframe. Per candle: the 3 nearest fresh supply zones above and demand
    zones below the close as (lo, hi), plus the nearest opposite zone edges (targets).
    Internally a zone is [hi, lo, candle_of_first_touch or -1]."""
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    a = atr(df)
    sup, dem, rows = [], [], []
    for b in range(len(c)):
        # drop broken zones, remember the first touch
        sup = [z for z in sup if not c[b] > z[0]]
        for z in sup:
            if z[2] == -1 and h[b] >= z[1]:
                z[2] = b
        dem = [z for z in dem if not c[b] < z[1]]
        for z in dem:
            if z[2] == -1 and l[b] <= z[0]:
                z[2] = b
        # new zone: 1-3 small-body base candles, then a strong leg-out candle
        if not math.isnan(a[b]) and abs(c[b] - o[b]) >= lATR * a[b]:
            k, bh, bl = 0, math.nan, math.nan
            for i in range(1, mBase + 1):
                j = b - i
                if j < 0:
                    break
                r = h[j] - l[j]
                if r > 0 and abs(c[j] - o[j]) <= bPct * r:
                    k += 1
                    bh = h[j] if math.isnan(bh) else max(bh, h[j])
                    bl = l[j] if math.isnan(bl) else min(bl, l[j])
                else:
                    break
            j = b - k - 1
            leg_in = k > 0 and j >= 0 and abs(c[j] - o[j]) > bPct * (h[j] - l[j])
            if leg_in and c[b] < o[b] and c[b] < bl:
                sup.append([bh, bl, -1])
            if leg_in and c[b] > o[b] and c[b] > bh:
                dem.append([bh, bl, -1])
        sup, dem = sup[-60:], dem[-60:]
        fresh = lambda z: z[2] == -1 or b - z[2] <= 1
        s, floor = [], -1e10
        for _ in range(3):
            best = None
            for z in sup:
                if z[0] > c[b] and fresh(z) and z[1] > floor and (best is None or z[1] < best[1]):
                    best = z
            if best is None:
                break
            s.append((best[1], best[0]))
            floor = best[1]
        d, ceil = [], 1e10
        for _ in range(3):
            best = None
            for z in dem:
                if z[1] < c[b] and fresh(z) and z[0] < ceil and (best is None or z[0] > best[0]):
                    best = z
            if best is None:
                break
            d.append((best[1], best[0]))
            ceil = best[0]
        tgtS = min((z[1] for z in sup if z[1] > c[b]), default=math.nan)
        tgtD = max((z[0] for z in dem if z[0] < c[b]), default=math.nan)
        rows.append((s, d, tgtS, tgtD))
    return rows


def resample(df, rule):
    kw = {} if rule.endswith("D") else dict(origin="epoch")  # daily candles = UTC days
    return df.resample(rule, label="left", closed="left", **kw).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()


def align(htf_index, period, m15_close):
    """For each M15 close time: index of the last higher-timeframe candle already closed (-1 = none)."""
    return np.searchsorted((htf_index + period).asi8, m15_close.asi8, side="right") - 1


def time_ok(t, p=P):
    """Pine time rules on the candle close time t (MYT)."""
    hhmm, mins, dow = t.hour * 100 + t.minute, t.hour * 60 + t.minute, t.weekday()
    bf, bt = p["breakFrom"], p["breakTo"]
    in_break = (bf <= hhmm < bt) if bf <= bt else (hhmm >= bf or hhmm < bt)
    gap = lambda n: abs(mins - (n // 100 * 60 + n % 100))
    news_ok = all(min(gap(n), 1440 - gap(n)) > p["newsMin"] for n in p["newsTimes"])
    return not in_break and news_ok and dow < 5 and not (dow == 4 and hhmm >= p["fridayCut"])


# ---------- replay ----------
@dataclass
class Event:
    time: pd.Timestamp  # M15 candle close (UTC)
    kind: str           # SETUP, MOVED, CANCEL, FILL, TP1, TP2, BE, SL, TIME
    text: str
    setup: object = None  # the Setup, for SETUP / MOVED events


@dataclass
class Setup:
    dir: int
    e1: float
    zo: float
    sl: float
    px: list            # entry price of orders 1, 2, 3 (3 = runner)
    tp1: float
    tp2: float
    qty: list           # size per order in R units: each order risks 1/3 R
    created: pd.Timestamp
    filled: list = field(default_factory=lambda: [None] * 3)  # fill price (chart), None = pending
    closed: list = field(default_factory=lambda: [False] * 3)
    exits: list = field(default_factory=list)                 # (order, "tp" | "stop")
    first_fill: pd.Timestamp = None
    tp1_done: bool = False
    rest_cancelled: bool = False
    bars_in: int = 0
    gross: float = 0.0
    max_qty: float = 0.0

    def open_orders(self):
        return [k for k in range(3) if self.filled[k] is not None and not self.closed[k]]


def replay(m15, h1, spr, tp_off_fixed=None, digits=5, p=P):
    """Replay v6 over closed M15 candles (UTC index, open/high/low/close).
    Returns (events, trades, current setup or None)."""
    h4, d1 = resample(h1, "4h"), resample(h1, "1D")
    zH4 = zones(h4, p["basePct"], p["maxBase"], p["legATR"])
    zH1 = zones(h1, p["basePct"], p["maxBase"], p["legATR"])
    sD1, sH4 = structure(d1, p["swD1"]), structure(h4, 3)
    tclose = m15.index + M15
    iH4 = align(h4.index, pd.Timedelta(hours=4), tclose)
    iH1 = align(h1.index, pd.Timedelta(hours=1), tclose)
    iD1 = align(d1.index, pd.Timedelta(days=1), tclose)
    # Frozen levels need a steady ATR: in a quiet hour the M15 ATR is tiny and the SL buffer would be too
    # tight once the market wakes up, so use the larger of the current ATR and its 24h average
    a15 = atr(m15)
    a15 = np.fmax(a15, pd.Series(a15).rolling(96, min_periods=1).mean().to_numpy())
    O, H, L, C = (m15[k].to_numpy() for k in ("open", "high", "low", "close"))
    myt = tclose.tz_convert(MYT)
    fmt = lambda x: f"{x:.{digits}f}"
    # buy entries and sell exits fill on the ask: on the bid chart they sit one spread lower
    ask = lambda px, d, entry: px - spr if (d == -1 and not entry) or (d == 1 and entry) else px

    events, trades, st = [], [], None
    for i in range(len(C)):
        T = tclose[i]

        # 1) pending orders, TP and SL fill along the candle path (TradingView: O-H-L-C if the high is nearer the open)
        if st is not None:
            path = [O[i], H[i], L[i], C[i]] if H[i] - O[i] <= O[i] - L[i] else [O[i], L[i], H[i], C[i]]
            filled_now = []
            for start, end in zip(path[:-1], path[1:]):
                pos, up = start, end >= start
                while True:
                    hit = None  # (distance, kind, order, price)
                    for k in range(3):
                        if st.filled[k] is None and not st.rest_cancelled:
                            checks = [("fill", ask(st.px[k], st.dir, True), st.dir == -1)]  # sell limit triggers going up
                        elif k in st.open_orders():
                            tp = st.tp2 if k == 2 else st.tp1
                            stop = st.px[2] if (k == 2 and st.tp1_done) else st.sl
                            checks = [("tp", ask(tp, st.dir, False), st.dir == 1),
                                      ("stop", ask(stop, st.dir, False), st.dir == -1)]
                        else:
                            continue
                        for kind, lvl, going_up in checks:
                            if (going_up and pos >= lvl) or (not going_up and pos <= lvl):
                                px = pos  # already through the level: fills at the current price
                            elif (going_up and up and lvl <= end) or (not going_up and not up and lvl >= end):
                                px = lvl
                            else:
                                continue
                            if hit is None or abs(px - pos) < hit[0]:
                                hit = (abs(px - pos), kind, k, px)
                    if hit is None:
                        break
                    _, kind, k, px = hit
                    if kind == "fill":
                        st.filled[k] = px
                        st.first_fill = st.first_fill or T
                        filled_now.append(k)
                        st.max_qty = max(st.max_qty, sum(st.qty[j] for j in st.open_orders()))
                    else:
                        st.closed[k] = True
                        st.gross += st.dir * st.qty[k] * (px - st.filled[k])
                        st.exits.append((k, kind))
                    pos = px
            if filled_now:
                events.append(Event(T, "FILL", "order " + " + ".join(str(k + 1) for k in sorted(filled_now)) + " filled"))

        # trade finished on this candle: every filled order is closed
        if st is not None and st.first_fill is not None and not st.open_orders():
            net = st.gross - spr * st.max_qty
            if st.tp1_done and st.filled[2] is not None:
                how = "TP2" if st.exits[-1] == (2, "tp") else "BE"
            else:
                how = "TP1" if any(e[1] == "tp" for e in st.exits) else "SL"
            msg = {"TP1": "TP1 hit, trade finished", "TP2": "runner hit TP2, trade finished",
                   "BE": "runner stopped at break-even, trade finished", "SL": "STOP LOSS hit, trade finished"}[how]
            pending = " Delete the pending orders." if how in ("TP1", "SL") and any(f is None for f in st.filled) else ""
            events.append(Event(T, how, f"{msg} {net:+.2f}R.{pending}"))
            trades.append(dict(setup=st.created, entry=st.first_fill, exit=T, dir=st.dir, how=how, R=round(net, 3)))
            st = None

        # 2) strategy logic at the candle close
        a = a15[i]
        if math.isnan(a) or min(iH4[i], iH1[i], iD1[i]) < 0:
            continue
        if st is not None and st.open_orders():
            st.bars_in += 1
            if st.tp1_done:
                continue
            if (L[i] <= st.tp1 - spr) if st.dir == -1 else (H[i] >= st.tp1):
                st.tp1_done, st.rest_cancelled = True, True
                if 2 in st.open_orders():
                    events.append(Event(T, "TP1", f"TP1 hit: orders 1+2 closed. Move order 3 SL to {fmt(st.px[2])} "
                                                  f"(break-even). Order 3 TP stays {fmt(st.tp2)}."))
            elif p["timeStopH"] > 0 and st.bars_in >= p["timeStopH"] * 4:
                for k in st.open_orders():
                    st.closed[k] = True
                    st.gross += st.dir * st.qty[k] * (C[i] - st.filled[k])
                net = st.gross - spr * st.max_qty
                events.append(Event(T, "TIME", f"12h without TP1: CLOSE ALL orders now and delete pending ones ({net:+.2f}R)."))
                trades.append(dict(setup=st.created, entry=st.first_fill, exit=T, dir=st.dir, how="TIME", R=round(net, 3)))
                st = None
            else:
                # TP1 follows the average entry: avg + dir * tp1R * |SL - avg|
                ks = [k for k in range(3) if st.filled[k] is not None]
                avg = sum(st.qty[k] * (st.filled[k] + (spr if st.dir == 1 else 0)) for k in ks) / sum(st.qty[k] for k in ks)
                new_tp1 = avg + st.dir * p["tp1R"] * abs(st.sl - avg)
                if abs(new_tp1 - st.tp1) > 1e-9 and events and events[-1].kind == "FILL" and events[-1].time == T and len(ks) > 1:
                    events[-1].text += f". Change TP of orders 1+2 to {fmt(new_tp1)}"
                st.tp1 = new_tp1
                st.tp2 = min(st.tp2, st.tp1) if st.dir == -1 else max(st.tp2, st.tp1)
            continue

        # flat (maybe with pending orders): nearest zone that passes the checks
        bias, h4s = sD1[iD1[i]], sH4[iH4[i]]
        sA, dA, tSA, tDA = zH4[iH4[i]]
        sB, dB, tSB, tDB = zH1[iH1[i]]
        tgtS = min(x for x in (tSA, tSB, math.inf) if not math.isnan(x))
        tgtD = max(x for x in (tDA, tDB, -math.inf) if not math.isnan(x))
        tp_off = max(tp_off_fixed if tp_off_fixed is not None else 0.12 * a, spr)
        buf = max(p["slBufATR"] * a, 2 * spr)
        c, valid = C[i], []
        if bias == -1 and h4s != 1:
            for lo, hi in sA + sB:
                dist = hi + buf - lo
                rr = (lo - (tgtD + tp_off)) / dist if tgtD > -math.inf else 0.0
                if c < lo and dist <= p["maxATR"] * a and spr <= p["maxSprPct"] / 100 * dist and rr >= p["minRR"]:
                    valid.append((lo, hi))
        if bias == 1 and h4s != -1:
            for lo, hi in dA + dB:
                dist = hi - (lo - buf)
                rr = ((tgtS - tp_off) - hi) / dist if tgtS < math.inf else 0.0
                if c > hi and dist <= p["maxATR"] * a and spr <= p["maxSprPct"] / 100 * dist and rr >= p["minRR"]:
                    valid.append((lo, hi))
        if not time_ok(myt[i], p):
            continue  # outside trading hours: no new setups, existing pending orders stay
        if not valid:
            if st is not None:
                events.append(Event(T, "CANCEL", "setup cancelled (zone no longer valid): DELETE the pending orders."))
                st = None
            continue
        # like Pine: always the nearest valid zone (keeping an older zone was tested and lost most of the winners)
        d = int(bias)
        sel = min(valid, key=lambda z: z[0]) if d == -1 else max(valid, key=lambda z: z[1])
        e1, zo = (sel[0], sel[1]) if d == -1 else (sel[1], sel[0])
        if st is not None and st.dir == d and st.e1 == e1 and st.zo == zo:
            continue  # same zone: keep the orders already placed
        mid = (e1 + zo) / 2
        sl = zo + buf if d == -1 else zo - buf
        tp1 = e1 + d * p["tp1R"] * abs(sl - e1)
        tpz = tgtD + tp_off if d == -1 else tgtS - tp_off
        tp2 = min(tpz, tp1) if d == -1 else max(tpz, tp1)
        px = [e1, mid, mid]
        kind = "MOVED" if st is not None else "SETUP"
        st = Setup(d, e1, zo, sl, px, tp1, tp2, [(1 / 3) / (abs(sl - x) + spr) for x in px], T)
        events.append(Event(T, kind, setup_text(st, c, fmt), st))
    return events, trades, st


def setup_text(st, price_now, fmt):
    side = "BUY" if st.dir == 1 else "SELL"
    return (f"{side} setup, zone {fmt(min(st.e1, st.zo))} - {fmt(max(st.e1, st.zo))}\n"
            f"1) {side} LIMIT {fmt(st.px[0])}\n2) {side} LIMIT {fmt(st.px[1])}\n3) {side} LIMIT {fmt(st.px[2])} (runner)\n"
            f"SL all: {fmt(st.sl)}\nTP orders 1+2: {fmt(st.tp1)}\nTP order 3: {fmt(st.tp2)}\n"
            f"Data price at setup: {fmt(price_now)}")
