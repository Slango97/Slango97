"""Broker intelligence: validate symbol metadata against the broker's own profit calculation and explain any
discrepancy. V3 observed tick-size/tick-value sizing disagreeing materially with mt5.order_calc_profit for XAUUSD.
Rule: the broker calculation is trusted only if an INDEPENDENT derivation (contract size x move x FX, or
tick metadata, or a recognised scaling/asymmetry cause) confirms it. Otherwise fail closed.

Run on your terminal to get a report you can keep or share:   python broker_diag.py
"""
import json
import math
from dataclasses import asdict, dataclass, field


@dataclass
class BrokerDiagnosis:
    status: str = "FAIL"                 # OK | WARN | FAIL
    sizing_method: str = "none"          # broker_calc | tick_value | none
    tick_sizing_trusted: bool = False
    cause: str = ""
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


def _close(a, b, tol):
    return a is not None and b is not None and b != 0 and abs(a / b - 1) <= tol


def _fx(api, profit_ccy, account_ccy):
    """Rate converting 1 unit of profit currency into account currency, from a live FX tick, or None."""
    if profit_ccy == account_ccy:
        return 1.0
    for sym, inv in ((profit_ccy + account_ccy, False), (account_ccy + profit_ccy, True)):
        t = api.symbol_info_tick(sym) if api.symbol_info(sym) is not None else None
        if t is not None and t.bid > 0:
            mid = (t.bid + t.ask) / 2
            return 1 / mid if inv else mid
    return None


