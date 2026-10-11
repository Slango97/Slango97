"""Risk Guardian Brain: summary/veto layer over the deterministic V4 RiskEngine.

It can only reduce permission. It never increases risk or changes a broker calculation.
"""
from __future__ import annotations

from .models import BrainFinding
from .safety import clamp_risk_multiplier


class RiskGuardianBrain:
    name = "risk_guardian"

    def run(self, decision, safety, account, positions, risk_engine=None, regime=None, cfg=None) -> BrainFinding:
        reasons, warnings, facts = [], [], {}
        risk_mult = 1.0
        if not safety.allowed:
            reasons.extend(safety.reasons)
            risk_mult = 0.0
        if decision.is_entry and account is None:
            reasons.append("account snapshot unavailable")
            risk_mult = 0.0
        if decision.is_entry:
            try:
                facts["requested_risk_pct"] = float(decision.risk_pct or 0.0)
            except Exception:
                facts["requested_risk_pct"] = 0.0
        if risk_engine is not None and account is not None:
            try:
                facts["daily_loss_pct"] = 100.0 * (risk_engine.state.risk["day_start_equity"] - account.equity) / max(risk_engine.state.risk["day_start_equity"], 1e-9)
                facts["drawdown_pct"] = float(risk_engine.drawdown_pct())
            except Exception as ex:
                warnings.append(f"risk snapshot unavailable: {ex}")
        if regime is not None:
            facts["regime"] = getattr(regime, "label", "")
            if getattr(regime, "label", "") in ("EXPANSION", "WEAK_TREND"):
                risk_mult = min(risk_mult, 0.8)
                reasons.append(f"regime {regime.label}: cautious risk")
        if facts.get("daily_loss_pct", 0.0) >= float(getattr(cfg.risk, "max_daily_loss_pct", 2.0)):
            risk_mult = 0.0
            reasons.append("daily loss cap reached")
        facts["risk_multiplier"] = clamp_risk_multiplier(risk_mult)
        score = 100.0 * risk_mult
        if score >= 75 and not reasons:
            reasons.append("deterministic risk gate remains authoritative and clear")
        return BrainFinding(self.name, "OK" if score >= 70 else "BLOCK", score, score / 100.0, 0,
                            "risk guardian clear" if score >= 70 else "risk guardian blocked",
                            reasons, warnings, facts)
