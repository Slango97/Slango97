"""Structured Decision Record for every candidate setup, traded or rejected. Telegram text and audit logs are
generated deterministically from this record, never from free-form LLM narrative."""
import uuid
from datetime import datetime, timezone

from pricefmt import round_price

from decision import PRIORITY

WEIGHTS = {"mtf": "w_mtf", "structure": "w_structure", "zone": "w_zone", "location": "w_location",
           "liquidity": "w_liquidity", "momentum": "w_momentum", "room": "w_room", "volatility": "w_volatility",
           "session": "w_session"}


def evidence(c, cfg):
    pos = list(c.reasons)
    neg = []
    for k, attr in WEIGHTS.items():
        w = getattr(cfg.strategy, attr)
        v = c.components.get(k, 0.0)
        if w and v < 0.3 * w:
            neg.append(f"weak {k} ({v:.1f}/{w:.0f})")
    neg += list(c.mtf_conflicts)
    if c.conflict_verdict in ("MIXED", "CONFLICTING"):
        neg.append(f"evidence conflict index {c.conflict:.2f}")
    if c.vol_regime in ("HIGH", "EXTREME"):
        neg.append(f"{c.vol_regime.lower()} volatility")
    return pos, neg


def candidate_record(c, cfg, selected=False):
    pos, neg = evidence(c, cfg)
    return {"direction": "BUY" if c.direction > 0 else "SELL", "style": c.style, "setup": c.setup, "tags": list(c.tags),
            "status": "SELECTED" if selected else ("REJECTED" if c.blockers else "VIABLE"),
            "score": c.score, "technical": c.technical, "components": dict(c.components),
            "pro_quality_score": getattr(c, "pro_quality_score", 0.0),
            "strong_evidence_domains": getattr(c, "strong_evidence_domains", 0),
            "required_evidence_domains": getattr(c, "required_evidence_domains", 0),
            "adaptive_risk_mult": getattr(c, "adaptive_risk_mult", 1.0), "policy_risk_mult": getattr(c, "policy_risk_mult", 1.0), "rr": c.rr,
            "entry": round_price(c.entry, abs(c.entry - c.sl)), "sl": round_price(c.sl, abs(c.entry - c.sl)),
            "tp": round_price(c.tp, abs(c.entry - c.sl)), "target_kind": c.target_kind,
            "confidence": c.calib_conf, "p_win": c.p_win, "expected_r": c.exp_r, "meta_p": c.meta_p, "conflict": c.conflict,
            "mtf_alignment": list(c.alignment), "mtf_conflicts": list(c.mtf_conflicts), "barriers": list(c.barriers),
            "invalidation": dict(c.invalidation), "positive_evidence": pos, "negative_evidence": neg,
            "blockers": list(c.blockers)}


def _build(dec, ms, news, cfg, symbol="XAUUSD", stage="DECISION", risk=None, refine=None, execution=None, gate=None):
    """dec: final Decision; ms: MarketState (or None); news: NewsContext (or None)."""
    reg, mq, lm = getattr(ms, "regime", None), getattr(ms, "micro", None), getattr(ms, "liquidity", None)
    d1 = (getattr(ms, "views", None) or {}).get("D1") or {}
    cands = []
    seen = set()
    for c in list(getattr(dec, "candidates", []) or []) + ([dec] if dec.is_entry else []):
        if id(c) in seen:
            continue
        seen.add(id(c))
        cands.append(candidate_record(c, cfg, selected=dec.is_entry and c is dec))
    rejections = [{"candidate": str(r).split(":")[0], "code": getattr(r, "code", ""), "message": str(r).split(": ", 1)[-1]}
                  for r in getattr(dec, "rejections", [])]
    action = dec.action
    blockers = sorted({b["code"] for c in cands for b in c["blockers"]} | ({dec.wait_reason} if dec.wait_reason and not dec.is_entry else set()),
                      key=lambda x: PRIORITY.index(x) if x in PRIORITY else 99)
    return {
        "id": uuid.uuid4().hex[:12], "time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "symbol": symbol,
        "stage": stage, "final_action": action, "wait_reason": dec.wait_reason, "blocker_codes": blockers,
        "regime": {"label": reg.label, "direction": reg.direction, "confidence": reg.confidence, "reasons": reg.reasons} if reg else None,
        "session": getattr(ms, "session", dec.session),
        "market_quality": {"score": round(mq.score), "band": mq.band, "flags": mq.flags} if mq else None,
        "news": None if news is None else {"status": news.status, "event_phase": getattr(news, "event_phase", ""),
                                            "blackout": news.blackout, "reason": news.blackout_reason, "bias": round(news.bias, 2),
                                            "confidence": round(news.confidence, 2), "risk_level": getattr(news, "risk_level", ""),
                                            "trade_veto": getattr(news, "trade_veto", False), "next_event": news.next_event,
                                            "summary": news.summary if news.status == "OK" else "",
                                            "intel_id": getattr(news, "intel_id", ""), "llm_bias": getattr(news, "llm_bias", 0.0),
                                            "llm_influence": getattr(news, "llm_influence", "none")},
        "liquidity": lm.narrative() if lm is not None else "",
        "macro": d1.get("macro_levels", {}), "drivers": getattr(ms, "aux", None),
        "gate": gate, "risk_authorization": risk, "m1_refinement": refine, "execution": execution,
        "candidates": cands, "structural_rejections": rejections[:12], "reasons": list(dec.reasons)[:8]}


def build(dec, ms, news, cfg, symbol="XAUUSD", stage="DECISION", risk=None, refine=None, execution=None, gate=None):
    """Decision record + (V5.1) the documents' pre-flight checklist for entries."""
    rec = _build(dec, ms, news, cfg, symbol, stage, risk, refine, execution, gate)
    if getattr(cfg.features, "doc_rules", False) and getattr(dec, "is_entry", False):
        try:
            from preflight import checklist, summary
            items = checklist(dec, ms, news, cfg)
            rec["preflight"] = {"summary": summary(items), "items": items}
            rec.setdefault("tp1", getattr(dec, "tp1", None))
        except Exception as ex:
            rec["preflight"] = {"error": str(ex)}
    return rec
