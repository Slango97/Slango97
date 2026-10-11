"""Market + Technical Brain.

Consumes the existing V4 analysis views and optionally raw OHLC frames.  It does not
place trades and does not mutate deterministic indicators.
"""
from __future__ import annotations

from math import isfinite

import numpy as np

from .models import BrainFinding


def _dir_label(d: int) -> str:
    return "BULLISH" if d > 0 else "BEARISH" if d < 0 else "NEUTRAL"


def _candle_patterns(df):
    if df is None or len(df) < 3:
        return []
    try:
        d = df.iloc[-5:]
        out = []
        last = d.iloc[-1]
        rng = max(float(last.high - last.low), 1e-9)
        body = abs(float(last.close - last.open))
        upper = float(last.high - max(last.open, last.close))
        lower = float(min(last.open, last.close) - last.low)
        if body / rng <= 0.12:
            out.append("DOJI")
        if lower >= 2.0 * max(body, 1e-9) and upper <= 0.5 * max(body, 1e-9):
            out.append("HAMMER")
        if upper >= 2.0 * max(body, 1e-9) and lower <= 0.5 * max(body, 1e-9):
            out.append("SHOOTING_STAR")
        prev = d.iloc[-2]
        prev_body_hi = max(float(prev.open), float(prev.close))
        prev_body_lo = min(float(prev.open), float(prev.close))
        cur_body_hi = max(float(last.open), float(last.close))
        cur_body_lo = min(float(last.open), float(last.close))
        if prev.close < prev.open and last.close > last.open and cur_body_lo <= prev_body_lo and cur_body_hi >= prev_body_hi:
            out.append("BULLISH_ENGULFING")
        if prev.close > prev.open and last.close < last.open and cur_body_hi >= prev_body_hi and cur_body_lo <= prev_body_lo:
            out.append("BEARISH_ENGULFING")
        if len(d) >= 3:
            two = d.iloc[-3]
            if float(last.high) < float(two.high) and float(last.low) > float(two.low):
                out.append("INSIDE_BAR")
        return out
    except Exception:
        return []


def _chart_pattern_hints(df):
    """Conservative pattern hints; never treated as deterministic entry permission."""
    if df is None or len(df) < 30:
        return []
    try:
        close = df["close"].astype(float).to_numpy()
        if not np.all(np.isfinite(close)):
            return []
        w = close[-30:]
        x = np.arange(len(w), dtype=float)
        slope = np.polyfit(x, w, 1)[0]
        spread = max(float(w.max() - w.min()), 1e-9)
        normalized = abs(slope) * len(w) / spread
        hints = []
        if normalized < 0.18 and spread / max(abs(w.mean()), 1e-9) < 0.01:
            hints.append("COMPRESSION_RANGE")
        elif normalized > 0.35:
            hints.append("DIRECTIONAL_CHANNEL_CANDIDATE")
        # crude double-top/bottom hint: two comparable extrema separated by a trough/peak.
        a, b = float(w[:10].max()), float(w[15:25].max())
        if abs(a - b) <= 0.15 * spread and float(w[10:15].min()) < min(a, b) - 0.25 * spread:
            hints.append("DOUBLE_TOP_CANDIDATE")
        a, b = float(w[:10].min()), float(w[15:25].min())
        if abs(a - b) <= 0.15 * spread and float(w[10:15].max()) > max(a, b) + 0.25 * spread:
            hints.append("DOUBLE_BOTTOM_CANDIDATE")
        return hints
    except Exception:
        return []


class TechnicalBrain:
    name = "market_technical"

    def run(self, views: dict, ms=None, frames=None, symbol: str = "") -> BrainFinding:
        reasons, warnings, facts = [], [], {}
        tf_rows = {}
        bull = bear = 0.0
        for tf, v in views.items():
            try:
                bias = 0.5 * (float(v.get("ema_trend", 0)) + float(v.get("struct_trend", 0)))
                bull += max(0.0, bias)
                bear += max(0.0, -bias)
                tf_rows[tf] = {
                    "close": round(float(v.get("close", 0.0)), 4),
                    "trend": _dir_label(int(v.get("struct_trend", 0))),
                    "ema_trend": _dir_label(int(v.get("ema_trend", 0))),
                    "rsi": round(float(v.get("rsi", 50.0)), 2),
                    "macd_hist": round(float(v.get("macd_hist", 0.0)), 6),
                    "adx": round(float(v.get("adx", 0.0)), 2),
                    "adx_band": v.get("adx_band", ""),
                    "volatility": v.get("vol_regime", ""),
                    "struct_regime": v.get("struct_regime", ""),
                    "sweep": v.get("sweep"),
                    "fvg_count": len(v.get("fvg") or []),
                    "order_block_count": len(v.get("ob") or []),
                }
            except Exception as ex:
                warnings.append(f"{tf}: {ex}")
        facts["timeframes"] = tf_rows
        frame_map = frames or {}
        raw = frame_map.get("M15") if frame_map.get("M15") is not None else frame_map.get("M5")
        facts["candlestick_patterns"] = _candle_patterns(raw)
        facts["chart_pattern_hints"] = _chart_pattern_hints(raw)
        facts["symbol"] = symbol
        top = max((v for v in tf_rows.values()), key=lambda x: x.get("adx", 0), default={})
        d = 1 if bull > bear + 0.25 else -1 if bear > bull + 0.25 else 0
        # Score evidence, not predicted return.  The weighting follows the source blueprint's confluence idea.
        htf = [tf_rows.get(x, {}).get("trend") for x in ("W1", "D1", "H4")]
        alignment = sum(1 for x in htf if x == _dir_label(d)) if d else 0
        zones = sum(row.get("fvg_count", 0) + row.get("order_block_count", 0) for row in tf_rows.values())
        score = min(100.0, 35.0 + 12.0 * alignment + min(20.0, zones * 2.5)
                     + (10.0 if facts["candlestick_patterns"] else 0.0)
                     + (8.0 if facts["chart_pattern_hints"] else 0.0))
        reasons.append(f"HTF directional alignment {alignment}/3" if htf else "HTF data not available")
        if facts["candlestick_patterns"]:
            reasons.append("latest candle patterns: " + ", ".join(facts["candlestick_patterns"]))
        if facts["chart_pattern_hints"]:
            reasons.append("chart-pattern hints: " + ", ".join(facts["chart_pattern_hints"]))
        return BrainFinding(self.name, "OK" if score >= 65 else "WARN", score, score / 100.0, d,
                            f"technical structure {_dir_label(d)}", reasons, warnings, facts)
