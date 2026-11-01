"""Run: python test_backtest.py"""
import time

import numpy as np
import pandas as pd

import backtest as bt
import test_smc as t


def bars(rows):
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=pd.date_range("2026-01-05 22:00", periods=len(rows), freq="5min"))


SELL = {"side": "sell", "entry": 100.0, "sl": 110.0, "tp1": 90.0, "tp": 70.0}
BUY = {"side": "buy", "entry": 100.0, "sl": 90.0, "tp1": 110.0, "tp": 130.0}


def test_simulate_sell():
    f = lambda rows: bt.simulate(SELL, bars(rows), spread=0.0)
    assert f([[100, 111, 99, 105]])["outcome"] == "SL" and f([[100, 111, 99, 105]])["r"] == -1
    r = f([[100, 101, 95, 96], [96, 97, 69, 70]])                      # tp1 then tp: 0.5*1 + 0.5*3
    assert r["outcome"] == "TP" and abs(r["r"] - 2.0) < 1e-9
    r = f([[100, 101, 89, 90], [90, 101, 95, 100]])                    # tp1, then back to entry -> +0.5R
    assert r["outcome"] == "BE" and abs(r["r"] - 0.5) < 1e-9
    r = f([[100, 111, 69, 100]])                                       # stop and target in one bar -> stop first
    assert r["outcome"] == "SL"
    assert bt.simulate(SELL, bars([[100, 109.8, 99, 105]]), spread=0.3)["outcome"] == "SL"   # ask side touches the stop


def test_simulate_buy():
    f = lambda rows: bt.simulate(BUY, bars(rows), spread=0.3)
    assert f([[100, 101, 89, 95]])["outcome"] == "SL"
    r = f([[100, 111, 101, 109], [109, 131, 108, 130]])
    assert r["outcome"] == "TP" and abs(r["r"] - 2.0) < 1e-9
    r = f([[100, 111, 99, 109]])                                       # same bar reaches +1R and falls back to entry
    assert r["outcome"] == "BE" and abs(r["r"] - 0.5) < 1e-9


def synthetic_m5(days=420, seed=3):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-01-01", periods=days * 24 * 12, freq="5min")
    idx = idx[(idx.dayofweek < 5)]
    n = len(idx)
    drift = 0.02 * np.sin(np.arange(n) / 9000.0)
    c = 2500 + np.cumsum(rng.normal(drift, 0.7))
    o = np.concatenate([[c[0]], c[:-1]])
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) + abs(rng.normal(0, 0.3, n)),
                         "low": np.minimum(o, c) - abs(rng.normal(0, 0.3, n)), "close": c}, index=idx)


def test_runs_end_to_end():
    import copy
    m5 = synthetic_m5()
    strict_tr, strict_why = bt.run(m5, t.CFG, offset=3, spread=0.3, start="2026-01-01")   # strict rules: evaluates, blocks
    assert sum(strict_why.values()) + len(strict_tr) > 100

    loose = copy.deepcopy(t.CFG)                                        # loosened only to push trades through the runner
    loose["bias"]["timeframes"] = ["H4"]; loose["stop"]["max_atr"] = 8; loose["target"]["min_rr"] = 1.0
    loose["sessions_myt"] = ["00:00-23:59"]; loose["risk"]["max_trades_24h"] = 5
    t0 = time.time()
    tr, why = bt.run(m5, loose, offset=3, spread=0.3, start="2025-09-01")
    print(f"      {len(tr)} trades in {time.time() - t0:.0f}s")
    assert len(tr) > 20
    assert (tr.exit_time >= tr.entry_time).all()
    assert (tr.entry_time.iloc[1:].values >= tr.exit_time.iloc[:-1].values).all()       # one trade at a time
    assert set(tr.side) <= {"sell", "buy"} and tr.r.min() >= -1.0 - 1e-9              # no loss beyond the stop
    assert "Trades" in bt.report(tr, why, 1.0)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok  ", name)
