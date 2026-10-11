"""Realistic execution costs for research: slippage grows with spread, volatility, session and event proximity;
missing spreads are modelled per session. Stress tests multiply both."""
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np


@dataclass
class CostModel:
    base_slippage: float = 0.02
    spread_frac: float = 0.25
    atr_frac: float = 0.02
    session_mult: dict = field(default_factory=lambda: {"NY_OPEN": 1.8, "LONDON_OPEN": 1.5, "OVERLAP": 1.4, "ASIA": 1.2,
                                                        "TOKYO": 1.2, "SYDNEY": 1.6, "OFF": 1.6, "NY_CLOSE": 1.3})
    spread_session_mult: dict = field(default_factory=lambda: {"ASIA": 1.4, "TOKYO": 1.4, "SYDNEY": 2.0, "OFF": 2.0, "NY_CLOSE": 1.3})
    event_mult: float = 2.5
    spread_mult: float = 1.0               # stress multipliers
    slippage_mult: float = 1.0

    def spread(self, observed, default, phase):
        if observed is not None and observed == observed and observed > 0:
            return observed * self.spread_mult
        return default * self.spread_session_mult.get(phase, 1.0) * self.spread_mult

    def slippage(self, spread, atr, phase, near_event=False):
        s = (self.base_slippage + self.spread_frac * spread + self.atr_frac * atr) * self.session_mult.get(phase, 1.0)
        return s * (self.event_mult if near_event else 1.0) * self.slippage_mult

    def save(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls, path):
        p = Path(path)
        if not p.exists():
            return None
        d = json.loads(p.read_text())
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def calibrate_from_trades(closes, min_trades=30):
    """Fit the slippage model from YOUR fills (trades.jsonl close records carry slippage, spread and session).
    slippage ~ base + spread_frac * spread, then per-session multipliers from medians. Returns (CostModel, report)
    or (None, reason) when there is not enough data."""
    rows = [(abs(t["slippage"]), t["spread"], t.get("session") or "OFF") for t in closes
            if isinstance(t.get("slippage"), (int, float)) and isinstance(t.get("spread"), (int, float))]
    if len(rows) < min_trades:
        return None, f"only {len(rows)} fills with slippage data (need {min_trades})"
    slip = np.array([r[0] for r in rows])
    spr = np.array([r[1] for r in rows])
    X = np.column_stack([np.ones(len(rows)), spr])
    coef, *_ = np.linalg.lstsq(X, slip, rcond=None)
    base, frac = max(0.0, float(coef[0])), max(0.0, float(coef[1]))
    overall = float(np.median(slip)) or 1e-9
    sess = {}
    for name in {r[2] for r in rows}:
        s = [r[0] for r in rows if r[2] == name]
        if len(s) >= 8:
            sess[name] = round(float(np.median(s)) / overall, 3)
    cm = CostModel(base_slippage=round(base, 4), spread_frac=round(frac, 4), atr_frac=0.0,
                   session_mult={**CostModel().session_mult, **sess})
    pred = np.array([cm.slippage(r[1], 0.0, r[2]) for r in rows])
    return cm, {"n": len(rows), "base": cm.base_slippage, "spread_frac": cm.spread_frac, "sessions_fitted": sess,
                "median_abs_error": round(float(np.median(np.abs(pred - slip))), 4)}
