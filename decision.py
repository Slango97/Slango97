"""Two stages:
1. generate()      - expensive, parameter-light: every style x direction is checked against the timeframe
                     hierarchy, classified, scored and given setup-aware levels. No policy filtering.
2. apply_policy()  - cheap, switchable gates (regime, market quality, confirmation, edge book, contradictions,
                     news, score, calibrated expectancy) and sizing. WAIT and HALT are normal outcomes.
The backtester caches stage 1 so stage 2 can be optimized and ablated quickly."""
from dataclasses import asdict, dataclass, field

from config import LADDER
from confluence import assess
import levels as levels_mod
from pricefmt import round_price
import mtf
from setups import classify

ENTRY_ACTIONS = ("BUY", "SELL")
# V4 decision priority (spec section 4); blockers are reported in this order
PRIORITY = ("DATA", "BROKER", "RISK_GATE", "MARKET_QUALITY", "NEWS", "REGIME", "HTF_CONFLICT", "NO_ZONE", "CONFIRMATION",
            "TRIGGER", "TARGET_BLOCKED", "STOP_WIDTH", "SPREAD", "RR", "EDGE", "CONFLICT", "SCORE", "OPPOSING_SETUP",
            "M1_REFINEMENT", "EXPECTANCY", "META_FILTER", "QUALITY", "INTEL", "OPEN_POSITION", "EXECUTION")


class Rejection(str):
    """A rejection message that also carries a machine-readable code (still behaves like the V3 string)."""
    def __new__(cls, msg, code="", style="", direction=0, extra=None):
        o = super().__new__(cls, msg)
        o.code, o.style, o.direction, o.extra = code, style, direction, extra or {}
        return o

    def __reduce__(self):
        return (Rejection, (str(self), self.code, self.style, self.direction, self.extra))


def level_code(msg):
    m = msg.lower()
    return ("TARGET_BLOCKED" if ("blocks" in m or "no room" in m or "unrealistic" in m) else "STOP_WIDTH" if "too wide" in m
            else "SPREAD" if "spread" in m else "RR" if "reward/risk" in m else "DATA")


@dataclass
class SignalScore:
    mtf: float = 0.0
    structure: float = 0.0
    zone: float = 0.0
    location: float = 0.0
    liquidity: float = 0.0
    momentum: float = 0.0
    room: float = 0.0
    volatility: float = 0.0
    session: float = 0.0
    news: float = 0.0

    @property
    def technical(self):
        return max(0.0, min(100.0, self.mtf + self.structure + self.zone + self.location + self.liquidity
                            + self.momentum + self.room + self.volatility + self.session))


@dataclass
class Decision:
    action: str = "WAIT"              # BUY | SELL | WAIT | HALT
    wait_reason: str = ""             # NO_SETUP | REGIME | MARKET_QUALITY | CONFIRMATION | CONFLICT | NEWS | SCORE | EXPECTANCY | EDGE
    direction: int = 0
    style: str = ""
    setup: str = ""
    tf: str = ""
    entry: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    tp1: float = None                  # V5.1 documents: first target (50% close, then stop to entry + spread)
    score: float = 0.0
    technical: float = 0.0
    rr: float = 0.0
    risk_pct: float = 0.0
    risk_tier: str = "fixed"
    target_kind: str = ""
    session: str = ""
    vol_regime: str = ""
    regime: str = ""
    market_quality: float = 100.0
    conflict: float = 0.0
    conflict_verdict: str = ""
    confirmed: bool = False
    triggered: bool = False
    p_win: float = None
    meta_p: float = None
    exp_r: float = None
    calib_conf: float = None
    pro_quality_score: float = 0.0
    strong_evidence_domains: int = 0
    required_evidence_domains: int = 0
    adaptive_risk_mult: float = 1.0
    policy_risk_mult: float = 1.0
    htf_bias: float = 0.0
    components: dict = field(default_factory=dict)
    tags: list = field(default_factory=list)
    reasons: list = field(default_factory=list)
    blockers: list = field(default_factory=list)       # [{"code", "message"}] in priority order
    roles: dict = field(default_factory=dict)
    alignment: list = field(default_factory=list)
    mtf_conflicts: list = field(default_factory=list)
    barriers: list = field(default_factory=list)
    invalidation: dict = field(default_factory=dict)
    candidates: list = field(default_factory=list)     # every evaluated candidate (with its blockers)
    rejections: list = field(default_factory=list)     # candidates that failed structurally (Rejection strings)

    @property
    def is_entry(self):
        return self.action in ENTRY_ACTIONS


