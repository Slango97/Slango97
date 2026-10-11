import numpy as np
from dataclasses import dataclass, field

from context import macro_levels
from sessions import session_context
from indicators import (DEFAULT_BOUNDS, add_indicators, adx_band, atr_regime, slope, validate_ohlc)
from smc import (build_pools, displacement, equal_levels, fair_value_gaps, fib_range, find_sweep, key_levels,
                 order_blocks, sr_levels, structure, trendline)

DEFAULT_PHASES = (("ASIA", 0, 420), ("LONDON_OPEN", 420, 540), ("LONDON_CONT", 540, 720), ("OVERLAP", 720, 810),
                  ("NY_OPEN", 810, 930), ("NY_CONT", 930, 1140), ("NY_CLOSE", 1140, 1260), ("OFF", 1260, 1440))


@dataclass
class MarketState:
    views: dict
    session: str
    time: object
    spread: float = 0.0
    issues: dict = field(default_factory=dict)
    regime: object = None          # regime.Regime
    micro: object = None           # microstructure.MarketQuality
    liquidity: object = None       # liquidity.LiquidityMap
    news: object = None            # news.NewsContext
    aux: object = None             # macro driver biases (signed for gold)


def _sign(x, eps=1e-12):
    return 1 if x > eps else -1 if x < -eps else 0


def regimes(v, adx_bands):
    """Condense raw features into the regimes the decision layer reasons about."""
    trend_dir = _sign(v["ema_trend"] + v["struct_trend"])
    strength = 0
    if v["ema_trend"] != 0:
        strength += 30
    if v["struct_trend"] != 0 and v["struct_trend"] == v["ema_trend"]:
        strength += 30
    strength += {"DEAD": 0, "WEAK": 5, "DEVELOPING": 10, "STRONG": 20, "VERY_STRONG": 25}[v["adx_band"]]
    if trend_dir and _sign(v["ema_slope"]) == trend_dir:
        strength += 15
    mom_dir = _sign(v["macd_hist"])
    expanding = mom_dir != 0 and v["macd_slope"] * mom_dir > 0 and v["atr_ratio"] >= 0.95
    fading = mom_dir != 0 and v["macd_slope"] * mom_dir < 0
    ev = v["last_event"]
    recent_ev = ev is not None and v["df_len"] - 1 - ev["idx"] <= 30
    structure_lbl = (f"{'BULLISH' if ev['dir'] > 0 else 'BEARISH'}_{ev['type']}" if recent_ev else v["struct_regime"])
    sw = v["sweep"]
    liq = "NONE"
    if sw and v["df_len"] - 1 - sw["bar"] <= 12:
        liq = "SELL_SIDE_SWEEP" if sw["dir"] > 0 else "BUY_SIDE_SWEEP"
    vol_pts = {"NORMAL": 25, "HIGH": 18, "LOW": 10, "EXTREME": 0}[v["vol_regime"]]
    quality = int(min(100, 0.4 * min(strength, 100) + vol_pts + (20 if v["struct_regime"] != "RANGE" else 8)
                      + (15 if v["adx_band"] in ("DEVELOPING", "STRONG", "VERY_STRONG") else 0)))
    return {"trend": {1: "BULLISH", -1: "BEARISH", 0: "NEUTRAL"}[trend_dir], "trend_dir": trend_dir,
            "trend_strength": min(strength, 100), "volatility": v["vol_regime"],
            "momentum": "EXPANDING" if expanding else "FADING" if fading else "STABLE", "momentum_dir": mom_dir,
            "structure": structure_lbl, "liquidity": liq, "market_quality": quality}


def doc_momentum(d):
    """Momentum triggers named in the strategy documents, on completed candles only:
    RSI divergence (price lower low + RSI higher low = bullish, mirror for bearish), RSI reclaiming 30 / losing 70
    (oversold / overbought rejection) and a slow-Stochastic exhaustion cross (below 20 / above 80)."""
    out = {"rsi_div": 0, "rsi_reclaim": 0, "stoch_x": 0, "stoch_k": None}
    try:
        rsi = d["rsi"].values
        if len(rsi) >= 5:
            prev, cur = rsi[-4:-1], rsi[-1]
            if prev.min() < 30 <= cur:
                out["rsi_reclaim"] = 1
            elif prev.max() > 70 >= cur:
                out["rsi_reclaim"] = -1
        if "stoch_k" in d and len(d) >= 3:
            k, kd = d["stoch_k"].values, d["stoch_d"].values
            if np.isfinite(k[-1]):
                out["stoch_k"] = round(float(k[-1]), 1)
            if np.all(np.isfinite([k[-2], k[-1], kd[-2], kd[-1]])):
                if k[-2] <= kd[-2] and k[-1] > kd[-1] and min(k[-2], k[-1]) < 20:
                    out["stoch_x"] = 1
                elif k[-2] >= kd[-2] and k[-1] < kd[-1] and max(k[-2], k[-1]) > 80:
                    out["stoch_x"] = -1
        from smc import swings
        w = d.iloc[-80:]
        sh, sl = swings(w, 3)
        lows, highs = np.where(sl)[0], np.where(sh)[0]
        lo, hi, rv, n = w["low"].values, w["high"].values, w["rsi"].values, len(w)
        if len(lows) >= 2 and lows[-1] >= n - 15 and lo[lows[-1]] < lo[lows[-2]] and rv[lows[-1]] > rv[lows[-2]] + 2:
            out["rsi_div"] = 1
        if len(highs) >= 2 and highs[-1] >= n - 15 and hi[highs[-1]] > hi[highs[-2]] and rv[highs[-1]] < rv[highs[-2]] - 2:
            out["rsi_div"] = -1 if out["rsi_div"] == 0 else 0
    except Exception:
        pass
    return out


