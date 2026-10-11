"""Chief Trader Brain: bounded synthesis of specialist findings."""
from __future__ import annotations

from .models import BrainFinding, V5Decision
from .safety import clamp_risk_multiplier


class ChiefTraderBrain:
    name = "chief_trader"

    def run(self, *, symbol: str, decision, connection: BrainFinding, technical: BrainFinding,
            quant: BrainFinding, news: BrainFinding, intermarket: BrainFinding,
            opportunity: BrainFinding, risk: BrainFinding, critic: BrainFinding | None = None) -> V5Decision:
        contradictions = []
        blockers = []
        if connection.score < 70:
            blockers.append("connection/broker health below safe threshold")
        if news.facts.get("blackout"):
            blockers.append("news event blackout")
        elif news.facts.get("trade_veto") and float(news.facts.get("confidence", 0.0) or 0.0) >= 0.75:
            blockers.append("strong news veto")
        elif news.facts.get("trade_veto"):
            contradictions.append("advisory news veto downgraded to contradiction")
        if news.facts.get("calendar_required") and not news.facts.get("calendar_ok", False):
            blockers.append("news/calendar not eligible")
        if risk.score < 70:
            blockers.extend(risk.reasons[:3] or ["risk guardian block"])
        if technical.bias and news.bias and technical.bias != news.bias:
            contradictions.append("technical/news directional disagreement")
        if technical.bias and intermarket.bias and technical.bias != intermarket.bias:
            contradictions.append("technical/intermarket directional disagreement")
        if critic is not None:
            if critic.facts.get("fatal"):
                blockers.extend([r for r in critic.reasons if r.startswith("[fatal]")][:2])
            elif critic.facts.get("major", 0) >= 2:
                blockers.append("trade critic: " + "; ".join(r for r in critic.reasons if r.startswith("[major]"))[:200])
            elif critic.facts.get("major", 0) == 1:
                contradictions.append("trade critic: " + next(r for r in critic.reasons if r.startswith("[major]")))
        # The chief cannot turn a WAIT/HALT into a trade.
        action = decision.action
        if action not in ("BUY", "SELL"):
            permission = "NO_TRADE"
        elif blockers:
            permission = "BLOCK"
        elif decision.score < getattr(self, "min_score", 62.0):
            permission = "NO_TRADE"
        elif contradictions:
            permission = "REDUCE"
        else:
            permission = "APPROVE"
        guardian_mult = float(risk.facts.get("risk_multiplier", 1.0) or 0.0)
        mult = min(guardian_mult, 1.0 if permission == "APPROVE" else 0.75 if permission == "REDUCE" else 0.0)
        conf = 0.45 * (technical.score / 100) + 0.25 * (quant.score / 100) + 0.20 * (opportunity.score / 100) + 0.10 * (news.score / 100)
        conf = max(0.0, min(1.0, conf))
        reasons = list((opportunity.reasons or [])[:4]) + list((risk.reasons or [])[:3])
        return V5Decision(action=action, permission=permission, confidence=conf,
                          setup_quality=opportunity.score, risk_multiplier=clamp_risk_multiplier(mult),
                          symbol=symbol, direction=int(getattr(decision, "direction", 0) or 0),
                          reasons=reasons[:8], contradictions=contradictions[:6], blockers=blockers[:6],
                          facts={"setup": decision.setup, "style": decision.style, "rr": decision.rr,
                                 "entry": decision.entry, "sl": decision.sl, "tp": decision.tp,
                                 "deterministic_score": decision.score})
