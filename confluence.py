"""Contradiction detection. Every independent piece of evidence votes on direction with a weight; instead of
summing points into a BUY, a trade facing heavy opposing evidence is downgraded or turned into WAIT."""
from dataclasses import dataclass, field

from analysis import bias

EFFECT = {"bullish": 1.0, "bearish": -1.0, "neutral": 0.0}


@dataclass
class Conflict:
    index: float = 0.0                # 0 = everything agrees, 1 = everything opposes
    verdict: str = "ALIGNED"          # ALIGNED | MIXED | CONFLICTING
    supporting: list = field(default_factory=list)
    opposing: list = field(default_factory=list)
    factors: dict = field(default_factory=dict)


def factors_for(views, roles, lmap, news, aux=None):
    f = {}
    if views.get(roles["macro"]):
        f["macro_" + roles["macro"]] = (bias(views[roles["macro"]]), 0.8)
    if views.get(roles["structural"]):
        f["structure_" + roles["structural"]] = (bias(views[roles["structural"]]), 1.0)
    if lmap is not None:
        f["liquidity_draw"] = (float(lmap.draw()), 0.5)
    if news is not None and getattr(news, "status", "") == "OK" and news.confidence > 0:
        f["news_bias"] = (max(-1.0, min(1.0, news.bias)), 1.0 * news.confidence)
        for i, drv in enumerate(getattr(news, "drivers", [])[:6]):
            eff = EFFECT.get(str(drv.get("effect", "neutral")).lower(), 0.0)
            if eff:
                f[f"driver:{str(drv.get('driver', i))[:30]}"] = (eff, 0.3 * news.confidence)
    if news is not None and getattr(news, "event_bias", None) is not None:
        f["event_surprise"] = (news.event_bias, 0.8)
    rc = getattr(news, "rules_confidence", 0.0) if news is not None else 0.0
    if rc > 0:                                         # headline rules: caution-only (see assess)
        f["headline_rules"] = (max(-1.0, min(1.0, news.rules_bias)), 0.6 * rc)
    for name, val in (aux or {}).items():            # e.g. {"usd_proxy": -1} meaning bearish USD -> bullish gold
        f[name] = (float(val), 0.7)
    return f


def assess(d, views, roles, lmap, news, cfg, aux=None):
    pc = cfg.policy
    fs = factors_for(views, roles, lmap, news, aux)
    sup = opp = 0.0
    s_names, o_names = [], []
    for name, (direction, w) in fs.items():
        x = direction * d
        if name == "headline_rules" and x > 0:
            continue                                   # deterministic headlines may add caution, never confidence
        if x > 0.1:
            sup += w * x
            s_names.append(name)
        elif x < -0.1:
            opp += w * -x
            o_names.append(name)
    idx = opp / (opp + sup) if opp + sup > 0 else 0.0
    verdict = "CONFLICTING" if idx >= pc.conflict_reject else "MIXED" if idx >= pc.conflict_reduce else "ALIGNED"
    return Conflict(round(idx, 3), verdict, s_names, o_names, {k: round(v[0], 2) for k, v in fs.items()})