def analyze_tf(df, tf, bounds=None, adx_bands=(15.0, 20.0, 25.0, 35.0), validate=True, phases=None):
    bounds = bounds or DEFAULT_BOUNDS
    clean, issues = validate_ohlc(df) if validate else (df, [])
    d = clean if "macd_hist" in clean.columns else add_indicators(clean, bounds)   # backtests pass precomputed, causal indicators
    last = d.iloc[-1]
    a = float(last["atr"])
    c = float(last["close"])
    st = structure(d, d["atr"])
    reg = atr_regime(d["atr"])
    if last["ema50"] > last["ema200"] and c > last["ema50"]:
        ema_trend = 1
    elif last["ema50"] < last["ema200"] and c < last["ema50"]:
        ema_trend = -1
    else:
        ema_trend = 0
    eq = equal_levels(st["sh"], st["sl"], a)
    keys = key_levels(d, bounds) if tf not in ("D1", "W1") else {}
    sweep = find_sweep(d, build_pools(st["sh"], st["sl"], eq, keys))
    disp_map, disp_events = displacement(d, d["atr"], st["all_events"])
    width = 4 * d["close"].rolling(20).std() / d["close"].rolling(20).mean()
    bbw = width.iloc[-200:].dropna()
    bbw_pct = float((bbw <= bbw.iloc[-1]).mean()) if len(bbw) else 0.5
    v = {
        "tf": tf, "time": d.index[-1], "df_len": len(d), "close": c, "atr": a,
        "atr_pct": reg["atr_pct"], "atr_ratio": reg["atr_ratio"], "vol_regime": reg["regime"],
        "rsi": float(last["rsi"]), "rsi_slope": slope(d["rsi"], 5, 1.0),
        "adx": float(last["adx"]), "adx_band": adx_band(float(last["adx"]), adx_bands),
        "macd_hist": float(last["macd_hist"]), "macd_slope": slope(d["macd_hist"], 3, a),
        "ema20": float(last["ema20"]), "ema_slope": slope(d["ema50"], 5, a),
        "bb_pct": float(last["bb_pct"]), "vwap": float(last["vwap_sess"]), "vwap_day": float(last["vwap_day"]),
        "ema_trend": ema_trend, "struct_trend": st["trend"], "struct_regime": st["regime"],
        "last_event": st["last_event"], "labels": st["labels"],
        "swing_highs": st["sh"], "swing_lows": st["sl"],
        "fvg": fair_value_gaps(d, d["atr"], disp_map=disp_map),
        "ob": order_blocks(d, d["atr"], st["all_events"], disp_map=disp_map),
        "disp_events": disp_events[-5:], "bb_width_pct": bbw_pct, "all_events": st["events"],
        "session_ctx": session_context(d, a, phases or DEFAULT_PHASES) if tf in ("M5", "M15") else {},
        "macro_levels": macro_levels(d) if tf == "D1" else {},
        "sweep": sweep, "sr": sr_levels(st["sh"], st["sl"], a), "eq_levels": eq, "key_levels": keys,
        "fib": fib_range(st["sh"], st["sl"]),
        "tl_up": trendline(st["sl"], len(d), a, "up"), "tl_dn": trendline(st["sh"], len(d), a, "down"),
        "data_issues": issues,
    }
    v["ms"] = regimes(v, adx_bands)
    v.update(doc_momentum(d))
    return v


def bias(v):
    """-1 (bearish) to +1 (bullish) from EMA stack and market structure."""
    return (v["ema_trend"] + v["struct_trend"]) / 2


def build_state(views, now_utc, bounds, spread=0.0, phases=None, **extra):
    from config import session_phase
    ms = MarketState(views=views, session=session_phase(now_utc, phases or DEFAULT_PHASES), time=now_utc, spread=spread,
                     issues={tf: v["data_issues"] for tf, v in views.items() if v.get("data_issues")})
    for k, val in extra.items():
        setattr(ms, k, val)
    return ms
