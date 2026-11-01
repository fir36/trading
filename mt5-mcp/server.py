"""MT5 MCP server. Rules live in strategy.yaml + smc.py and are enforced here.

The model can only ask the server to evaluate or act: place_trade takes no price, SL, TP or
lot arguments, and re-runs every rule itself before sending anything.
Orders are NOT sent unless execution.dry_run is false in strategy.yaml.
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yaml
from mcp.server.mcpserver import MCPServer

import smc

try:
    import MetaTrader5 as mt5  # Windows only
except ImportError:  # lets the module import (and tests run) elsewhere
    mt5 = None

STRATEGY_FILE = Path(os.environ.get("STRATEGY_FILE", Path(__file__).with_name("strategy.yaml")))
BARS = {"W1": 120, "D1": 250, "H4": 400, "H1": 600, "M15": 400, "M5": 400}

mcp = MCPServer(
    "mt5-smc",
    instructions=(
        "Rule-enforcing MT5 gold strategy. Call check_setup to see the decision and why. "
        "place_trade re-checks all rules itself; if it says NO_TRADE, do not look for a way around it. "
        "Call manage_positions regularly while a trade is open."
    ),
)


def _cfg() -> dict:
    return yaml.safe_load(STRATEGY_FILE.read_text())


def _now() -> datetime:
    return datetime.now(smc.MYT)


def _connect() -> None:
    if mt5 is None:
        raise RuntimeError("MetaTrader5 package not installed. On Windows: pip install MetaTrader5")
    if mt5.terminal_info() is None:
        kw = {}
        if os.environ.get("MT5_PATH"):
            kw["path"] = os.environ["MT5_PATH"]
        if os.environ.get("MT5_LOGIN"):
            kw.update(login=int(os.environ["MT5_LOGIN"]), password=os.environ["MT5_PASSWORD"], server=os.environ["MT5_SERVER"])
        if not mt5.initialize(**kw):
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")


def _symbol(name: str | None, c: dict) -> str:
    sym = name or c["symbol"]
    if not mt5.symbol_select(sym, True):
        raise RuntimeError(f"Symbol {sym!r} not found. Set 'symbol' in strategy.yaml to your broker's name.")
    return sym


def _bars(sym: str, tf: str) -> pd.DataFrame:
    frames = {"M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15, "H1": mt5.TIMEFRAME_H1,
              "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1, "W1": mt5.TIMEFRAME_W1}
    arr = mt5.copy_rates_from_pos(sym, frames[tf], 0, BARS[tf] + 1)
    if arr is None or len(arr) < 20:
        raise RuntimeError(f"Not enough {tf} history for {sym}: {mt5.last_error()}")
    return pd.DataFrame(arr)[["open", "high", "low", "close"]].iloc[:-1].reset_index(drop=True)  # drop the forming bar


def _filling(info) -> int:
    if info.filling_mode & 1:
        return mt5.ORDER_FILLING_FOK
    if info.filling_mode & 2:
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


def _context(symbol: str | None) -> dict:
    c = _cfg()
    _connect()
    sym = _symbol(symbol, c)
    info, tick, acct = mt5.symbol_info(sym), mt5.symbol_info_tick(sym), mt5.account_info()
    if info is None or tick is None or acct is None:
        raise RuntimeError(f"MT5 returned no data: {mt5.last_error()}")
    tfs = (set(c["bias"]["timeframes"]) | set(c["zones"]["timeframes"]) | set(c["entry"]["confirm_timeframes"])
           | {c["stop"]["atr_timeframe"], "H1"})
    spec = dict(point=info.point, digits=info.digits, tick_size=info.trade_tick_size, tick_value=info.trade_tick_value,
                volume_min=info.volume_min, volume_max=info.volume_max, volume_step=info.volume_step)
    positions = [p for p in (mt5.positions_get(symbol=sym) or ()) if p.magic == c["magic"]]
    utc = datetime.now(timezone.utc)
    deals = mt5.history_deals_get(utc - timedelta(hours=48), utc + timedelta(hours=48)) or ()
    trades_24h = sum(1 for d in deals if d.magic == c["magic"] and d.entry == mt5.DEAL_ENTRY_IN and d.time >= tick.time - 86400)
    return dict(cfg=c, sym=sym, info=info, tick=tick, acct=acct, spec=spec, positions=positions, trades_24h=trades_24h,
                data={tf: _bars(sym, tf) for tf in tfs})


def _evaluate(x: dict) -> dict:
    return smc.evaluate(x["data"], x["cfg"], x["spec"], x["acct"].equity, {"bid": x["tick"].bid, "ask": x["tick"].ask},
                        _now(), open_positions=len(x["positions"]), trades_24h=x["trades_24h"])


def _send(req: dict) -> dict:
    chk = mt5.order_check(req)
    if chk is None or chk.retcode != 0:
        return {"ok": False, "stage": "order_check", "retcode": getattr(chk, "retcode", None), "comment": getattr(chk, "comment", mt5.last_error())}
    r = mt5.order_send(req)
    if r is None or r.retcode != mt5.TRADE_RETCODE_DONE:
        return {"ok": False, "stage": "order_send", "retcode": getattr(r, "retcode", None), "comment": getattr(r, "comment", mt5.last_error())}
    return {"ok": True, "retcode": r.retcode, "order": r.order, "deal": r.deal, "price": r.price, "volume": r.volume}


# ---------- tools ----------

@mcp.tool()
def get_rules() -> dict:
    """The strategy rules this server enforces (strategy.yaml)."""
    return _cfg()


@mcp.tool()
def account_status(symbol: str | None = None) -> dict:
    """Account type, equity, open positions for this strategy, and whether orders are live or dry-run."""
    x = _context(symbol)
    demo = x["acct"].trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO
    return {"symbol": x["sym"], "demo_account": demo, "equity": x["acct"].equity, "balance": x["acct"].balance,
            "dry_run": x["cfg"]["execution"]["dry_run"], "allow_live": x["cfg"]["execution"]["allow_live"],
            "open_positions": [{"ticket": p.ticket, "type": "sell" if p.type == mt5.POSITION_TYPE_SELL else "buy",
                                "volume": p.volume, "open": p.price_open, "sl": p.sl, "tp": p.tp, "profit": p.profit}
                               for p in x["positions"]],
            "trades_24h": x["trades_24h"], "time_myt": _now().strftime("%Y-%m-%d %H:%M")}


@mcp.tool()
def check_setup(symbol: str | None = None) -> dict:
    """Run every rule now: limits, session, spread, W1/D1/H4 bias, H4/H1 zones, M15/M5 confirmation,
    stop size, reward:risk, position size. Returns decision TRADE/NO_TRADE, each rule's result and the
    computed entry/SL/TP/lots. Never places an order."""
    return _evaluate(_context(symbol))


@mcp.tool()
def place_trade(symbol: str | None = None, confirm: bool = False) -> dict:
    """Re-run all rules and, only if the decision is TRADE, open the position with the computed entry/SL/TP/lots.
    Takes no price arguments by design. In dry-run mode (default) nothing is sent. When live, confirm=true is
    required and a non-demo account is refused unless allow_live is set in strategy.yaml."""
    x = _context(symbol)
    res = _evaluate(x)
    if res["decision"] != "TRADE":
        return {"order_sent": False, **res}
    setup, ex = res["setup"], x["cfg"]["execution"]
    if ex["dry_run"]:
        return {"order_sent": False, "dry_run": True, "would_send": setup, "analysis": res}
    if not confirm:
        return {"order_sent": False, "needs_confirm": True, "setup": setup}
    if x["acct"].trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO and not ex["allow_live"]:
        return {"order_sent": False, "refused": "real account; set execution.allow_live: true in strategy.yaml to permit"}
    buy = setup["side"] == "buy"
    tick = mt5.symbol_info_tick(x["sym"])
    req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": x["sym"], "volume": setup["lots"],
           "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL, "price": tick.ask if buy else tick.bid,
           "sl": setup["sl"], "tp": setup["tp"], "deviation": ex["deviation_points"], "magic": x["cfg"]["magic"],
           "comment": "smc-mcp", "type_time": mt5.ORDER_TIME_GTC, "type_filling": _filling(x["info"])}
    return {"order_sent": True, "request": req, "result": _send(req), "setup": setup}


@mcp.tool()
def manage_positions(symbol: str | None = None) -> dict:
    """For this strategy's open positions: once price is +partial_at_r x risk in profit, close partial_fraction
    and move the stop to break-even. Call it regularly; the server has no background loop."""
    c = _cfg()
    _connect()
    sym = _symbol(symbol, c)
    info, tick = mt5.symbol_info(sym), mt5.symbol_info_tick(sym)
    t, dry, actions = c["target"], c["execution"]["dry_run"], []
    for p in mt5.positions_get(symbol=sym) or ():
        if p.magic != c["magic"] or not p.sl:
            continue
        sell = p.type == mt5.POSITION_TYPE_SELL
        if (p.sl <= p.price_open) if sell else (p.sl >= p.price_open):
            continue  # already at break-even or better
        px = tick.ask if sell else tick.bid
        if ((p.price_open - px) if sell else (px - p.price_open)) < t["partial_at_r"] * abs(p.sl - p.price_open):
            continue
        part = round(math.floor(p.volume * t["partial_fraction"] / info.volume_step + 1e-9) * info.volume_step, 6)
        can_split = part >= info.volume_min and p.volume - part >= info.volume_min
        act = {"ticket": p.ticket, "close_volume": part if can_split else 0, "new_sl": round(p.price_open, info.digits)}
        actions.append(act)
        if dry:
            continue
        if can_split:
            act["close"] = _send({"action": mt5.TRADE_ACTION_DEAL, "symbol": sym, "volume": part, "position": p.ticket,
                                  "type": mt5.ORDER_TYPE_BUY if sell else mt5.ORDER_TYPE_SELL, "price": px,
                                  "deviation": c["execution"]["deviation_points"], "magic": c["magic"],
                                  "comment": "smc-partial", "type_time": mt5.ORDER_TIME_GTC, "type_filling": _filling(info)})
            if not act["close"]["ok"]:
                continue  # keep the original stop if the partial close failed
        r = mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "position": p.ticket, "symbol": sym,
                            "sl": act["new_sl"], "tp": p.tp})
        act["sl_moved"] = r is not None and r.retcode == mt5.TRADE_RETCODE_DONE
    return {"dry_run": dry, "actions": actions}


if __name__ == "__main__":
    mcp.run()
