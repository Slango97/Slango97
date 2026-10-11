"""Opportunity/Setup Brain: transforms the deterministic decision into a human-readable opportunity state."""
from __future__ import annotations

from .models import BrainFinding


class OpportunityBrain:
    name = "opportunity"

    def run(self, decision, technical: BrainFinding, news: BrainFinding, quant: BrainFinding) -> BrainFinding:
        score = float(getattr(decision, "score", 0.0) or 0.0)
        reasons = list(getattr(decision, "reasons", []) or [])
        warnings = []
        blockers = list(getattr(decision, "blockers", []) or [])
        candidates = list(getattr(decision, "candidates", []) or [])
        candidate_scores = [float(getattr(c, "score", 0.0) or 0.0) for c in candidates]
        best_candidate_score = max(candidate_scores, default=0.0)
        if blockers:
            warnings.extend(str(b.get("message", b)) if isinstance(b, dict) else str(b) for b in blockers[:4])
        if technical.bias and news.bias and technical.bias != news.bias:
            warnings.append("technical and news directional biases disagree")
        base = max(score, best_candidate_score)
        quality = min(100.0, max(0.0, 0.50 * base + 0.30 * technical.score + 0.20 * quant.score))
        if decision.is_entry and not blockers and score >= 65:
            headline = f"trade opportunity candidate: {decision.action}"
        else:
            headline = "no executable opportunity"
        return BrainFinding(self.name, "OK" if quality >= 65 else "WARN", quality,
                            min(1.0, quality / 100.0), int(getattr(decision, "direction", 0) or 0),
                            headline, reasons[:8], warnings[:8],
                            {"decision_action": decision.action, "setup": decision.setup, "style": decision.style,
                             "entry": decision.entry, "sl": decision.sl, "tp": decision.tp, "rr": decision.rr,
                             "blockers": blockers, "candidate_count": len(candidates),
                             "best_candidate_score": best_candidate_score})
