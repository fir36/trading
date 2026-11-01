"""Strategy logic only (no MetaTrader5 import) so it can be tested anywhere.

All sell/buy logic is written once, for sells. Buys run the same code on a
mirrored price series (prices negated, highs/lows swapped) and levels are
flipped back at the end.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

MYT = timezone(timedelta(hours=8))
TF_MINUTES = {"M5": 5, "M15": 15, "H1": 60, "H4": 240, "D1": 1440, "W1": 10080}


# ---------- primitives ----------

def mirror(df: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({"open": -df.open, "high": -df.low, "low": -df.high, "close": -df.close})


def atr(df: pd.DataFrame, n: int = 14) -> float:
    pc = df.close.shift(1)
    tr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)
    return float(tr.tail(n).mean())


def swings(df: pd.DataFrame, n: int) -> list[tuple[int, str, float]]:
    """Confirmed pivots: (index, 'H'|'L', price). A pivot needs n bars on each side."""
    hi, lo = df.high.to_numpy(), df.low.to_numpy()
    out = []
    for i in range(n, len(df) - n):
        if hi[i] > hi[i - n:i].max() and hi[i] >= hi[i + 1:i + n + 1].max():
            out.append((i, "H", float(hi[i])))
        if lo[i] < lo[i - n:i].min() and lo[i] <= lo[i + 1:i + n + 1].min():
            out.append((i, "L", float(lo[i])))
    return out


def structure(df: pd.DataFrame, n: int) -> str:
    """bearish = lower high + lower low, bullish = higher high + higher low, else range."""
    sw = swings(df, n)
    h = [p for _, t, p in sw if t == "H"][-2:]
    l = [p for _, t, p in sw if t == "L"][-2:]
    if len(h) < 2 or len(l) < 2:
        return "unknown"
    if h[1] < h[0] and l[1] < l[0]:
        return "bearish"
    if h[1] > h[0] and l[1] > l[0]:
        return "bullish"
    return "range"


def supply_zones(df: pd.DataFrame, n: int, price: float, a: float, zc: dict) -> list[dict]:
    """Unbroken swing highs above price. Zone = candle high down to body top (clamped by ATR)."""
    min_t, max_t = zc["min_thickness_atr"] * a, zc["max_thickness_atr"] * a
    out = []
    for idx, kind, high in swings(df, n):
        if kind != "H":
            continue
        low = min(max(df.open.iat[idx], df.close.iat[idx]), high - min_t)
        low = max(low, high - max_t)
        after = df.iloc[idx + 1:]
        if (after.close > high).any() or high <= price:
            continue  # price accepted above it, or it is below price
        out.append({"low": float(low), "high": float(high), "tested": int((after.high >= low).sum())})
    out.sort(key=lambda z: z["low"] - price)
    return out


def confirm_sell(df: pd.DataFrame, zone: dict, lookback: int, max_age: int, n_swing: int) -> dict | None:
    """None if price has not reached the zone (or closed above it). Else which confirmations fired."""
    w = df.tail(lookback).reset_index(drop=True)
    hi, op, cl = w.high.to_numpy(), w.open.to_numpy(), w.close.to_numpy()
    hit = np.flatnonzero(hi >= zone["low"])
    if hit.size == 0:
        return None
    t0 = int(hit[0])
    if (cl[t0:] > zone["high"]).any():
        return None
    last = len(w) - 1
    first_fresh = last - max_age + 1
    peak_pos = t0 + int(np.argmax(hi[t0:]))

    engulf = any(
        k >= t0 and cl[k - 1] > op[k - 1] and cl[k] < op[k] and op[k] >= cl[k - 1] and cl[k] <= op[k - 1]
        and max(hi[k - 1], hi[k]) >= zone["low"]
        for k in range(max(1, first_fresh), last + 1)
    )

    choch, level = False, None
    prior_lows = [p for i, t, p in swings(w, n_swing) if t == "L" and i < peak_pos]
    if prior_lows:
        level = prior_lows[-1]  # last higher low before the peak inside the zone
        for k in range(peak_pos + 1, last + 1):
            if cl[k] < level:
                choch = k >= first_fresh
                break
    return {"choch": choch, "engulf": engulf, "peak": float(hi[t0:].max()), "choch_level": level}


# ---------- rules ----------

def _in_window(hhmm: str, window: str) -> bool:
    s, e = window.split("-")
    return s <= hhmm <= e if s <= e else (hhmm >= s or hhmm <= e)


def time_ok(now: datetime, cfg: dict) -> tuple[bool, str]:
    wd, hhmm = now.weekday(), now.strftime("%H:%M")
    if wd >= 5 or (wd == 0 and hhmm < "06:00"):
        return False, "market closed (weekend)"
    cutoff = cfg.get("friday_cutoff_myt")
    if wd == 4 and cutoff and hhmm >= cutoff:
        return False, f"Friday after {cutoff} MYT"
    if not any(_in_window(hhmm, w) for w in cfg["sessions_myt"]):
        return False, f"{hhmm} MYT is outside sessions {cfg['sessions_myt']}"
    nw = cfg["news"]
    for ev in nw.get("events_myt", []):
        t = datetime.strptime(ev, "%Y-%m-%d %H:%M").replace(tzinfo=MYT)
        if t - timedelta(minutes=nw["blackout_minutes_before"]) <= now <= t + timedelta(minutes=nw["blackout_minutes_after"]):
            return False, f"news blackout around {ev} MYT"
    return True, f"{hhmm} MYT inside session"


def lot_size(equity: float, risk_pct: float, sl_dist: float, spec: dict) -> float:
    per_lot = sl_dist / spec["tick_size"] * spec["tick_value"]
    raw = equity * risk_pct / 100 / per_lot
    step = spec["volume_step"]
    lots = round(math.floor(raw / step + 1e-9) * step, 6)
    return 0.0 if lots < spec["volume_min"] else min(lots, spec["volume_max"])


def evaluate(data: dict[str, pd.DataFrame], cfg: dict, spec: dict, equity: float, tick: dict,
             now: datetime, open_positions: int = 0, trades_24h: int = 0) -> dict:
    """data: closed bars per timeframe (open/high/low/close). tick: {'bid','ask'}. Stops at the first failed rule."""
    checks: list[dict] = []
    out = {"decision": "NO_TRADE", "side": None, "reason": None, "checks": checks,
           "structure": {}, "zones": [], "setup": None}

    def ok(rule, detail):
        checks.append({"rule": rule, "ok": True, "detail": detail})

    def fail(rule, detail):
        checks.append({"rule": rule, "ok": False, "detail": detail})
        out["reason"] = f"{rule}: {detail}"
        return out

    rk, digits = cfg["risk"], spec.get("digits", 2)
    rnd = lambda x: round(float(x), digits)

    # 1. account limits, time, spread
    if open_positions >= rk["max_open_positions"]:
        return fail("limits", f"{open_positions} position(s) already open")
    if trades_24h >= rk["max_trades_24h"]:
        return fail("limits", f"{trades_24h} trades in the last 24h (max {rk['max_trades_24h']})")
    ok("limits", f"open={open_positions}, trades_24h={trades_24h}")

    good, detail = time_ok(now, cfg)
    if not good:
        return fail("time_window", detail)
    ok("time_window", detail)

    spread = (tick["ask"] - tick["bid"]) / spec["point"]
    if spread > rk["max_spread_points"]:
        return fail("spread", f"{spread:.0f} points > max {rk['max_spread_points']}")
    ok("spread", f"{spread:.0f} points")

    # 2. higher-timeframe bias (all must agree)
    out["structure"] = {tf: structure(data[tf], cfg["swing"][tf]) for tf in cfg["bias"]["timeframes"]}
    vals = set(out["structure"].values())
    if vals == {"bearish"}:
        side = "sell"
    elif vals == {"bullish"}:
        side = "buy"
    else:
        return fail("bias", f"timeframes disagree: {out['structure']}")
    out["side"] = side
    ok("bias", f"{side}-only: {out['structure']}")

    # work in sell-space; buys are mirrored. flip(x) maps sell-space price back to real price.
    flip = (lambda x: x) if side == "sell" else (lambda x: -x)
    s = {tf: (df if side == "sell" else mirror(df)) for tf, df in data.items()}
    entry_s = tick["bid"] if side == "sell" else -tick["ask"]

    # 3. zones on H4/H1
    zc, zones = cfg["zones"], []
    for tf in zc["timeframes"]:
        a = atr(s[tf], cfg["stop"]["atr_period"])
        for z in supply_zones(s[tf], cfg["swing"][tf], entry_s, a, zc)[: zc["max_per_tf"]]:
            zones.append({**z, "tf": tf})
    for z in zones:
        z["confluence"] = 1 + len({o["tf"] for o in zones if o["tf"] != z["tf"] and o["low"] <= z["high"] and z["low"] <= o["high"]})
    zones = sorted((z for z in zones if z["confluence"] >= zc["min_confluence"]),
                   key=lambda z: (-z["confluence"], z["tested"] > 0, z["low"] - entry_s))
    real = lambda z: {"tf": z["tf"], "low": rnd(min(flip(z["low"]), flip(z["high"]))),
                      "high": rnd(max(flip(z["low"]), flip(z["high"]))), "tested": z["tested"], "confluence": z["confluence"]}
    out["zones"] = [real(z) for z in zones]
    if not zones:
        return fail("zones", "no unbroken H4/H1 zone ahead of price")
    ok("zones", f"{len(zones)} candidate zone(s)")

    # 4. price at zone, 5. confirmation
    en = cfg["entry"]
    touched, hit = [], None
    for z in zones:
        for tf in en["confirm_timeframes"]:
            bars = max(10, int(en["touch_lookback_hours"] * 60 / TF_MINUTES[tf]))
            r = confirm_sell(s[tf], z, bars, en["signal_max_age_bars"], cfg["swing"][tf])
            if r is None:
                continue
            touched.append((z, tf, r))
            if (r["choch"] + r["engulf"]) >= en["min_confirmations"] and hit is None:
                hit = (z, tf, r)
    if not touched:
        nearest = min(zones, key=lambda z: abs(z["low"] - entry_s))
        return fail("at_zone", f"price not in any zone; nearest {side} zone {real(nearest)['low']}-{real(nearest)['high']}")
    ok("at_zone", f"{len(touched)} zone/timeframe combination(s) touched")
    if hit is None:
        got = [f"{real(z)['tf']} {tf}: CHoCH={r['choch']} engulf={r['engulf']}" for z, tf, r in touched]
        return fail("confirmation", f"need {en['min_confirmations']} of CHoCH/engulfing, got {got}")
    zone, ctf, conf = hit
    names = [n for n, v in (("CHoCH", conf["choch"]), ("engulfing", conf["engulf"])) if v]
    ok("confirmation", f"{' + '.join(names)} on {ctf}")

    # 6. stop: beyond the peak since the zone touch (mode peak) or beyond the zone itself (mode zone)
    st = cfg["stop"]
    a = atr(s[st["atr_timeframe"]], st["atr_period"])
    beyond = zone["high"] if st.get("mode", "peak") == "zone" else conf["peak"]
    sl_s = max(max(beyond, entry_s) + st["buffer_atr"] * a, entry_s + st["min_atr"] * a)
    dist = sl_s - entry_s
    if dist > st["max_atr"] * a:
        return fail("stop", f"stop {dist:.2f} = {dist / a:.1f} ATR, wider than {st['max_atr']} ATR")
    ok("stop", f"{dist:.2f} ({dist / a:.1f} ATR)")

    # 7. reward:risk to the nearest H1 swing low (mode h1_swing) or just short of the next opposite zone (mode next_zone)
    tg = cfg["target"]
    if tg.get("mode", "h1_swing") == "next_zone":
        tops = []  # top of each unbroken demand zone (sell-space); found as supply zones of the mirrored series
        for tf in zc["timeframes"]:
            m = mirror(s[tf])
            tops += [-z["low"] for z in supply_zones(m, cfg["swing"][tf], -entry_s, atr(m, st["atr_period"]), zc)]
        tops = [t for t in tops if t < entry_s]
        if not tops:
            return fail("reward_risk", f"no H4/H1 {'demand' if side == 'sell' else 'supply'} zone beyond entry")
        tp_s = max(tops) + tg.get("offset", 0.0)
    else:
        lows = [p for _, t, p in swings(s["H1"], cfg["swing"]["H1"]) if t == "L" and p < entry_s]
        if not lows:
            return fail("reward_risk", "no H1 swing target below entry")
        tp_s = max(lows)
    rr = (entry_s - tp_s) / dist
    if rr < cfg["target"]["min_rr"]:
        return fail("reward_risk", f"nearest target gives {rr:.2f}R, need {cfg['target']['min_rr']}R")
    ok("reward_risk", f"{rr:.2f}R")

    # 8. position size
    risk_pct = min(rk["per_trade_pct"], 2.0)
    lots = lot_size(equity, risk_pct, dist, spec)
    if lots <= 0:
        return fail("position_size", f"minimum lot {spec['volume_min']} would risk more than {risk_pct}%")
    ok("position_size", f"{lots} lots risks {risk_pct}%")

    out["decision"] = "TRADE"
    out["setup"] = {
        "side": side, "entry": rnd(flip(entry_s)), "sl": rnd(flip(sl_s)), "tp": rnd(flip(tp_s)),
        "tp1": rnd(flip(entry_s - cfg["target"]["partial_at_r"] * dist)),
        "lots": lots, "risk_pct": risk_pct, "risk_money": round(equity * risk_pct / 100, 2),
        "rr": round(rr, 2), "sl_distance": rnd(dist), "atr": rnd(a),
        "zone": real(zone), "confirm_tf": ctf, "confirmations": names,
    }
    return out
