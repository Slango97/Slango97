"""Per-market contract and cost specifications for multi-market research (V5.6).

The research kernel was written for XAUUSD: its default cost model charges $0.02 slippage (200 pips on EURUSD)
and its fixed 40-point spread cap rejects every BTC/index bar. Each market therefore gets its own point size,
contract size, typical spread/slippage in PRICE units, commission and weekend rule.

The defaults below are typical retail ECN values, NOT your broker's. `export_history.py --markets ...` writes
<market>/export_meta.json with the broker's real point, digits, contract size and profit currency, and the
observed spread column; `spec_for` lets those override the defaults. Results computed on default costs must be
labelled as such (market_research.py does).
"""
from dataclasses import asdict, dataclass, replace


@dataclass(frozen=True)
class MarketSpec:
    market: str                       # requested watchlist name (XAUUSD, EURUSD, ...)
    point: float
    contract_size: float
    spread: float                     # typical spread, price units (used where the CSV has no spread column)
    slippage: float                   # base adverse slippage per fill, price units
    commission_usd_per_lot_side: float
    profit_currency: str = "USD"
    trades_weekends: bool = False
    source: str = "default"           # default | broker_export

    def to_dict(self):
        return asdict(self)


DEFAULTS = {
    "XAUUSD": MarketSpec("XAUUSD", 0.01, 100.0, 0.25, 0.05, 3.5),
    "XAGUSD": MarketSpec("XAGUSD", 0.001, 5000.0, 0.025, 0.005, 3.5),
    "EURUSD": MarketSpec("EURUSD", 0.00001, 100000.0, 0.00008, 0.00002, 3.5),
    "GBPUSD": MarketSpec("GBPUSD", 0.00001, 100000.0, 0.00010, 0.00003, 3.5),
    "AUDUSD": MarketSpec("AUDUSD", 0.00001, 100000.0, 0.00009, 0.00002, 3.5),
    "USDJPY": MarketSpec("USDJPY", 0.001, 100000.0, 0.010, 0.003, 3.5, "JPY"),
    "USDCAD": MarketSpec("USDCAD", 0.00001, 100000.0, 0.00012, 0.00003, 3.5, "CAD"),
    "USDCHF": MarketSpec("USDCHF", 0.00001, 100000.0, 0.00012, 0.00003, 3.5, "CHF"),
    "US30": MarketSpec("US30", 0.1, 1.0, 2.5, 1.0, 0.0),
    "NAS100": MarketSpec("NAS100", 0.1, 1.0, 1.5, 0.5, 0.0),
    "USOIL": MarketSpec("USOIL", 0.01, 100.0, 0.04, 0.01, 0.0),
    "BTCUSD": MarketSpec("BTCUSD", 0.01, 1.0, 25.0, 5.0, 0.0, trades_weekends=True),
}


def canonical(name):
    """Map a broker symbol (XAUUSDs, GOLD, US30Cash, EURUSD.a ...) to its watchlist name, or None."""
    try:
        from brain.universe import ALIASES
    except Exception:                                 # pragma: no cover
        ALIASES = {}
    raw = str(name or "")
    up = raw.upper()
    for req, aliases in ALIASES.items():
        if up == req or up in (a.upper() for a in aliases):
            return req
    clean = "".join(ch for ch in up if ch.isalnum())
    for req in sorted(DEFAULTS, key=len, reverse=True):
        if clean.startswith(req):
            return req
    if clean.startswith("GOLD"):
        return "XAUUSD"
    if clean.startswith("SILVER"):
        return "XAGUSD"
    return None


def spec_for(market, meta=None):
    """Default spec for `market`, overridden by a broker export_meta.json dict when supplied."""
    req = canonical(market) or str(market).upper()
    base = DEFAULTS.get(req) or MarketSpec(req, 0.00001, 100000.0, 0.0001, 0.00003, 3.5)
    if not meta:
        return base
    upd = {"source": "broker_export"}
    for key, field_ in (("point", "point"), ("contract_size", "contract_size")):
        try:
            v = float(meta.get(key))
            if v > 0:
                upd[field_] = v
        except (TypeError, ValueError):
            pass
    if meta.get("currency_profit"):
        upd["profit_currency"] = str(meta["currency_profit"]).upper()
    try:
        sp = float(meta.get("median_spread_points"))
        if sp > 0:
            upd["spread"] = sp * upd.get("point", base.point)
    except (TypeError, ValueError):
        pass
    return replace(base, **upd)


def commission_in_profit_currency(spec, price):
    """Backtest P&L is in the profit (quote) currency; a USD commission on a USD-base pair (USDJPY, USDCAD,
    USDCHF) is worth `price` quote units per USD."""
    if spec.profit_currency == "USD":
        return spec.commission_usd_per_lot_side
    return spec.commission_usd_per_lot_side * float(price)
