"""Quantitative Brain: mathematics, expectancy, risk/reward and evidence quality."""
from __future__ import annotations

from .models import BrainFinding


class QuantitativeBrain:
    name = "quantitative"

    def run(self, decision=None, account=None, positions=None, cfg=None, market_quality: float = 100.0) -> BrainFinding:
        reasons, warnings, facts = [], [], {}
        if decision is None:
            return BrainFinding(self.name, "WARN", 0, 0, 0, "no decision supplied")
        facts.update({
            "score": float(getattr(decision, "score", 0.0) or 0.0),
            "technical_score": float(getattr(decision, "technical", 0.0) or 0.0),
            "rr": float(getattr(decision, "rr", 0.0) or 0.0),
            "risk_pct": float(getattr(decision, "risk_pct", 0.0) or 0.0),
            "p_win": getattr(decision, "p_win", None),
            "expected_r": getattr(decision, "exp_r", None),
            "market_quality": float(market_quality or 0.0),
            "open_positions": len(positions or []),
        })
        rr = facts["rr"]
        p = facts["p_win"]
        er = facts["expected_r"]
        if er is None and p is not None:
            try:
                p = float(p)
                er = p * rr - (1.0 - p)
                facts["derived_expected_r"] = er
            except Exception:
                er = None
        if rr > 0:
            reasons.append(f"structural R:R {rr:.2f}")
        if p is not None:
            reasons.append(f"calibrated/model P(win) {float(p):.1%}")
        if er is not None:
            reasons.append(f"expected value {float(er):+.2f}R")
        if not decision.is_entry:
            score = min(100.0, 50.0 + 0.5 * facts["score"])
        else:
            score = min(100.0, 35.0 + min(30.0, max(0.0, rr - 1.0) * 15.0) + 0.25 * facts["score"]
                        + (10.0 if er is not None and er > 0 else 0.0)
                        + (5.0 if market_quality >= 70 else 0.0))
        if account is not None:
            try:
                facts["equity"] = float(getattr(account, "equity", 0.0) or 0.0)
            except Exception:
                pass
        return BrainFinding(self.name, "OK" if score >= 65 else "WARN", score, score / 100.0,
                            1 if decision.direction > 0 else -1 if decision.direction < 0 else 0,
                            "quantitative profile acceptable" if score >= 65 else "quantitative profile weak",
                            reasons, warnings, facts)
