"""Instrument-agnostic price precision.

The kernel was born on XAUUSD, where two decimals are enough. On EURUSD (5 digits) or USDJPY (3 digits)
rounding to two decimals moves a level by up to half a big figure: TP1 could land BEHIND the entry (an
instant "TP1 hit" -> 50% close + break-even) and a zone-midpoint limit could land beyond the stop.
Precision is therefore derived from the scale of the move being described (stop distance or ATR), so
gold keeps two decimals while FX gets five, without plumbing broker metadata into pure research code.
"""
import math


def price_decimals(scale, minimum=2, extra=2, maximum=8):
    """Decimals needed so rounding error stays below ~1% of `scale` (a stop distance or ATR)."""
    try:
        s = abs(float(scale))
    except (TypeError, ValueError):
        return maximum
    if not math.isfinite(s) or s <= 0:
        return maximum
    return int(max(minimum, min(maximum, math.ceil(-math.log10(s)) + extra)))


def round_price(x, scale):
    return round(float(x), price_decimals(scale))


def fmt_price(x, scale=None, digits=None):
    """Human formatting. Uses broker digits when known, else the scale heuristic, else 5 significant decimals."""
    if not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(float(x)):
        return "n/a"
    if digits is None:
        if scale is not None:
            digits = price_decimals(scale)
        else:
            ax = abs(float(x))
            digits = 2 if ax >= 100 else 3 if ax >= 20 else 5
    return f"{float(x):.{int(digits)}f}"
