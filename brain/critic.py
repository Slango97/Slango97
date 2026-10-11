"""Trade Critic / Debate Brain (architecture document, brain 08): before the Chief Trader decides, a second brain
attacks the candidate. Deterministic objections with severities; the Chief turns them into NO_TRADE / REDUCE."""
from __future__ import annotations

from .models import BrainFinding


class TradeCriticBrain:
    name = "trade_critic"

    def run(self, decision, technical: BrainFinding, news: BrainFinding, intermarket: BrainFinding, ms=None, cfg=None) -> BrainFinding:
        if not getattr(decision, "is_entry", False):
            return BrainFinding(self.name, "OK", 100.0, 1.0, 0, "no candidate to criticise")
        d = getattr(decision, "direction", 0) or 0
        obj = []                                            # (severity, objection)
        htf = getattr(decision, "htf_bias", 0.0) or 0.0
        if htf * d < 0:
            obj.append(("major", f"higher-timeframe bias {htf:+.2f} opposes the trade"))
        elif abs(htf) < 0.15:
            obj.append(("minor", "D1/H4 bias is neutral"))
        near = min((b["r"] for b in (getattr(decision, "barriers", None) or []) if b.get("r") is not None and b["r"] > 0), default=None)
        if near is not None and near < 1.2:
            obj.append(("major", f"TP1 sits close to structure ({near:.2f}R away)"))
        label = news.facts.get("event_label") or ""
        if label in ("INITIAL_REACTION", "ABSORPTION", "PRE_EVENT"):
            obj.append(("fatal", f"news window {label}: the documents forbid new entries"))
        elif label == "POST_EVENT":
            obj.append(("minor", "post-news volatility may still be elevated"))
        if news.facts.get("medium_upcoming"):
            obj.append(("minor", "medium-impact news upcoming: " + news.facts["medium_upcoming"][0]))
        if intermarket.bias and intermarket.bias != d and intermarket.confidence >= 0.4:
            obj.append(("major", "DXY / US10Y / intermarket context opposes the trade"))
        if technical.bias and news.bias and news.bias != d and news.confidence >= 0.4:
            obj.append(("minor", "news direction disagrees with the technical setup"))
        if not (getattr(decision, "confirmed", True) and getattr(decision, "triggered", True)):
            obj.append(("major", "lower-timeframe confirmation is weak"))
        n = {s: sum(1 for x in obj if x[0] == s) for s in ("fatal", "major", "minor")}
        score = max(0.0, 100.0 - 45 * n["fatal"] - 20 * n["major"] - 6 * n["minor"])
        status = "BLOCK" if n["fatal"] or n["major"] >= 2 else "REDUCE" if n["major"] == 1 else "OK"
        return BrainFinding(self.name, status, score, score / 100.0, 0,
                            "no material objections" if not obj else f"{len(obj)} objection(s): {obj[0][1]}",
                            [f"[{s}] {t}" for s, t in obj], [], {"objections": obj, **n})
