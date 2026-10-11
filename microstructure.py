"""Market-quality score (0-100) from execution conditions. The same scoring runs live (1-minute bars plus
sampled spreads) and in the backtest (bar data), so the gate behaves the same in both."""
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np


@dataclass
class MarketQuality:
    score: float = 100.0
    band: str = "NORMAL"            # NORMAL | CAUTIOUS | NO_TRADE | ABNORMAL
    components: dict = field(default_factory=dict)
    flags: list = field(default_factory=list)
    range_ratio: float = 1.0        # recent bar range vs baseline (used for post-event stabilization)
    spread_ratio: float = 1.0


def band_for(score, mc):
    if score >= mc.normal_min:
        return "NORMAL"
    if score >= mc.no_trade_below:
        return "CAUTIOUS"
    if score >= mc.abnormal_below:
        return "NO_TRADE"
    return "ABNORMAL"


def score_market(mc, spread_now, spread_baseline, o, h, l, c, tv, max_spread=None, slippage_ratio=None, stale=False):
    """o/h/l/c/tv: arrays of recent closed bars (oldest first, at least ~20). Returns MarketQuality."""
    comp, flags = {}, []
    n = len(c)
    if n < 10:
        return MarketQuality(score=50.0, band=band_for(50.0, mc), flags=["insufficient bars"])
    base = slice(max(0, n - 1 - mc.baseline_bars), n - 1)
    rng = np.asarray(h) - np.asarray(l)
    ref = max(float(np.median(rng[base])) * 1.5, 1e-9)

    ratio = spread_now / spread_baseline if spread_baseline > 0 else 1.0
    pen = 60 if ratio >= mc.spread_ratio_extreme else 35 if ratio >= mc.spread_ratio_bad else 15 if ratio >= mc.spread_ratio_warn else 0
    if max_spread is not None and spread_now > max_spread:
        pen = max(pen, 40)
        flags.append("spread above maximum")
    comp["spread"] = -pen
    if pen:
        flags.append(f"spread {ratio:.1f}x normal")

    tvb = float(np.median(np.asarray(tv)[base])) or 1.0
    rate = float(np.mean(np.asarray(tv)[-2:])) / tvb
    comp["tick_activity"] = -25 if rate < mc.tick_rate_low else -20 if rate > mc.tick_rate_high else 0
    if comp["tick_activity"]:
        flags.append("illiquid / dead tape" if rate < mc.tick_rate_low else "frenzied tick activity")

    vel = rng[-1] / ref
    comp["velocity"] = -30 if vel >= mc.velocity_bad_atr else -15 if vel >= mc.velocity_warn_atr else 0
    if comp["velocity"]:
        flags.append(f"fast candle ({vel:.1f}x)")

    tva = np.asarray(tv, dtype=float)[base]
    z = (tv[-1] - tva.mean()) / tva.std() if tva.std() > 0 else 0.0
    comp["volume_spike"] = -15 if z >= mc.volume_z_bad else 0
    gap = abs(o[-1] - c[-2]) / ref
    comp["gap"] = -20 if gap >= mc.gap_atr else 0
    if comp["gap"]:
        flags.append("price gap")
    comp["slippage"] = -15 if slippage_ratio is not None and slippage_ratio >= mc.slippage_ratio_bad else 0
    comp["stale"] = -50 if stale else 0
    score = max(0.0, 100.0 + sum(comp.values()))
    rr = float(np.mean(rng[-5:]) / max(np.median(rng[base]), 1e-9))
    return MarketQuality(score=score, band=band_for(score, mc), components=comp, flags=flags,
                         range_ratio=rr, spread_ratio=ratio)


class TickMonitor:
    """Live sampler: keeps an hour of spreads and evaluates market quality from 1-minute bars."""

    def __init__(self, cfg, client):
        self.cfg, self.c = cfg, client
        self.spreads = deque(maxlen=3600)
        self.slippages = deque(maxlen=50)
        self._bars, self._bars_at = None, 0.0
        self.last = MarketQuality()

    def sample(self):
        t = self.c.tick()
        if t is not None and t.ask > t.bid > 0:
            self.spreads.append((time.time(), float(t.ask - t.bid)))

    def record_slippage(self, slippage_price):
        self.slippages.append(abs(slippage_price))

    def spread_baseline(self):
        recent = [s for ts, s in self.spreads if time.time() - ts <= 3600]
        return float(np.median(recent)) if len(recent) >= 10 else (recent[-1] if recent else 0.0)

    def evaluate(self):
        if time.time() - self._bars_at > 20 or self._bars is None:
            try:
                self._bars, _ = self.c.rates("M1", 90)
                self._bars_at = time.time()
            except Exception:
                self._bars = None
        t = self.c.tick()
        if self._bars is None or t is None:
            self.last = MarketQuality(score=30.0, band=band_for(30.0, self.cfg.micro), flags=["no 1-minute data"])
            return self.last
        b = self._bars
        slip_ratio = None
        if len(self.slippages) >= 5:
            typical = float(np.median(self.slippages)) or 1e-9
            slip_ratio = float(np.mean(list(self.slippages)[-3:])) / typical
        stale = self.c.tick_age_s() > self.cfg.execution.max_tick_age_s
        self.last = score_market(self.cfg.micro, float(t.ask - t.bid), self.spread_baseline() or float(t.ask - t.bid),
                                 b["open"].values, b["high"].values, b["low"].values, b["close"].values,
                                 b["tick_volume"].values, self.cfg.execution.max_spread_points * self.c.symbol_info().point,
                                 slip_ratio, stale)
        return self.last

    def vol_probe(self):
        return self.last.range_ratio, self.last.spread_ratio
