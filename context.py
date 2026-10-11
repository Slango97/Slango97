"""Macro reference context from daily bars: monthly and yearly opens, highs, lows and midpoints.
These are reference levels and liquidity, never BUY/SELL votes."""
import numpy as np

HIGH_LEVELS = ("PMH", "PYH", "CMH", "CYH")
LOW_LEVELS = ("PML", "PYL", "CML", "CYL")
POOL_KINDS = ("PMH", "PML", "PYH", "PYL")


def macro_levels(df):
    if len(df) < 2:
        return {}
    idx = df.index
    ym = np.asarray(idx.year * 12 + idx.month)
    yr = np.asarray(idx.year)
    o, h, l, c = (df[x].values for x in ("open", "high", "low", "close"))
    out = {}
    cm = ym == ym[-1]
    out.update(MO=float(o[cm][0]), CMH=float(h[cm].max()), CML=float(l[cm].min()))
    pm = ym == ym[-1] - 1
    if pm.any():
        out.update(PMH=float(h[pm].max()), PML=float(l[pm].min()), PMC=float(c[pm][-1]))
        out["PM_MID"] = (out["PMH"] + out["PML"]) / 2
    cy = yr == yr[-1]
    out.update(YO=float(o[cy][0]), CYH=float(h[cy].max()), CYL=float(l[cy].min()))
    py = yr == yr[-1] - 1
    if py.any():
        out.update(PYH=float(h[py].max()), PYL=float(l[py].min()))
        out["PY_MID"] = (out["PYH"] + out["PYL"]) / 2
    return out


def macro_context(levels, price, atr):
    if not levels:
        return {"levels": {}, "notes": []}
    notes = []
    for k, name in (("MO", "monthly open"), ("YO", "yearly open")):
        if k in levels:
            notes.append(f"{'above' if price > levels[k] else 'below'} {name} {levels[k]:.2f}")
    if "PMH" in levels:
        if price > levels["PMH"]:
            notes.append("above last month's high")
        elif price < levels["PML"]:
            notes.append("below last month's low")
        else:
            notes.append(f"{'upper' if price > levels['PM_MID'] else 'lower'} half of last month's range")
    ext = [(k, v) for k, v in levels.items() if k in ("PYH", "PYL", "PMH", "PML")]
    if ext and atr > 0:
        k, v = min(ext, key=lambda kv: abs(kv[1] - price))
        notes.append(f"nearest macro extreme {k} {v:.2f} ({abs(v - price) / atr:.1f} ATR)")
    return {"levels": levels, "notes": notes}