def _sweep_for(vc, vz, d):
    if vc["sweep"] and vc["sweep"]["dir"] == d:
        return vc["sweep"]
    if vz["sweep"] and vz["sweep"]["dir"] == d:
        return vz["sweep"]
    return None


def _score(S, roles, views, d, price, m, ms, cfg, lv):
    st = cfg.strategy
    sc = SignalScore(mtf=m.score)
    vz, vc = views[roles["zone"]], views[roles["confirm"]]
    why = list(m.notes)
    sc.structure = 0.6 * st.w_structure * vz["struct_trend"] * d
    ev = vc["last_event"]
    if ev and ev["dir"] == d and vc["df_len"] - 1 - ev["idx"] <= st.structure_lookback_bars and ev["quality"] >= st.min_bos_quality:
        sc.structure += 0.4 * st.w_structure * ev["quality"] / 100
        why.append(f"{ev['type']} q{ev['quality']} on {roles['confirm']}")
    z = m.zone
    sc.zone = min(st.w_zone, st.w_zone * (0.4 + 0.6 * z["quality"] / 100) + (3 if z["overlap"] else 0))
    why.append("order block + FVG" if z["overlap"] else f"order block q{z['ob']['quality']}" if z["ob"] else f"FVG q{z['fvg']['quality']}")
    fib, loc = vz["fib"], 0.0
    if fib and fib["hi"] > fib["lo"]:
        rng = fib["hi"] - fib["lo"]
        retr = (fib["hi"] - price) / rng if d > 0 else (price - fib["lo"]) / rng
        loc = 1.0 if 0.62 <= retr <= 0.79 else 0.6 if (0.5 <= retr < 0.62 or 0.79 < retr <= 1.0) else 0.2 if retr >= 0.38 else -0.4
        if loc == 1.0:
            why.append("OTE")
    tl = vz["tl_up"] if d > 0 else vz["tl_dn"]
    if tl and abs(price - tl["value"]) <= 0.5 * vz["atr"]:
        loc += 0.2
        why.append(f"{tl['touches']}-touch trendline")
    sc.location = st.w_location * max(-0.4, min(1.0, loc))
    sw = _sweep_for(vc, vz, d)
    if sw:
        sc.liquidity = st.w_liquidity * sw["weight"] / 10
        why.append(f"{sw['kind']} swept")
    mom = 0.0
    if vc["adx_band"] in ("DEVELOPING", "STRONG", "VERY_STRONG") and vc["macd_hist"] * d > 0:
        mom += 0.4
    if (d > 0 and vc["rsi"] < 70) or (d < 0 and vc["rsi"] > 30):
        mom += 0.3
    if vc["ms"]["momentum"] == "EXPANDING" and vc["ms"]["momentum_dir"] == d:
        mom += 0.3
    if cfg.features.doc_rules:                      # V5.1: the documents' momentum triggers (evidence, capped)
        sig = [n for n, k in (("RSI divergence", "rsi_div"), ("RSI " + ("reclaimed 30" if d > 0 else "lost 70"), "rsi_reclaim"),
                              ("Stochastic exhaustion cross", "stoch_x")) if vc.get(k) == d]
        if sig:
            mom = min(1.0, mom + 0.25 * len(sig))
            why.append(" + ".join(sig))
    sc.momentum = st.w_momentum * mom
    sc.room = st.w_room * (1.0 if lv["rr"] >= S.min_rr * 1.5 else 0.6)
    sc.volatility = st.w_volatility * {"NORMAL": 1.0, "HIGH": 0.6, "LOW": 0.4, "EXTREME": 0.0}[vz["vol_regime"]]
    sc.session = min(st.w_session, cfg.sessions.phase_points.get(ms.session, 0) * st.w_session / 5)
    return sc, why


