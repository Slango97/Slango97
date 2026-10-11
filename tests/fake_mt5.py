"""A small in-memory stand-in for the MetaTrader5 package. Install it with `install()` BEFORE importing
mt5_client. It models market orders, SL/TP changes, closes, deal history, and scripted failures."""
import sys
import time
import types

import numpy as np

NS = types.SimpleNamespace
TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_M15 = 1, 5, 15
TIMEFRAME_H1, TIMEFRAME_H4, TIMEFRAME_D1, TIMEFRAME_W1 = 16385, 16388, 16408, 32769
ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
TRADE_ACTION_DEAL, TRADE_ACTION_SLTP = 1, 6
TRADE_ACTION_PENDING, TRADE_ACTION_REMOVE = 5, 8
ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT = 2, 3
ORDER_TIME_SPECIFIED = 2
TRADE_RETCODE_PLACED = 10008
ORDER_TIME_GTC = 0
ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
TRADE_RETCODE_REQUOTE, TRADE_RETCODE_DONE, TRADE_RETCODE_DONE_PARTIAL = 10004, 10009, 10010
TRADE_RETCODE_TIMEOUT, TRADE_RETCODE_PRICE_CHANGED, TRADE_RETCODE_PRICE_OFF = 10012, 10020, 10021
TRADE_RETCODE_TOO_MANY_REQUESTS, TRADE_RETCODE_NO_CHANGES, TRADE_RETCODE_INVALID_STOPS = 10024, 10025, 10016
DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_ENTRY_INOUT, DEAL_ENTRY_OUT_BY = 0, 1, 2, 3
SYMBOL_TRADE_MODE_FULL = 4
_TF_NAME = {1: "M1", 5: "M5", 15: "M15", 16385: "H1", 16388: "H4", 16408: "D1", 32769: "W1"}


class World:
    def __init__(self):
        self.reset()

    def reset(self):
        self.bid, self.ask = 2000.00, 2000.25
        self.tick_time_offset = 0
        self.connected, self.init_ok, self.algo = True, True, True
        self.account_mode = 0                       # 0 demo, 2 real
        self.balance = self.equity = 10000.0
        self.margin = 0.0
        self.positions, self.deals, self.calls = [], [], []
        self.next_ticket = 1000
        self.script = []
        self.slip = 0.0
        self.known = {"XAUUSD"}
        self.rates = {}
        self.stops_level = 0
        self.symbol_trade_mode = 4
        self.strip_sl = False
        self.reject_modify = False
        self.tick_value, self.tick_size, self.contract_size = 1.0, 0.01, 100.0
        self.profit_scale = 1.0                   # broker-side calc multiplier (inject a discrepancy)
        self.calc_profit_ok = True
        self.account_currency, self.profit_currency = "USD", "USD"
        self.fx = {}                              # e.g. {"USDEUR": 0.92}
        self.pending_orders = []
        self.frozen_tick_time = None              # quiet market: last tick time stops advancing

    def tick_for_test(self, bid, ask=None):
        self.bid, self.ask = bid, ask if ask is not None else bid + 0.25
        for p in self.positions:
            p.price_current = self.bid if p.type == 0 else self.ask


world = World()


def initialize(**kw):
    if world.init_ok:
        world.connected = True
    return world.init_ok


def shutdown():
    return True


def last_error():
    return (0, "ok")


def terminal_info():
    return NS(connected=world.connected, trade_allowed=world.algo)


def account_info():
    return NS(login=123, trade_mode=world.account_mode, balance=world.balance, equity=world.equity,
              currency=world.account_currency, leverage=500,
              margin=world.margin, margin_free=world.equity - world.margin,
              margin_level=(100 * world.equity / world.margin) if world.margin else 0.0,
              trade_allowed=True, trade_expert=True, margin_mode=2)




def symbols_get(group=None):
    """Return a small broker inventory including a suffix-style gold symbol."""
    return (
        NS(name="EURUSD", currency_base="EUR", currency_profit="USD", trade_mode=4, path="Forex", description="Euro vs US Dollar",
            trade_tick_size=0.00001, trade_tick_value=1.0, volume_min=0.01, volume_step=0.01),
        NS(name="XAUUSDs", currency_base="XAU", currency_profit="USD", trade_mode=4, path="Metals", description="Gold vs US Dollar",
            trade_tick_size=0.01, trade_tick_value=0.1, volume_min=0.01, volume_step=0.01),
    )

