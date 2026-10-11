
"""V4.3 pro-level trade quality gate.

The gate does not predict price. It enforces evidence diversity so a high composite score cannot
be produced by one strong factor while structure/zone/trigger evidence is weak. It is deterministic,
auditable, and bounded; an LLM cannot bypass it.
"""
from dataclasses import dataclass

@dataclass
class QualityResult:
    passed: bool
    score: float
    strong_domains: int
    required_domains: int
    reasons: list
    evidence: dict


def _share(c, key, maximum):
    try:
        return max(0.0, min(1.0, float(c.components.get(key, 0.0)) / float(maximum or 1.0)))
    except Exception:
        return 0.0


def assess(c, ms, cfg):
    st = cfg.strategy
    maxw = {"mtf": st.w_mtf, "structure": st.w_structure, "zone": st.w_zone,
            "location": st.w_location, "liquidity": st.w_liquidity, "momentum": st.w_momentum,
            "room": st.w_room, "volatility": st.w_volatility, "session": st.w_session}
    ev = {k: round(_share(c, k, v), 3) for k, v in maxw.items()}
    core = ["mtf", "structure", "zone", "location", "liquidity", "momentum"]
    strong = sum(ev[k] >= 0.50 for k in core)
    mq = float(getattr(c, "market_quality", 100.0))
    required = 4 if mq < cfg.micro.normal_min else 3
    reasons = []
    if ev["mtf"] < 0.45:
        reasons.append("insufficient multi-timeframe alignment")
    if ev["structure"] < 0.45:
        reasons.append("weak structural evidence")
    if ev["zone"] < 0.45:
        reasons.append("weak entry-zone quality")

    setup = c.setup
    if setup in ("LIQUIDITY_SWEEP_REVERSAL",):
        if ev["liquidity"] < 0.40 and ev["structure"] < 0.70:
            reasons.append("reversal lacks sweep/structure confirmation")
    elif setup in ("BREAKOUT_RETEST", "BOS_PULLBACK"):
        if ev["structure"] < 0.60 or ev["momentum"] < 0.45:
            reasons.append("breakout lacks strong structure/momentum")
    elif setup == "MEAN_REVERSION":
        regime = str(getattr(c, "regime", ""))
        if regime not in ("RANGE", "COMPRESSION"):
            reasons.append("mean-reversion setup outside range/compression regime")
        if ev["location"] < 0.50 or ev["room"] < 0.40:
            reasons.append("mean-reversion location/room is weak")
    else:
        if ev["momentum"] < 0.40:
            reasons.append("trend setup lacks momentum confirmation")

    # Weighted evidence score, explicitly bounded and separate from the legacy trading score.
    w = {"mtf": 0.25, "structure": 0.20, "zone": 0.18, "location": 0.10, "liquidity": 0.10,
         "momentum": 0.10, "room": 0.04, "volatility": 0.02, "session": 0.01}
    q = 100.0 * sum(ev[k] * w[k] for k in w)
    floor = float(getattr(cfg.policy, "min_pro_quality_score", 62.0))
    if getattr(c, "vol_regime", "") in ("HIGH", "EXTREME"):
        floor = max(floor, float(getattr(cfg.policy, "high_vol_quality_floor", 68.0)))
    if q < floor:
        reasons.append(f"pro-quality score {q:.1f} below floor {floor:.1f}")
    if strong < required:
        reasons.append(f"only {strong} independent evidence domains meet quality threshold; need {required}")
    if mq < cfg.micro.abnormal_below:
        reasons.append("market quality is abnormal")

    return QualityResult(not reasons, round(q, 1), strong, required, reasons[:6], ev)