def generate(ms, price, cfg, aux=None):
    """All structurally valid candidates (no policy gates applied). Returns (candidates, rejections)."""
    cands, rejected = [], []
    views = ms.views
    regime = getattr(ms, "regime", None)
    lmap = getattr(ms, "liquidity", None)
    for d in (1, -1):
        for name, S in cfg.strategy.styles.items():
            tag = f"{name}/{'L' if d > 0 else 'S'}"
            R = lambda msg, code, **ex: Rejection(f"{tag}: {msg}", code, name, d, ex)
            if name == "scalp" and ms.session not in cfg.strategy.scalp_sessions:
                rejected.append(R(f"scalping not allowed in {ms.session}", "REGIME"))
                continue
            roles = cfg.hierarchy.roles[name]
            m = mtf.evaluate(views, roles, d, price, cfg)
            if not m.ok:
                rejected.append(R(m.reason, m.code or "DATA", alignment=m.alignment, conflicts=m.conflicts))
                continue
            vz, vc, vs = views[roles["zone"]], views[roles["confirm"]], views[roles["structural"]]
            if cfg.strategy.block_extreme_vol and vz["vol_regime"] == "EXTREME":
                rejected.append(R(f"extreme volatility on {roles['zone']}", "MARKET_QUALITY"))
                continue
            setup, tags = classify(d, price, m.zone, vz, vc, vs, cfg)
            sw = _sweep_for(vc, vz, d)
            ev = vz["last_event"] if vz["last_event"] and vz["last_event"]["dir"] == d else None
            macro = (views.get("D1") or {}).get("macro_levels") if cfg.features.macro_context else None
            lv, why_not = levels_mod.compute(setup, d, price, m.zone, vz, vs, lmap, regime.label if regime else "",
                                             S, cfg, ms.spread, sw, ev, macro)
            if lv is None:
                rejected.append(R(why_not, level_code(why_not), setup=setup, alignment=m.alignment))
                continue
            sc, why = _score(S, roles, views, d, price, m, ms, cfg, lv)
            conf = assess(d, views, roles, lmap, getattr(ms, "news", None), cfg, aux)
            tech = round(sc.technical, 1)
            cands.append(Decision(
                action="WAIT", direction=d, style=name, setup=setup, tf=S.tf, entry=price, sl=lv["sl"], tp=lv["tp"],
                score=tech, technical=tech, rr=lv["rr"], target_kind=lv["target_kind"], session=ms.session,
                vol_regime=vz["vol_regime"], regime=regime.label if regime else "",
                market_quality=ms.micro.score if getattr(ms, "micro", None) else 100.0,
                conflict=conf.index, conflict_verdict=conf.verdict, confirmed=m.confirmed, triggered=m.triggered,
                htf_bias=round(m.structural, 2), components={k: round(x, 1) for k, x in asdict(sc).items()},
                tags=tags, reasons=why + lv["notes"] + [f"target: {lv['target_kind']}"]
                + ([f"opposed by {', '.join(conf.opposing)}"] if conf.opposing else []),
                roles=dict(roles), alignment=m.alignment, mtf_conflicts=m.conflicts, barriers=lv.get("barriers", []),
                invalidation=lv.get("invalidation", {})))
    return cands, rejected


def _tier(c, ms, cfg):
    sz = cfg.sizing
    if sz.mode == "fixed":
        return "fixed", 1.0
    order = ["weak", "normal", "exceptional"]
    if c.meta_p is not None and cfg.policy.use_meta_model:
        t = 2 if c.meta_p >= 0.6 else 1 if c.meta_p >= cfg.policy.meta_min_prob else 0
    elif c.exp_r is not None and cfg.policy.use_calibration:
        t = 2 if c.exp_r >= sz.exp_r_exceptional else 1 if c.exp_r >= sz.exp_r_normal else 0
    else:
        t = 2 if c.score >= sz.score_exceptional else 1 if c.score >= sz.score_normal else 0
    mq, reg = getattr(ms, "micro", None), getattr(ms, "regime", None)
    t -= int(mq is not None and mq.band == "CAUTIOUS")
    t -= int(c.conflict >= cfg.policy.conflict_reduce)
    t -= int(reg is not None and reg.size_down)
    t = max(0, t)
    return order[t], sz.tiers[order[t]]