def symbol_info(name):
    if name in world.fx:
        return NS(name=name)
    if name not in world.known:
        return None
    return NS(name=name, point=0.01, digits=2, trade_tick_size=world.tick_size, trade_tick_value=world.tick_value,
              trade_tick_value_profit=world.tick_value, trade_tick_value_loss=world.tick_value, trade_calc_mode=2,
              currency_base="XAU" if name.startswith("XAU") else "", currency_profit=world.profit_currency,
              trade_contract_size=world.contract_size, volume_min=0.01, volume_max=50.0, volume_step=0.01,
              trade_stops_level=world.stops_level, trade_freeze_level=0, filling_mode=3,
              trade_mode=world.symbol_trade_mode, path="Metals", description="Gold vs US Dollar")


def symbol_select(name, enable=True):
    return name in world.known


def symbol_info_tick(name):
    if not world.connected:
        return None
    if name in world.fx:
        return NS(bid=world.fx[name], ask=world.fx[name], time=int(time.time()))
    t = world.frozen_tick_time if world.frozen_tick_time is not None else int(time.time()) + world.tick_time_offset
    return NS(bid=world.bid, ask=world.ask, time=t)


def order_calc_profit(typ, symbol, vol, p_open, p_close):
    if not world.calc_profit_ok:
        return None
    fx = 1.0
    if world.profit_currency != world.account_currency:
        fx = world.fx.get(world.profit_currency + world.account_currency) or 1 / world.fx[world.account_currency + world.profit_currency]
    return (p_close - p_open) * (1 if typ == 0 else -1) * vol * world.contract_size * world.profit_scale * fx


def orders_get(symbol=None):
    return tuple(world.pending_orders)


def order_calc_margin(typ, symbol, vol, price):
    return vol * 100.0 * price / 200.0


def positions_get(symbol=None, ticket=None, group=None):
    out = [p for p in world.positions if (symbol is None or p.symbol == symbol) and (ticket is None or p.ticket == ticket)]
    return tuple(out) if out else ()


def history_deals_get(*args, ticket=None, position=None, **kw):
    out = [d for d in world.deals if (ticket is None or d.ticket == ticket) and (position is None or d.position_id == position)]
    return tuple(out) if out else ()


def _new_ticket():
    world.next_ticket += 1
    return world.next_ticket


def _open(req):
    d = 1 if req["type"] == ORDER_TYPE_BUY else -1
    price = req["price"] + d * world.slip
    t = _new_ticket()
    pos = NS(ticket=t, time=int(time.time()), type=req["type"], magic=req["magic"], volume=req["volume"],
             price_open=price, sl=0.0 if world.strip_sl else req.get("sl", 0.0), tp=req.get("tp", 0.0), price_current=price,
             symbol=req["symbol"], comment=req.get("comment", ""), identifier=t)
    world.positions.append(pos)
    deal = NS(ticket=_new_ticket(), order=t, position_id=t, time=pos.time, entry=DEAL_ENTRY_IN, type=req["type"],
              volume=req["volume"], price=price, commission=0.0, swap=0.0, profit=0.0, fee=0.0, reason=3)
    world.deals.append(deal)
    return NS(retcode=TRADE_RETCODE_DONE, order=t, deal=deal.ticket, volume=req["volume"], price=price, comment="")


def _close(req):
    pos = next((p for p in world.positions if p.ticket == req["position"]), None)
    if pos is None:
        return NS(retcode=10036, comment="position closed", order=0, deal=0)
    d = 1 if pos.type == 0 else -1
    vol = req["volume"]
    profit = (req["price"] - pos.price_open) * d * vol * 100.0
    world.deals.append(NS(ticket=_new_ticket(), order=0, position_id=pos.ticket, time=int(time.time()),
                          entry=DEAL_ENTRY_OUT, type=1 - pos.type, volume=vol, price=req["price"], commission=-3.5 * vol,
                          swap=0.0, profit=profit, fee=0.0, reason=req.get("_reason", 3)))
    world.balance += profit
    pos.volume = round(pos.volume - vol, 8)
    if pos.volume <= 1e-9:
        world.positions.remove(pos)
    return NS(retcode=TRADE_RETCODE_DONE, order=0, deal=world.deals[-1].ticket, comment="")


