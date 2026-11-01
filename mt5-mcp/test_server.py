"""Server wiring test against a fake MetaTrader5 module. Run: python test_server.py"""
import asyncio
import copy
import sys
from datetime import datetime
from types import SimpleNamespace as NS

import test_smc as t

DEMO, REAL, DONE = 0, 2, 10009
sent = []                                   # every order_send request lands here
state = {}


def make_fake():
    data, cfg = t.sell_data(), None
    last = float(data["M15"].close.iloc[-1])
    state.update(data=data, bid=last, ask=last + 0.30, mode=DEMO, positions=[])

    def rates(sym, tf, pos, count):
        import pandas as pd
        df = state["data"][tf]
        pad = [df.head(1)] * max(0, 30 - len(df))                        # flat history so short synthetic series pass the length guard
        full = pd.concat(pad + [df, df.tail(1)])                         # last row = the still-forming bar the server drops
        return full.assign(time=0).iloc[-count:].to_records(index=False)

    m = NS(
        TIMEFRAME_M5="M5", TIMEFRAME_M15="M15", TIMEFRAME_H1="H1", TIMEFRAME_H4="H4", TIMEFRAME_D1="D1", TIMEFRAME_W1="W1",
        ACCOUNT_TRADE_MODE_DEMO=DEMO, DEAL_ENTRY_IN=0, POSITION_TYPE_BUY=0, POSITION_TYPE_SELL=1,
        TRADE_ACTION_DEAL=1, TRADE_ACTION_SLTP=6, ORDER_TYPE_BUY=0, ORDER_TYPE_SELL=1, ORDER_TIME_GTC=0,
        ORDER_FILLING_FOK=0, ORDER_FILLING_IOC=1, ORDER_FILLING_RETURN=2, TRADE_RETCODE_DONE=DONE,
        terminal_info=lambda: object(), symbol_select=lambda s, v: True, last_error=lambda: (0, "ok"),
        symbol_info=lambda s: NS(point=0.01, digits=2, trade_tick_size=0.01, trade_tick_value=1.0, volume_min=0.01,
                                 volume_max=100.0, volume_step=0.01, filling_mode=2),
        symbol_info_tick=lambda s: NS(bid=state["bid"], ask=state["ask"], time=1_000_000),
        account_info=lambda: NS(equity=10000.0, balance=10000.0, trade_mode=state["mode"]),
        copy_rates_from_pos=rates, positions_get=lambda symbol=None: state["positions"], history_deals_get=lambda a, b: [],
        order_check=lambda r: NS(retcode=0, comment="ok"),
        order_send=lambda r: (sent.append(r), NS(retcode=DONE, order=1, deal=1, price=r.get("price"), volume=r.get("volume")))[1],
    )
    return m


sys.modules["MetaTrader5"] = make_fake()
import server  # noqa: E402

LIVE = copy.deepcopy(t.CFG); LIVE["execution"]["dry_run"] = False
DRY = copy.deepcopy(t.CFG)
server._now = lambda: t.WED_22H


def use(cfg):
    server._cfg = lambda: cfg
    sent.clear()


def test_tools_registered():
    names = {x.name for x in asyncio.run(server.mcp.list_tools())}
    assert {"get_rules", "account_status", "check_setup", "place_trade", "manage_positions"} <= names, names


def test_check_and_dry_run():
    use(DRY)
    assert server.check_setup()["decision"] == "TRADE"
    r = server.place_trade(confirm=True)
    assert r["order_sent"] is False and r["dry_run"] and r["would_send"]["lots"] == 0.02 and not sent


def test_live_needs_confirm_then_sends_computed_values_only():
    use(LIVE)
    assert server.place_trade()["needs_confirm"] and not sent
    r = server.place_trade(confirm=True)
    assert r["order_sent"] and r["result"]["ok"] and len(sent) == 1
    req = sent[0]
    assert req["type"] == 1 and req["volume"] == 0.02 and req["price"] == state["bid"]       # SELL at bid
    assert req["sl"] == r["setup"]["sl"] > req["price"] > r["setup"]["tp"] == req["tp"]
    import inspect
    assert list(inspect.signature(server.place_trade).parameters) == ["symbol", "confirm"]  # no price/sl/tp/lot inputs


def test_real_account_refused():
    use(LIVE)
    state["mode"] = REAL
    try:
        r = server.place_trade(confirm=True)
        assert r["order_sent"] is False and "real account" in r["refused"] and not sent
    finally:
        state["mode"] = DEMO


def test_no_trade_sends_nothing():
    use(LIVE)
    server._now = lambda: datetime(2026, 11, 7, 22, 30, tzinfo=t.smc.MYT)                      # Saturday
    try:
        r = server.place_trade(confirm=True)
        assert r["order_sent"] is False and r["decision"] == "NO_TRADE" and not sent
    finally:
        server._now = lambda: t.WED_22H


def pos(volume, ptype=1, open_=3190.0, sl=3237.0):
    return NS(magic=26102901, sl=sl, tp=3068.5, price_open=open_, volume=volume, type=ptype, ticket=77, profit=0.0)


def test_manage_positions_partial_and_breakeven():
    use(LIVE)
    state["positions"] = [pos(0.04)]
    state["ask"] = 3140.0                                       # +50 on a 47 risk -> past 1R
    r = server.manage_positions()
    assert r["actions"][0]["close_volume"] == 0.02 and r["actions"][0]["sl_moved"]
    close, sltp = sent
    assert close["type"] == 0 and close["volume"] == 0.02 and close["position"] == 77      # BUY closes the sell
    assert sltp["sl"] == 3190.0 and sltp["position"] == 77

    use(LIVE)
    state["positions"] = [pos(0.01)]                           # cannot split a minimum lot -> only move the stop
    assert server.manage_positions()["actions"][0]["close_volume"] == 0 and len(sent) == 1 and "sl" in sent[0]

    use(LIVE)
    state["positions"] = [pos(0.04)]
    state["ask"] = 3170.0                                      # only +20 -> nothing happens
    assert server.manage_positions()["actions"] == [] and not sent

    use(LIVE)
    state["positions"] = [pos(0.04, sl=3190.0)]                # already at break-even
    state["ask"] = 3100.0
    assert server.manage_positions()["actions"] == [] and not sent

    use(DRY)
    state["positions"] = [pos(0.04)]
    assert server.manage_positions()["actions"] and not sent   # dry-run reports, never sends
    state["positions"], state["ask"] = [], state["bid"] + 0.30


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok  ", name)