def _block(c, code, message):
    c.blockers.append({"code": code, "message": message})


def _sorted_blockers(c):
    c.blockers.sort(key=lambda b: PRIORITY.index(b["code"]) if b["code"] in PRIORITY else len(PRIORITY))
    return c.blockers



def _adaptive_risk_multiplier(ms, c, cfg, caution):
    """Never increase risk. Reduce exposure when execution quality/regime/event risk deteriorate."""
    if not getattr(cfg.risk, "adaptive_quality_risk", True):
        return 1.0
    mult = 1.0
    mq = getattr(ms, "micro", None)
    if mq is not None and getattr(mq, "band", "") == "CAUTIOUS":
        mult = min(mult, cfg.risk.cautious_risk_mult)
    if getattr(c, "vol_regime", "") in ("HIGH", "EXTREME"):
        mult = min(mult, cfg.risk.high_vol_risk_mult)
    reg = getattr(ms, "regime", None)
    if reg is not None and getattr(reg, "label", "") == "EXPANSION":
        mult = min(mult, cfg.risk.expansion_risk_mult)
    if caution:
        mult = min(mult, cfg.risk.news_caution_risk_mult)
    return max(0.1, min(1.0, float(mult)))

def tp1_level(c, cfg):
    """TP1 at the nearest structural level between entry and target that gives at least tp1_min_r (documents:
    'TP1 at the nearest structural liquidity block', R:R at least 1:1); otherwise tp1_default_r; None = single target."""
    st = cfg.strategy
    risk = abs(c.entry - c.sl)
    if risk <= 0 or not c.tp or not c.direction:
        return None
    for b in sorted((b for b in (c.barriers or []) if b.get("r") is not None and b["r"] > 0), key=lambda b: b["r"]):
        if b["r"] >= c.rr - 0.25:
            break
        if b["r"] >= st.tp1_min_r:
            return round_price(b["price"] - c.direction * 0.05 * risk, risk)
    if c.rr >= st.tp1_default_r + 0.5:
        return round_price(c.entry + c.direction * st.tp1_default_r * risk, risk)
    return None


