"""Chart rendering for Telegram (Enhancement Spec sections 8-11). No mplfinance dependency.

Charts are VISUALISATIONS of the trading brain, never a second decision engine:

* A - market structure: candles, swing highs/lows, support/resistance, FVG / order-block zones, liquidity sweep;
* B - indicators: only what the kernel computes and uses (EMA20/50/200, RSI, Stochastic, MACD histogram, ATR),
      calculated with the kernel's own `indicators.add_indicators`;
* C - execution setup: entry / entry zone, SL, TP1, TP2 (+ optional TP3 runner), direction, RR, timeframe,
      validity and a Lebanon-local timestamp. Its levels come ONLY from a `TradePlan` built from the exact
      candidate handed to the risk/execution engine (or the broker's executed values), so the picture and the
      order can never disagree.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from pricefmt import fmt_price

UP, DOWN, ZONE_BULL, ZONE_BEAR = "#1a9850", "#d73027", "#91cf60", "#fc8d59"


@dataclass(frozen=True)
class TradePlan:
    symbol: str
    timeframe: str
    direction: int
    entry: float
    sl: float
    tp1: float | None
    tp2: float
    tp3: float | None = None
    rr: float | None = None
    entry_zone: tuple | None = None
    valid_until: str = ""
    setup: str = ""
    source: str = "candidate"          # candidate | executed
    ticket: int = 0

    @property
    def side(self):
        return "BUY" if self.direction > 0 else "SELL"

    def to_dict(self):
        return asdict(self)


def plan_from_decision(dec, symbol, timeframe, execution=None, valid_until=""):
    """The ONLY way to build execution-chart levels: copy them from the decision object passed to
    ExecutionEngine.execute (and, once filled, from the broker's executed entry/SL/TP)."""
    ex = execution or {}
    zone = None
    inv = getattr(dec, "invalidation", None) or {}
    if inv.get("zone"):
        z = inv["zone"]
        zone = (float(min(z)), float(max(z)))
    entry = float(ex.get("fill", dec.entry))
    sl = float(ex.get("sl") or dec.sl)
    tp = float(ex.get("tp") or dec.tp)
    risk = abs(entry - sl)
    rr = round(abs(tp - entry) / risk, 2) if risk > 0 else getattr(dec, "rr", None)
    tp1 = getattr(dec, "tp1", None)
    return TradePlan(symbol=str(symbol), timeframe=str(timeframe), direction=int(dec.direction), entry=entry, sl=sl,
                     tp1=float(tp1) if tp1 else None, tp2=tp, rr=rr, entry_zone=zone, valid_until=valid_until,
                     setup=str(getattr(dec, "setup", "") or ""), source="executed" if execution else "candidate",
                     ticket=int(ex.get("ticket", 0) or 0))


def _plt():
    try:
        import matplotlib
        matplotlib.use("Agg")                 # headless: reporting runs off the GUI/main thread
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle
        return plt, Rectangle
    except Exception:
        return None, None


def _candles(ax, d, Rectangle):
    for i, (o, h, l, c) in enumerate(zip(d["open"].astype(float), d["high"].astype(float),
                                         d["low"].astype(float), d["close"].astype(float))):
        col = UP if c >= o else DOWN
        ax.vlines(i, l, h, linewidth=0.8, color=col)
        ax.add_patch(Rectangle((i - 0.32, min(o, c)), 0.64, max(abs(c - o), 1e-9), facecolor=col, edgecolor=col,
                               linewidth=0.6, alpha=0.85))
    ax.set_xlim(-1, len(d) + 8)
    ax.grid(alpha=0.15)


def _save(fig, plt, path):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(p, dpi=120)
    plt.close(fig)
    return str(p)


def _scale(d):
    try:
        return float((d["high"] - d["low"]).tail(50).mean())
    except Exception:
        return None


def render_chart(df, *, path, title="SLANGO PRO TRADER", entry=None, sl=None, tp=None, levels=None, patterns=None):
    """V5.0 single-panel chart (kept for compatibility)."""
    if df is None or len(df) < 10:
        return None
    plt, Rectangle = _plt()
    if plt is None:
        return None
    d = df.tail(90)
    sc = _scale(d)
    fig, ax = plt.subplots(figsize=(12, 7))
    _candles(ax, d, Rectangle)
    for val, ls, name in ((entry, "--", "Entry"), (sl, ":", "SL"), (tp, ":", "TP")):
        if val is not None:
            ax.axhline(float(val), linestyle=ls, linewidth=1.2, label=f"{name} {fmt_price(val, sc)}")
    for lev in levels or []:
        try:
            ax.axhline(float(lev), linestyle="-", linewidth=0.7, alpha=0.5)
        except Exception:
            pass
    ax.set_title(title + (f" | Patterns: {', '.join(patterns[:4])}" if patterns else ""))
    if entry is not None or sl is not None or tp is not None:
        ax.legend(loc="upper left", fontsize=8)
    fig.tight_layout()
    return _save(fig, plt, path)


def render_structure(df, view, *, path, title, stamp=""):
    """Chart A: why the market is interesting (structure, S/R, zones, liquidity)."""
    if df is None or len(df) < 20:
        return None
    plt, Rectangle = _plt()
    if plt is None:
        return None
    n = min(120, len(df))
    d = df.tail(n)
    offset = len(df) - n
    sc = _scale(d)
    v = view or {}
    fig, ax = plt.subplots(figsize=(12, 7))
    _candles(ax, d, Rectangle)
    lo, hi = float(d["low"].min()), float(d["high"].max())
    pad = 0.05 * (hi - lo)
    for z in (v.get("fvg") or [])[-4:] + (v.get("ob") or [])[-4:]:
        try:
            top, bot = float(z["top"]), float(z["bottom"])
            if bot > hi + pad or top < lo - pad:
                continue
            bull = z.get("type") == "bull"
            kind = "OB" if "disp" in z else "FVG"
            ax.add_patch(Rectangle((0, bot), len(d) + 8, top - bot, facecolor=ZONE_BULL if bull else ZONE_BEAR,
                                   alpha=0.18, edgecolor="none"))
            ax.text(len(d) + 7, (top + bot) / 2, f"{'bull' if bull else 'bear'} {kind}", fontsize=7, ha="right", va="center")
        except Exception:
            continue
    for s in (v.get("sr") or [])[:8]:
        try:
            lv = float(s["level"])
            if lo - pad <= lv <= hi + pad:
                ax.axhline(lv, color="#4d4d4d", linewidth=0.6 + 0.2 * min(4, s.get("touches", 1)), alpha=0.6)
                ax.text(1, lv, f"S/R x{s.get('touches', '?')} {fmt_price(lv, sc)}", fontsize=7, va="bottom")
        except Exception:
            continue
    for key, marker, col in (("swing_highs", "v", DOWN), ("swing_lows", "^", UP)):
        for k, p in (v.get(key) or [])[-12:]:
            try:
                x = int(k) - offset
                if 0 <= x < len(d):
                    ax.scatter([x], [float(p)], marker=marker, color=col, s=28, zorder=3)
            except Exception:
                continue
    sw = v.get("sweep")
    if isinstance(sw, dict) and sw.get("extreme") is not None:
        ax.axhline(float(sw["extreme"]), color="#7b3294", linestyle="--", linewidth=0.9,
                   label=f"liquidity sweep {fmt_price(sw['extreme'], sc)}")
        ax.legend(loc="upper left", fontsize=8)
    ax.set_ylim(lo - pad, hi + pad)
    trend = {1: "BULLISH", -1: "BEARISH"}.get(v.get("struct_trend"), "RANGE/NEUTRAL")
    ax.set_title(f"{title}\nA - Market structure: {trend} | {v.get('struct_regime', '')} | {stamp}", fontsize=10)
    fig.tight_layout()
    return _save(fig, plt, path)


def render_indicators(df, *, path, title, stamp="", bounds=None):
    """Chart B: the indicator evidence the kernel actually uses (no decorative extras)."""
    if df is None or len(df) < 60:
        return None
    plt, Rectangle = _plt()
    if plt is None:
        return None
    try:
        from indicators import add_indicators
        full = df if "macd_hist" in df.columns else add_indicators(df, bounds)
    except Exception:
        return None
    d = full.tail(120)
    fig, axes = plt.subplots(4, 1, figsize=(12, 10), sharex=True, gridspec_kw={"height_ratios": [3, 1, 1, 1]})
    ax, ax_rsi, ax_macd, ax_atr = axes
    _candles(ax, d, Rectangle)
    x = range(len(d))
    for col, c in (("ema20", "#2b83ba"), ("ema50", "#fdae61"), ("ema200", "#5e3c99")):
        if col in d:
            ax.plot(x, d[col].astype(float).values, color=c, linewidth=1.1, label=col.upper())
    ax.legend(loc="upper left", fontsize=8)
    ax.set_title(f"{title}\nB - Indicators used by the kernel | {stamp}", fontsize=10)
    if "rsi" in d:
        ax_rsi.plot(x, d["rsi"].astype(float).values, color="#2b83ba", linewidth=1, label="RSI 14")
    if "stoch_k" in d:
        ax_rsi.plot(x, d["stoch_k"].astype(float).values, color="#bababa", linewidth=0.8, label="Stoch %K")
    for lv in (30, 70):
        ax_rsi.axhline(lv, color="#999999", linewidth=0.6, linestyle=":")
    ax_rsi.set_ylim(0, 100)
    ax_rsi.legend(loc="upper left", fontsize=7)
    if "macd_hist" in d:
        h = d["macd_hist"].astype(float).values
        ax_macd.bar(list(x), h, color=[UP if v >= 0 else DOWN for v in h], width=0.8, label="MACD hist")
        ax_macd.legend(loc="upper left", fontsize=7)
    if "atr" in d:
        ax_atr.plot(x, d["atr"].astype(float).values, color="#636363", linewidth=1, label="ATR 14")
        ax_atr.legend(loc="upper left", fontsize=7)
    for a in axes[1:]:
        a.grid(alpha=0.15)
    fig.tight_layout()
    return _save(fig, plt, path)


def render_execution(df, plan: TradePlan, *, path, title, stamp=""):
    """Chart C: the actionable plan, drawn from `plan` only (TP3/runner > TP2 > TP1 > ENTRY ZONE > SL)."""
    if df is None or len(df) < 10 or plan is None:
        return None
    plt, Rectangle = _plt()
    if plt is None:
        return None
    d = df.tail(90)
    sc = abs(plan.entry - plan.sl) or _scale(d)
    fig, ax = plt.subplots(figsize=(12, 7))
    _candles(ax, d, Rectangle)
    right = len(d) + 8
    if plan.entry_zone:
        zlo, zhi = plan.entry_zone
        ax.add_patch(Rectangle((len(d) - 1, zlo), 9, max(zhi - zlo, 1e-9), facecolor="#80b1d3", alpha=0.35,
                               edgecolor="#1f78b4", label=f"ENTRY ZONE {fmt_price(zlo, sc)}-{fmt_price(zhi, sc)}"))
    rows = [("SL", plan.sl, DOWN, "-"), ("ENTRY", plan.entry, "#1f78b4", "--")]
    if plan.tp1:
        rows.append(("TP1", plan.tp1, UP, ":"))
    rows.append(("TP2" if plan.tp1 else "TP", plan.tp2, UP, "-"))
    if plan.tp3:
        rows.append(("TP3 / runner", plan.tp3, "#006837", "-."))
    for name, val, col, ls in rows:
        ax.axhline(float(val), color=col, linestyle=ls, linewidth=1.4)
        ax.text(right - 0.5, float(val), f"{name} {fmt_price(val, sc)}", color=col, fontsize=9, ha="right",
                va="bottom", fontweight="bold")
    lo = min(float(d["low"].min()), *(float(v) for _, v, _, _ in rows))
    hi = max(float(d["high"].max()), *(float(v) for _, v, _, _ in rows))
    pad = 0.04 * (hi - lo)
    ax.set_ylim(lo - pad, hi + pad)
    ax.set_xlim(-1, right)
    rr = f"RR 1:{plan.rr:.2f}" if plan.rr else ""
    valid = f" | valid until {plan.valid_until}" if plan.valid_until else ""
    src = "EXECUTED" if plan.source == "executed" else "TRADE-READY"
    ax.set_title(f"{title}\nC - {src} {plan.side} {plan.symbol} {plan.timeframe} | {plan.setup} | {rr}{valid} | {stamp}",
                 fontsize=10)
    if plan.entry_zone:
        ax.legend(loc="upper left", fontsize=8)
    fig.tight_layout()
    return _save(fig, plt, path)
