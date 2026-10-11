"""Broker-aware financial-instrument discovery.

The trader must not assume a universal symbol spelling. Brokers commonly add suffixes
or prefixes (for example XAUUSDs, XAUUSDm, GOLD, etc.). This module ranks the
symbols actually exposed by the connected MT5 terminal using semantic instrument
properties, then lets the caller select the highest-confidence candidate.
"""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class SymbolCandidate:
    name: str
    score: float
    reasons: tuple


def _norm(value) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _text(*values) -> str:
    return " ".join(str(v or "") for v in values).upper()


def score_gold_symbol(info, preferred="XAUUSD", aliases=()):
    """Return (score, reasons); zero means not a credible gold/USD instrument."""
    name = str(getattr(info, "name", "") or "")
    if not name:
        return 0.0, ()

    n = _norm(name)
    pref = _norm(preferred)
    alias_norms = {_norm(a) for a in aliases if a}
    reasons = []
    score = 0.0

    # Strongest identity signals are the canonical name/aliases and XAUUSD roots.
    if n == pref and pref:
        score += 1000
        reasons.append("exact preferred symbol")
    elif n in alias_norms:
        score += 970
        reasons.append("exact configured alias")
    elif pref and n.startswith(pref):
        score += 950
        reasons.append("preferred symbol with broker suffix/prefix")
    elif "XAUUSD" in n:
        score += 900
        reasons.append("contains XAUUSD")

    base = _norm(getattr(info, "currency_base", ""))
    profit = _norm(getattr(info, "currency_profit", ""))
    path_desc = _text(getattr(info, "path", ""), getattr(info, "description", ""), getattr(info, "page", ""))

    if base == "XAU":
        score += 180
        reasons.append("currency_base=XAU")
    if profit == "USD":
        score += 70
        reasons.append("currency_profit=USD")
    if "GOLD" in n:
        score += 600
        reasons.append("gold in symbol name")
    if "GOLD" in path_desc or "XAU" in path_desc:
        score += 120
        reasons.append("gold/XAU in description or path")

    trade_mode = getattr(info, "trade_mode", None)
    if trade_mode == 4:
        score += 80
        reasons.append("full trading mode")
    elif trade_mode in (0, None):
        # Unknown in a mocked/test symbol is not enough to reject it here.
        pass
    else:
        score -= 220
        reasons.append(f"non-full trade mode={trade_mode}")

    if getattr(info, "trade_tick_size", 0) and getattr(info, "trade_tick_value", 0):
        score += 25
        reasons.append("valid tick economics")
    if getattr(info, "volume_min", 0) and getattr(info, "volume_step", 0):
        score += 15
        reasons.append("valid volume constraints")

    # Prevent a generic GOLD symbol with unrelated currencies from winning over a
    # real XAUUSD contract.
    if "GOLD" in n and base not in ("", "XAU") and profit not in ("", "USD"):
        score -= 250
        reasons.append("gold-like name but inconsistent currencies")

    return max(score, 0.0), tuple(reasons)


def rank_gold_symbols(symbols, preferred="XAUUSD", aliases=()):
    ranked = []
    for info in symbols or ():
        score, reasons = score_gold_symbol(info, preferred=preferred, aliases=aliases)
        if score > 0:
            ranked.append(SymbolCandidate(str(getattr(info, "name", "")), score, reasons))
    ranked.sort(key=lambda x: (-x.score, len(x.name), x.name.upper()))
    return ranked


def discover_gold_symbol(api, preferred="XAUUSD", aliases=(), pin=None):
    """Resolve a tradable gold symbol from a live MT5 Python API object."""
    names = [] if not pin or str(pin).upper() == "AUTO" else [str(pin)]
    names += [a for a in aliases if a and a not in names]
    for name in names:
        try:
            if api.symbol_info(name) is not None and api.symbol_select(name, True):
                return SymbolCandidate(name, 1100.0 if _norm(name) == _norm(pin or preferred) else 1000.0,
                                        ("exact configured symbol",) if pin else ("exact configured alias",))
        except Exception:
            continue
    try:
        inventory = api.symbols_get()
    except Exception:
        inventory = None
    for cand in rank_gold_symbols(inventory, preferred=preferred, aliases=aliases):
        try:
            if api.symbol_info(cand.name) is not None and api.symbol_select(cand.name, True):
                return cand
        except Exception:
            continue
    return None
