"""News / Macro Brain built on the existing deterministic NewsEngine/NewsContext."""
from __future__ import annotations

from .models import BrainFinding


class NewsMacroBrain:
    name = "news_macro"

    def run(self, ctx, symbol: str = "") -> BrainFinding:
        if ctx is None:
            return BrainFinding(self.name, "WARN", 0, 0, 0, "no news context")
        facts = {"event_label": getattr(ctx, "event_label", ""), "medium_upcoming": list(getattr(ctx, "medium_upcoming", []) or []), 
            "status": getattr(ctx, "status", "UNKNOWN"),
            "bias": float(getattr(ctx, "bias", 0.0) or 0.0),
            "confidence": float(getattr(ctx, "confidence", 0.0) or 0.0),
            "impact": getattr(ctx, "impact", "low"),
            "event_phase": getattr(ctx, "event_phase", "UNKNOWN"),
            "blackout": bool(getattr(ctx, "blackout", False)),
            "trade_veto": bool(getattr(ctx, "trade_veto", False)),
            "calendar_ok": bool(getattr(ctx, "calendar_ok", False)),
            "calendar_state": getattr(ctx, "calendar_state", "UNAVAILABLE"),
            "calendar_age_h": getattr(ctx, "calendar_cache_age_h", None),
            "drivers": list(getattr(ctx, "drivers", []) or []),
            "contradictions": list(getattr(ctx, "contradictions", []) or []),
            "expected_move": getattr(ctx, "expected_move", None),
            "summary": getattr(ctx, "summary", ""),
            "next_event": getattr(ctx, "next_event", ""),
            "headlines": list(getattr(ctx, "headlines", []) or [])[:8],
            "blackout_reason": getattr(ctx, "blackout_reason", ""),
            "calendar_required": bool(getattr(ctx, "calendar_required", False)),
        }
        reasons = []
        warnings = []
        if facts["calendar_ok"]:
            reasons.append(f"calendar {facts['calendar_state'].lower()}")
        else:
            warnings.append("calendar is not currently eligible for fresh entries")
        if facts["event_phase"] in ("PRE_EVENT", "BLACKOUT", "CAUTION") or facts["blackout"]:
            warnings.append(f"event phase {facts['event_phase']}")
        if facts["contradictions"]:
            warnings.append("news contradictions present")
        score = 70.0 if facts["calendar_ok"] else 20.0
        score += 10.0 if facts["status"] == "OK" else 0.0
        score -= 15.0 if facts["blackout"] else 0.0
        score = max(0.0, min(100.0, score))
        bias = 1 if facts["bias"] > 0.15 else -1 if facts["bias"] < -0.15 else 0
        return BrainFinding(self.name, "OK" if score >= 60 else "WARN", score,
                            max(0.0, min(1.0, facts["confidence"])), bias,
                            f"news state {facts['status']}", reasons, warnings, facts)