def order_check(req):
    # V4.3 broker preflight stand-in: the real API returns retcode=0 with comment=Done on a valid check.
    return NS(retcode=0, comment="Done", balance=world.balance, equity=world.equity, margin=world.margin, margin_free=world.equity-world.margin)


def order_send(req):
    world.calls.append(dict(req))
    if world.script:
        s = world.script.pop(0)
        if s == "none":
            return None
        if s == "executed_no_reply":
            _open(req)
            return None
        if isinstance(s, int):
            return NS(retcode=s, comment="scripted", order=0, deal=0)
    if req["action"] == TRADE_ACTION_PENDING:
        t = _new_ticket()
        world.pending_orders.append(NS(ticket=t, magic=req["magic"], type=req["type"], price_open=req["price"], sl=req["sl"],
                                       tp=req["tp"], volume_initial=req["volume"], comment=req.get("comment", ""),
                                       expiration=req.get("expiration")))
        return NS(retcode=TRADE_RETCODE_PLACED, order=t, deal=0, comment="")
    if req["action"] == TRADE_ACTION_REMOVE:
        before = len(world.pending_orders)
        world.pending_orders = [o for o in world.pending_orders if o.ticket != req["order"]]
        return NS(retcode=TRADE_RETCODE_DONE if len(world.pending_orders) < before else 10013, order=0, deal=0, comment="")
    if req["action"] == TRADE_ACTION_DEAL and "position" not in req:
        return _open(req)
    if req["action"] == TRADE_ACTION_DEAL:
        return _close(req)
    if req["action"] == TRADE_ACTION_SLTP:
        pos = next((p for p in world.positions if p.ticket == req["position"]), None)
        if pos is None:
            return NS(retcode=10036, comment="no position", order=0, deal=0)
        if world.reject_modify:
            return NS(retcode=TRADE_RETCODE_INVALID_STOPS, comment="invalid stops", order=0, deal=0)
        pos.sl, pos.tp = req["sl"], req["tp"]
        return NS(retcode=TRADE_RETCODE_DONE, order=0, deal=0, comment="")
    return NS(retcode=10013, comment="invalid", order=0, deal=0)


def copy_rates_from_pos(symbol, tf, start, n):
    df = world.rates.get(_TF_NAME.get(tf))
    if df is None:
        return None
    df = df.iloc[-n:] if len(df) >= n else df
    last = df.iloc[-1:].copy()
    last.index = last.index + (df.index[-1] - df.index[-2] if len(df) > 1 else np.timedelta64(5, "m"))
    df = np.concatenate([df.index.values, last.index.values]), np.concatenate([df.values, last.values])
    arr = np.zeros(len(df[0]), dtype=[("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8"),
                                      ("tick_volume", "u8"), ("spread", "i4"), ("real_volume", "u8")])
    arr["time"] = df[0].astype("datetime64[s]").astype("i8")
    for i, c in enumerate(("open", "high", "low", "close", "tick_volume")):
        arr[c] = df[1][:, i]
    return arr


def copy_rates_range(symbol, tf, fr, to):
    df = world.rates.get(_TF_NAME.get(tf))
    if df is None:
        return None
    import pandas as pd
    sub = df[(df.index >= pd.Timestamp(fr).tz_localize(None)) & (df.index <= pd.Timestamp(to).tz_localize(None))]
    arr = np.zeros(len(sub), dtype=[("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8")])
    for c in ("open", "high", "low", "close"):
        arr[c] = sub[c].values
    return arr


def fill_pending(ticket):
    """Test helper: the broker fills a pending limit order."""
    o = next(x for x in world.pending_orders if x.ticket == ticket)
    world.pending_orders.remove(o)
    _open({"type": 0 if o.type == ORDER_TYPE_BUY_LIMIT else 1, "price": o.price_open, "magic": o.magic, "volume": o.volume_initial,
           "sl": o.sl, "tp": o.tp, "symbol": "XAUUSD", "comment": o.comment})


def install():
    sys.modules["MetaTrader5"] = sys.modules[__name__]
    return world
