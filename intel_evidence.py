"""V5.9 evidence gate for AI news intelligence (read side; intel_evaluation.py writes the evidence).

Unproven LLM views keep every RISK-REDUCING power (strong opposition veto, soft risk reduction, trade_veto) but
may not ADD score to a setup. Only a fresh PROVEN verdict - direction calls significantly better than a coin on
scored out-of-sample outcomes, and vetoes not shown to block winners - unlocks the score bonus.
A HARMFUL verdict is reported loudly; switching the LLM off stays the operator's decision.
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

_CACHE = {}


def load(path):
    p = Path(path)
    try:
        mt = p.stat().st_mtime
    except OSError:
        return None
    hit = _CACHE.get(str(p))
    if hit and hit[0] == mt:
        return hit[1]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        data = None
    _CACHE[str(p)] = (mt, data)
    return data


def news_verdict(cfg, now=None):
    """(verdict, reason): PROVEN | UNPROVEN | HARMFUL | MISSING | STALE."""
    ic = cfg.intel
    data = load(ic.evidence_file)
    if not isinstance(data, dict):
        return "MISSING", "no AI-news evidence yet"
    try:
        gen = datetime.fromisoformat(str(data["generated_at"]).replace("Z", "+00:00"))
        gen = gen if gen.tzinfo else gen.replace(tzinfo=timezone.utc)
        age = ((now or time.time()) - gen.timestamp()) / 86400
    except Exception:
        return "STALE", "evidence has no valid timestamp"
    if age > ic.evidence_max_age_days:
        return "STALE", f"evidence is {age:.0f} days old"
    n = (data.get("news_intel") or {})
    return str(n.get("verdict", "MISSING")), str(n.get("summary", ""))


def llm_influence(cfg):
    """'full' when the gate is off or the evidence is PROVEN, else 'reduce_only'."""
    if not getattr(cfg.intel, "evidence_gate", True):
        return "full"
    return "full" if news_verdict(cfg)[0] == "PROVEN" else "reduce_only"
