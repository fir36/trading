"""Run: python test_smc.py   (or pytest). Uses synthetic data, no MT5 needed."""
import copy
from datetime import datetime

import numpy as np
import pandas as pd
import yaml

import smc

CFG = yaml.safe_load(open("strategy.yaml"))
SPEC = dict(point=0.01, digits=2, tick_size=0.01, tick_value=1.0, volume_min=0.01, volume_max=100.0, volume_step=0.01)
WED_22H = datetime(2026, 11, 4, 22, 30, tzinfo=smc.MYT)


def zigzag(points, bars_per_leg, wick=1.5):
    closes = [points[0]]
    for a, b in zip(points, points[1:]):
        closes += list(np.linspace(a, b, bars_per_leg + 1)[1:])
    c = np.array(closes)
    o = np.concatenate([[c[0]], c[:-1]])
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) + wick, "low": np.minimum(o, c) - wick, "close": c})


def sell_data(m15_points=None):
    down = [3600, 3300, 3500, 3200, 3400, 3100, 3300, 3000]          # lower highs, lower lows
    m15 = m15_points or [3070, 3150, 3205, 3196, 3232, 3190]          # rally into zone, HL 3196, peak 3232, break
    return {
        "W1": zigzag(down, 4), "D1": zigzag(down, 5), "H4": zigzag(down, 8),
        "H1": zigzag([3400, 3300, 3360, 3240, 3300, 3070, 3232, 3060, 3226], 6),
        "M15": zigzag(m15, 3, wick=1.0),
        "M5": zigzag([3100, 3120, 3105, 3130, 3110], 8),               # never reaches the zone
    }


def mirrored(data, k=7000.0):
    return {tf: pd.DataFrame({"open": k - d.open, "high": k - d.low, "low": k - d.high, "close": k - d.close})
            for tf, d in data.items()}


def run(data, cfg=CFG, equity=10000.0, now=WED_22H, spread=0.30, **kw):
    last = float(data["M15"].close.iloc[-1])
    return smc.evaluate(data, cfg, SPEC, equity, {"bid": last, "ask": last + spread}, now, **kw)


def failed(res):
    return res["checks"][-1]["rule"] if res["decision"] == "NO_TRADE" else None


# ---------- primitives ----------
def test_structure():
    assert smc.structure(zigzag([3600, 3300, 3500, 3200, 3400, 3100, 3300, 3000], 4), 2) == "bearish"
    assert smc.structure(zigzag([3000, 3300, 3100, 3400, 3200, 3500, 3300, 3600], 4), 2) == "bullish"
    assert smc.structure(zigzag([3000, 3300, 3100, 3400, 3050, 3500, 3000, 3450], 4), 2) == "range"  # expanding


def test_lot_size():
    assert smc.lot_size(10000, 1, 44.0, SPEC) == 0.02            # $100 risk / $4400 per lot -> 0.0227 -> 0.02
    assert smc.lot_size(100, 1, 44.0, SPEC) == 0.0               # below minimum lot -> refuse
    assert smc.lot_size(10_000_000, 1, 1.0, SPEC) == 100.0       # clamped to max volume


def test_time_rules():
    f = lambda d, h, m: smc.time_ok(datetime(2026, 11, d, h, m, tzinfo=smc.MYT), CFG)[0]
    assert f(4, 22, 30) and f(4, 0, 30) is True                  # Wed 22:30 and Wed 00:30 inside 21:00-01:00
    assert not f(4, 14, 0)                                       # outside session
    assert not f(7, 12, 0) and not f(8, 12, 0)                   # Sat / Sun
    assert not f(6, 22, 30)                                      # Fri after cutoff
    cfg = copy.deepcopy(CFG); cfg["news"]["events_myt"] = ["2026-11-04 22:45"]
    assert not smc.time_ok(WED_22H, cfg)[0]                      # 15 min before news
    assert smc.time_ok(datetime(2026, 11, 4, 23, 30, tzinfo=smc.MYT), cfg)[0]


# ---------- full rule engine ----------
def test_sell_trade():
    r = run(sell_data())
    assert r["decision"] == "TRADE", r["reason"]
    s = r["setup"]
    assert r["side"] == "sell" and s["sl"] > s["entry"] > s["tp1"] > s["tp"]
    assert s["rr"] >= 2.0 and s["lots"] == 0.02 and "CHoCH" in s["confirmations"]
    assert s["zone"]["low"] <= 3233.5 and s["sl"] > s["zone"]["high"]       # stop sits above the zone


