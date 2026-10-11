import sys
import tempfile
import time
import types
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT), str(ROOT / "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

from config import Config  # noqa: E402

NS = types.SimpleNamespace
TUE_10 = datetime(2025, 3, 4, 10, 0, tzinfo=timezone.utc)        # a Tuesday inside London hours


def tmpdir():
    return tempfile.mkdtemp(prefix="xauagent-")


def candles(rows, start="2025-03-03 00:00", freq="5min"):
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df["tick_volume"] = 100.0
    df.index = pd.date_range(start, periods=len(rows), freq=freq)
    return df


def zigzag(points, per_leg=8, wick=0.15, start="2025-03-03 00:00", freq="5min"):
    """Candles that walk linearly through the given turning points."""
    path = []
    for a, b in zip(points[:-1], points[1:]):
        path += list(np.linspace(a, b, per_leg, endpoint=False))
    path.append(points[-1])
    rows = []
    for i in range(1, len(path)):
        o, c = path[i - 1], path[i]
        rows.append((o, max(o, c) + wick, min(o, c) - wick, c))
    return candles(rows, start, freq)


def cfg(**kw):
    return Config()


def fake_view(tf="M15", price=2000.0, atr=3.0, bias=1, **over):
    v = {"tf": tf, "time": pd.Timestamp("2025-03-04 10:00"), "df_len": 300, "close": price, "atr": atr,
         "atr_pct": 0.5, "atr_ratio": 1.0, "vol_regime": "NORMAL", "rsi": 55.0, "rsi_slope": 0.0, "adx": 26.0,
         "adx_band": "STRONG", "macd_hist": 0.5 * bias, "macd_slope": 0.1 * bias, "ema20": price - bias * 1.0,
         "ema_slope": 0.1 * bias, "bb_pct": 0.5, "vwap": price, "vwap_day": price, "ema_trend": bias,
         "struct_trend": bias, "struct_regime": "BULLISH" if bias > 0 else "BEARISH", "last_event": None,
         "labels": [], "swing_highs": [(250, price + 8 * atr)], "swing_lows": [(250, price - 8 * atr)],
         "fvg": [], "ob": [], "sweep": None, "sr": [], "eq_levels": [], "key_levels": {}, "fib": None,
         "tl_up": None, "tl_dn": None, "data_issues": [], "disp_events": [], "bb_width_pct": 0.5, "all_events": [],
         "session_ctx": {},
         "ms": {"trend": "BULLISH", "trend_dir": bias, "trend_strength": 80, "volatility": "NORMAL",
                "momentum": "EXPANDING", "momentum_dir": bias, "structure": "RANGE", "liquidity": "NONE",
                "market_quality": 80}}
    v.update(over)
    return v


def bull_ob(price, atr, quality=80, tests=0, idx=280):
    return {"type": "bull", "top": price + 0.3 * atr, "bottom": price - 0.8 * atr, "idx": idx, "disp": idx + 2,
            "tests": tests, "mitigation_pct": 0.1, "disp_strength": 2.0, "bos_confirmed": True, "vol_ratio": 1.5,
            "quality": quality}


def long_setup_views(price=2000.0, atr=3.0):
    """A clean bullish hierarchy for the DAY style: D1 macro and H4 structure bullish, price inside a bullish H1
    order block at the 0.7 retracement, an M15 BOS + sell-side sweep confirming, and an M5 momentum trigger."""
    views = {tf: fake_view(tf, price, atr * (1 + i), 1) for i, tf in enumerate(("M5", "M15", "H1", "H4", "D1", "W1"))}
    h1 = views["H1"]
    a1 = h1["atr"]
    h1["ob"] = [bull_ob(price, a1)]
    h1["fib"] = {"leg": "up", "hi": price + 7 * a1, "lo": price - 3 * a1, "eq": price + 2 * a1, "levels": {}}
    m15 = views["M15"]
    m15["sweep"] = {"dir": 1, "level": price - atr, "kind": "PDL", "weight": 10, "bar": 297, "extreme": price - 1.1 * atr}
    m15["last_event"] = {"type": "BOS", "dir": 1, "idx": 290, "level": price - 2 * atr, "quality": 80,
                         "kind": "strong", "holds": True}
    return views


def pos(ticket=1, d=1, entry=2000.0, sl=1994.0, tp=2012.0, vol=0.10, price=None, comment="da74 r6.00", age_s=600):
    return NS(ticket=ticket, type=0 if d > 0 else 1, magic=560001, volume=vol, price_open=entry, sl=sl, tp=tp,
              price_current=price if price is not None else entry, comment=comment, time=time.time() - age_s,
              symbol="XAUUSD")


class FakeClient:
    """Minimal client double for risk / reconciliation / manager tests (no MT5 needed)."""

    def __init__(self, equity=10000.0, positions=None, now=TUE_10):
        self.equity, self.margin, self.margin_free_override = equity, 0.0, None
        self._positions = list(positions or [])
        self.now = now
        self.read_only = False
        self.mods, self.closes, self.summaries = [], [], {}
        self.bid, self.ask = 2000.0, 2000.25
        self.spread_pts = 25.0
        self.point = 0.01
        self.symbol = "XAUUSD"

    def account(self):
        free = self.margin_free_override if self.margin_free_override is not None else self.equity - self.margin
        return NS(equity=self.equity, balance=self.equity, margin=self.margin, margin_free=free)

    def symbol_info(self):
        return NS(point=0.01, volume_min=0.01, volume_step=0.01, volume_max=50.0, digits=2)

    def utc_now(self):
        return self.now

    def spread_points(self):
        return self.spread_pts

    def spread_price(self):
        return self.spread_pts * 0.01

    def min_stop_distance(self):
        return 0.3

    def tick(self):
        return NS(bid=self.bid, ask=self.ask, time=int(time.time()))

    def tick_age_s(self):
        return 1.0

    def calc_loss_per_lot(self, d, entry, sl):
        return abs(entry - sl) * 100.0

    def calc_loss_per_lot_for(self, symbol, d, entry, sl):
        return abs(entry - sl) * 100.0

    def calc_margin(self, d, lots, price):
        return lots * 100 * price / 200

    def positions(self):
        return list(self._positions)

    def pos_age_hours(self, p):
        return (time.time() - p.time) / 3600

    def deal_summary(self, pid):
        return self.summaries.get(pid)

    def modify_sltp(self, p, sl=None, tp=None):
        self.mods.append(sl)
        if sl is not None:
            p.sl = sl
        return True

    def close(self, p, volume=None):
        self.closes.append((p.ticket, volume))
        if volume is None or volume >= p.volume:
            self._positions = [q for q in self._positions if q.ticket != p.ticket]
        else:
            p.volume = round(p.volume - volume, 8)
        return True


def utc(h=10, d=4, mo=3, y=2025):
    return datetime(y, mo, d, h, 0, tzinfo=timezone.utc)