def diagnose(api, symbol, cfg):
    tol = cfg.reliability.profit_tolerance
    dg = BrokerDiagnosis()
    info, acc, tick = api.symbol_info(symbol), api.account_info(), api.symbol_info_tick(symbol)
    if info is None or acc is None or tick is None or tick.ask <= 0:
        dg.errors.append("symbol, account or tick unavailable")
        return dg
    m = dg.metrics
    m.update(point=info.point, digits=info.digits, tick_size=info.trade_tick_size, tick_value=info.trade_tick_value,
             tick_value_profit=getattr(info, "trade_tick_value_profit", None), tick_value_loss=getattr(info, "trade_tick_value_loss", None),
             contract_size=info.trade_contract_size, volume_min=info.volume_min, volume_max=info.volume_max,
             volume_step=info.volume_step, stops_level=info.trade_stops_level, freeze_level=info.trade_freeze_level,
             filling_mode=info.filling_mode, calc_mode=getattr(info, "trade_calc_mode", None),
             profit_currency=getattr(info, "currency_profit", "USD"), account_currency=getattr(acc, "currency", "USD"),
             leverage=getattr(acc, "leverage", None))
    if info.point <= 0 or info.trade_tick_size <= 0 or info.trade_tick_value <= 0:
        dg.errors.append("non-positive point / tick size / tick value")
    if info.digits is not None and info.point > 0 and abs(info.point - 10 ** -info.digits) > 1e-12:
        dg.warnings.append(f"point {info.point} does not match digits {info.digits}")
    if info.point > 0 and info.trade_tick_size > 0 and abs(round(info.trade_tick_size / info.point) - info.trade_tick_size / info.point) > 1e-6:
        dg.warnings.append("tick size is not a multiple of point")
    if info.volume_min <= 0 or info.volume_step <= 0 or info.volume_max < info.volume_min:
        dg.errors.append("invalid lot limits")
    elif abs(round(info.volume_min / info.volume_step) - info.volume_min / info.volume_step) > 1e-6:
        dg.warnings.append("minimum lot is not a multiple of the lot step")
    if not info.filling_mode:
        dg.errors.append("no supported filling mode")
    price = tick.ask
    if info.trade_stops_level * info.point > 0.01 * price:
        dg.errors.append(f"stops level {info.trade_stops_level} pts is abnormally large")

    def calc(typ, lots, p0, p1):
        try:
            return api.order_calc_profit(typ, symbol, lots, p0, p1)
        except Exception:
            return None
    B, S = api.ORDER_TYPE_BUY, api.ORDER_TYPE_SELL
    probes = {"buy_1lot_+1": calc(B, 1.0, price, price + 1.0), "buy_1lot_+5": calc(B, 1.0, price, price + 5.0),
              "buy_01lot_+1": calc(B, 0.1, price, price + 1.0), "sell_1lot_-1": calc(S, 1.0, price, price - 1.0),
              "buy_1lot_-1": calc(B, 1.0, price, price - 1.0)}
    m["probes"] = probes
    if any(v is None for v in probes.values()):
        dg.errors.append("order_calc_profit unavailable")
    else:
        base = probes["buy_1lot_+1"]
        if not (_close(probes["buy_1lot_+5"], 5 * base, tol) and _close(probes["buy_01lot_+1"], 0.1 * base, tol)):
            dg.errors.append("broker profit is not linear in volume/move")
        if not _close(abs(probes["sell_1lot_-1"]), abs(base), tol):
            dg.warnings.append("buy/sell profit asymmetric")
        loss = -probes["buy_1lot_-1"]
        tick_based = 1.0 / info.trade_tick_size * info.trade_tick_value
        fx = _fx(api, m["profit_currency"], m["account_currency"])
        contract_based = info.trade_contract_size * 1.0 * fx if fx else None
        m.update(broker_per_lot_per_unit=round(base, 6), broker_loss_per_lot_per_unit=round(loss, 6),
                 tick_based_per_lot_per_unit=round(tick_based, 6),
                 contract_based_per_lot_per_unit=None if contract_based is None else round(contract_based, 6),
                 fx_profit_to_account=fx, ratio_broker_to_tick=round(base / tick_based, 6) if tick_based else None)
        tvl = m["tick_value_loss"]
        if _close(loss, tick_based, tol):
            dg.cause, dg.tick_sizing_trusted = "CONSISTENT", True
        elif _close(loss, contract_based, tol + 0.01):
            dg.cause = ("ACCOUNT_CURRENCY_CONVERSION" if fx != 1.0 else "TICK_METADATA_INCONSISTENT") + \
                       ": broker calc matches contract size x move x FX; tick metadata does not"
        elif tvl and _close(loss, 1.0 / info.trade_tick_size * tvl, tol):
            dg.cause = "ASYMMETRIC_TICK_VALUE: broker uses the loss tick value"
        else:
            r = base / tick_based if tick_based else 0
            k = round(math.log10(r)) if r > 0 else 0
            if k != 0 and _close(r, 10.0 ** k, tol) and contract_based is None:
                dg.cause = f"SCALE_1E{k}: metadata off by 10^{k}, and no independent check is possible"
                dg.errors.append("profit calculation disagrees with tick metadata by a power of ten and cannot be "
                                 "confirmed independently (account/profit currency FX unavailable)")
            else:
                dg.cause = "UNEXPLAINED"
                dg.errors.append(f"broker profit {base:.4f}/lot/unit disagrees with tick metadata {tick_based:.4f} and contract "
                                 f"derivation {contract_based} - cause unknown, refusing to size trades")
        if dg.cause and not dg.cause.startswith("CONSISTENT") and not dg.errors:
            dg.warnings.append("sizing uses order_calc_profit; tick-value fallback disabled. Cause: " + dg.cause)
    exp = cfg.system.expected_contract_size
    if abs(info.trade_contract_size - exp) > 1e-9:
        dg.warnings.append(f"contract size {info.trade_contract_size} (expected {exp}): position sizes differ from a 100 oz contract")
    try:
        mg = api.order_calc_margin(B, symbol, 1.0, price)
        lev = m["leverage"]
        fx = m.get("fx_profit_to_account") or 1.0
        if mg and lev:
            implied = info.trade_contract_size * price * fx / mg
            m.update(margin_per_lot=round(mg, 2), implied_leverage=round(implied, 1))
            if not 0.2 <= implied / lev <= 5:
                dg.warnings.append(f"symbol margin implies leverage {implied:.0f} vs account {lev}")
    except Exception:
        dg.warnings.append("order_calc_margin unavailable")
    dg.sizing_method = "none" if dg.errors else "broker_calc"
    dg.status = "FAIL" if dg.errors else ("WARN" if dg.warnings and dg.cause != "CONSISTENT" else "OK")
    return dg


def main():
    import MetaTrader5 as mt5
    from config import Config
    cfg = Config.from_env()
    if not mt5.initialize():
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
    from symbol_resolver import discover_gold_symbol
    cand = discover_gold_symbol(mt5, preferred="XAUUSD", aliases=cfg.system.symbol_aliases, pin=cfg.system.symbol)
    if cand is None:
        raise SystemExit("tradable gold symbol not found in MT5 inventory")
    sym = cand.name
    print(json.dumps(diagnose(mt5, sym, cfg).to_dict(), indent=2, default=str))
    mt5.shutdown()


if __name__ == "__main__":
    main()