def test_buy_is_exact_mirror():
    sell, buy = run(sell_data(), spread=0), run(mirrored(sell_data()), spread=0)
    assert buy["decision"] == "TRADE", buy["reason"]
    b, s = buy["setup"], sell["setup"]
    assert buy["side"] == "buy" and b["sl"] < b["entry"] < b["tp1"] < b["tp"]
    assert (b["rr"], b["lots"], b["sl_distance"]) == (s["rr"], s["lots"], s["sl_distance"])
    # with a real spread the buy enters at the ask, so its stop is wider by exactly the spread
    wide = run(mirrored(sell_data()), spread=0.30)["setup"]
    assert abs(wide["sl_distance"] - s["sl_distance"] - 0.30) < 0.011


def test_each_rule_blocks():
    d = sell_data()
    assert failed(run(d, open_positions=1)) == "limits"
    assert failed(run(d, trades_24h=2)) == "limits"
    assert failed(run(d, now=datetime(2026, 11, 4, 14, 0, tzinfo=smc.MYT))) == "time_window"
    assert failed(run(d, spread=1.5)) == "spread"

    mixed = dict(d, D1=zigzag([3000, 3300, 3100, 3400, 3050, 3500, 3000, 3450], 5))
    assert failed(run(mixed)) == "bias"

    far = sell_data([3070, 3100, 3090, 3120, 3110, 3100])                   # never reaches the zone
    assert failed(run(far)) == "at_zone"

    no_signal = sell_data([3070, 3150, 3205, 3196, 3232])                   # at the zone, still rising
    assert failed(run(no_signal)) == "confirmation"

    stale = sell_data([3070, 3150, 3205, 3196, 3232, 3190, 3190.5, 3190, 3190.5])  # CHoCH older than 2 bars
    assert failed(run(stale)) == "confirmation"

    tight = copy.deepcopy(CFG); tight["stop"]["max_atr"] = 1.0
    assert failed(run(d, cfg=tight)) == "stop"

    greedy = copy.deepcopy(CFG); greedy["target"]["min_rr"] = 10
    assert failed(run(d, cfg=greedy)) == "reward_risk"

    assert failed(run(d, equity=100.0)) == "position_size"


def test_risk_is_hard_capped():
    cfg = copy.deepcopy(CFG); cfg["risk"]["per_trade_pct"] = 50
    assert run(sell_data(), cfg=cfg)["setup"]["risk_pct"] == 2.0


# ---------- v2 rules (strategy_v2.yaml): stop beyond the zone, target before the next opposite zone ----------
CFG2 = yaml.safe_load(open("strategy_v2.yaml"))


def test_v2_zone_stop_and_next_zone_target():
    r = run(sell_data(), cfg=CFG2)
    assert r["decision"] == "TRADE", r["reason"]
    s = r["setup"]
    assert abs(s["sl"] - (s["zone"]["high"] + 0.5 * s["atr"])) < 0.02          # zone edge + 0.5 ATR
    no_off = copy.deepcopy(CFG2); no_off["target"]["offset"] = 0.0
    tp0 = run(sell_data(), cfg=no_off)["setup"]["tp"]
    assert 3058.5 <= tp0 <= 3100                                                # top of the H1 demand zone at the 3058.5 low
    assert abs(s["tp"] - (tp0 + 1.0)) < 1e-9                                    # sell TP sits 1.0 above that zone


def test_v2_buy_is_exact_mirror():
    sell, buy = run(sell_data(), cfg=CFG2, spread=0), run(mirrored(sell_data()), cfg=CFG2, spread=0)
    assert buy["decision"] == "TRADE", buy["reason"]
    b, s = buy["setup"], sell["setup"]
    assert b["sl"] < b["entry"] < b["tp1"] < b["tp"]
    assert (b["rr"], b["lots"], b["sl_distance"]) == (s["rr"], s["lots"], s["sl_distance"])


def test_v2_blocks():
    greedy = copy.deepcopy(CFG2); greedy["target"]["min_rr"] = 10
    assert failed(run(sell_data(), cfg=greedy)) == "reward_risk"
    mixed = dict(sell_data(), D1=zigzag([3000, 3300, 3100, 3400, 3050, 3500, 3000, 3450], 5))
    assert failed(run(mixed, cfg=CFG2)) == "bias"                               # D1 alone decides the bias


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok  ", name)