def apply_policy(cands, ms, news, cfg, exclude=(), calibration=None, edge_book=None, rejected=(), meta_model=None):
    """Evaluate every candidate against every gate and record ALL blockers (not just the first), in the spec's
    priority order: market quality / news / regime (global) -> HTF + MTF alignment -> trigger -> edge ->
    contradictions -> score -> quant expected R -> optional LLM intelligence (lowest)."""
    pc, nc = cfg.policy, cfg.news
    reg, mq = getattr(ms, "regime", None), getattr(ms, "micro", None)
    base = dict(session=ms.session, regime=reg.label if reg else "", market_quality=mq.score if mq else 100.0,
                rejections=list(rejected))
    cands = [c for c in cands if (c.style, c.direction) not in exclude]
    glob = None
    if mq is not None and pc.use_micro_gate and mq.score < cfg.micro.no_trade_below:
        glob = ("WAIT", "MARKET_QUALITY", f"market quality {mq.score:.0f}: " + ", ".join(mq.flags))
    elif news is not None and getattr(news, "blackout", False):
        glob = ("HALT", "NEWS", f"event blackout: {news.blackout_reason}")
    elif reg is not None and pc.use_regime_filter and reg.no_trade:
        glob = ("HALT", "REGIME", f"regime {reg.label}: stand down" + (f" ({'; '.join(reg.reasons)})" if reg.reasons else ""))
    if glob:
        for c in cands:
            _block(c, glob[1], glob[2])
        reasons = [glob[2]] + (reg.reasons if glob[1] == "REGIME" and reg else [])
        return Decision(action=glob[0], wait_reason=glob[1], reasons=reasons, candidates=cands, **base)

    news_ok = news is not None and nc.enabled and getattr(news, "status", "") == "OK" and news.confidence >= nc.min_confidence
    caution = news is not None and cfg.features.event_caution and getattr(news, "event_phase", "") == "CAUTION"
    for c in cands:
        policy_mult = 1.0
        if pc.use_regime_filter and reg is not None:
            allowed = [t for t in c.tags if t in reg.allowed]
            if not allowed:
                _block(c, "REGIME", f"{c.setup} not allowed in {reg.label}")
            elif c.setup not in reg.allowed:
                c.setup = next((s for s in pc.setup_priority if s in allowed), allowed[0])
        # Moderate confirmation policy: one missing lower-TF confirmation is a risk/score penalty;
        # absence of BOTH confirmation and trigger remains a hard no-trade condition.
        if pc.use_hierarchy_confirmation:
            if not c.confirmed and not c.triggered:
                _block(c, "CONFIRMATION", "both confirmation and entry trigger are missing")
            elif not c.confirmed:
                policy_mult = min(policy_mult, pc.soft_confirmation_risk_mult)
                c.score = max(0.0, c.score - 4.0)
                c.reasons.append("soft confirmation penalty: no BOS/CHoCH/sweep confirmation")
            elif not c.triggered:
                policy_mult = min(policy_mult, pc.soft_confirmation_risk_mult)
                c.score = max(0.0, c.score - 4.0)
                c.reasons.append("soft trigger penalty: entry trigger not fully confirmed")
        if pc.use_edge_book and edge_book is not None and not edge_book.allowed(c.setup):
            _block(c, "EDGE", f"{c.setup} has negative out-of-sample expectancy in the edge book")
        if pc.use_contradiction:
            if c.conflict >= pc.conflict_reject:
                _block(c, "CONFLICT", f"conflicting conditions (index {c.conflict:.2f})")
            elif c.conflict >= pc.conflict_reduce:
                policy_mult = min(policy_mult, pc.soft_conflict_risk_mult)
                c.reasons.append(f"soft conflict reduction: index {c.conflict:.2f}")
        if getattr(cfg.policy, "use_pro_quality_gate", True):
            try:
                from pro_quality import assess as assess_quality
                qr = assess_quality(c, ms, cfg)
                c.pro_quality_score = qr.score
                c.strong_evidence_domains = qr.strong_domains
                c.required_evidence_domains = qr.required_domains
                if not qr.passed:
                    # Hard veto only for genuinely deficient evidence. Borderline quality is traded smaller.
                    if qr.score < pc.min_hard_quality_score or qr.strong_domains < 2:
                        _block(c, "QUALITY", "pro-quality hard floor: " + "; ".join(qr.reasons))
                    else:
                        policy_mult = min(policy_mult, pc.soft_quality_risk_mult)
                        c.reasons.append("soft quality reduction: " + "; ".join(qr.reasons[:2]))
                c.reasons.append(f"pro-quality {qr.score:.0f}/100 ({qr.strong_domains}/{qr.required_domains} domains)")
            except Exception as ex:
                _block(c, "QUALITY", f"pro-quality evaluation failed: {ex}")
        if nc.enabled and news is not None and c.style == "scalp" and (getattr(news, "impact", "low") == "high" or caution):
            _block(c, "NEWS", "no scalps around high-impact news" + (" (caution window)" if caution else ""))
        nb = news.bias * news.confidence * c.direction if news_ok else 0.0
        if news_ok and nb <= -nc.veto_against and news.confidence >= 0.75:
            _block(c, "INTEL", f"strong news/intelligence opposition ({nb:+.2f})")
        elif news_ok and nb <= -nc.soft_opposition_threshold:
            policy_mult = min(policy_mult, pc.soft_news_risk_mult)
            c.reasons.append(f"soft news opposition reduction ({nb:+.2f})")
        # V5.9: the score BONUS uses only evidence allowed to add (an unproven LLM view is excluded); opposition
        # and vetoes above still use the full view because they can only reduce risk.
        sb, sc_ = getattr(news, "support_bias", None), getattr(news, "support_confidence", None)
        nb_sup = (sb * sc_ * c.direction if sb is not None and sc_ is not None else nb) if news_ok else 0.0
        c.components["news"] = round(min(cfg.strategy.news_max_bonus, cfg.strategy.news_max_bonus * nb_sup), 1) if nb_sup >= nc.support_from else 0.0
        c.score = round(min(100.0, c.technical + c.components["news"]), 1)
        if calibration is not None:
            pr = calibration.predict(c.score, c.setup, c.regime, c.session)
            c.p_win, c.exp_r, c.calib_conf = pr["p_win"], pr["exp_r"], pr["confidence"]
            if pc.use_calibration and c.exp_r < pc.min_expected_r:
                _block(c, "EXPECTANCY", f"expected {c.exp_r:+.2f}R below {pc.min_expected_r}R")
        if meta_model is not None:
            from metalabel import features as _feat
            c.meta_p = round(meta_model.predict(_feat(c)), 4)
            if pc.use_meta_model and c.meta_p < pc.meta_min_prob:
                _block(c, "META_FILTER", f"learned filter: P(target first) {c.meta_p:.0%} below {pc.meta_min_prob:.0%}")
        if cfg.features.doc_rules:
            c.tp1 = tp1_level(c, cfg)
        c.policy_risk_mult = round(policy_mult, 3)
        _sorted_blockers(c)
    viable = [c for c in cands if not c.blockers]
    use_er = pc.use_calibration and calibration is not None
    if not viable:
        if not cands:
            return Decision(action="WAIT", wait_reason="NO_SETUP", reasons=[str(r) for r in rejected][:6] or ["no valid setup"], **base)
        top = max(cands, key=lambda c: c.technical)
        notes = [f"{c.style}/{'L' if c.direction > 0 else 'S'} {c.setup}: " + "; ".join(b["message"] for b in c.blockers[:3]) for c in cands]
        notes += [str(r) for r in rejected]
        return Decision(action="WAIT", wait_reason=top.blockers[0]["code"], reasons=notes[:8], candidates=cands, **base)
    viable.sort(key=lambda c: ((c.exp_r if use_er else c.score), LADDER.index(c.tf)), reverse=True)
    best = viable[0]
    best.candidates, best.rejections = cands, list(rejected)
    opp = max((c.score for c in viable if c.direction != best.direction), default=0.0)
    if best.score < cfg.strategy.min_score:
        _block(best, "SCORE", f"score {best.score} below {cfg.strategy.min_score}")
        best.action, best.wait_reason = "WAIT", "SCORE"
        best.reasons.append(f"score {best.score} below {cfg.strategy.min_score}")
        best.reasons.extend([str(r) for r in rejected[:3]])
        return best
    if best.score - opp < cfg.strategy.min_score_gap:
        _block(best, "OPPOSING_SETUP", f"opposite setup scores {opp}")
        best.action, best.wait_reason = "WAIT", "CONFLICT"
        best.reasons.append(f"opposite setup scores {opp}")
        return best
    if news is not None and getattr(news, "trade_veto", False) and getattr(news, "status", "") == "OK":
        _block(best, "INTEL", "market intelligence veto: " + getattr(news, "summary", ""))
        return Decision(action="HALT", wait_reason="NEWS", reasons=["market intelligence veto: " + getattr(news, "summary", "")],
                        candidates=cands, **base)
    tier, mult = _tier(best, ms, cfg)
    if caution and cfg.sizing.mode == "tiered" and tier != "weak":
        tier = {"exceptional": "normal", "normal": "weak"}[tier]
        mult = cfg.sizing.tiers[tier]
    S = cfg.strategy.styles[best.style]
    adaptive = _adaptive_risk_multiplier(ms, best, cfg, caution)
    best.risk_tier, best.risk_pct = tier, round(cfg.risk.risk_per_trade_pct * S.risk_mult * mult * adaptive * best.policy_risk_mult, 4)
    best.adaptive_risk_mult = round(adaptive, 3)
    if adaptive < 0.999:
        best.reasons.append(f"adaptive risk multiplier {adaptive:.0%}")
    best.action = "BUY" if best.direction > 0 else "SELL"
    return best


def decide(ms, price, news, cfg, exclude=(), calibration=None, edge_book=None, aux=None, meta_model=None):
    cands, rejected = generate(ms, price, cfg, aux)
    return apply_policy(cands, ms, news, cfg, exclude, calibration, edge_book, rejected, meta_model)
