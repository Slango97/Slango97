"""Timeframe hierarchy: macro direction -> structural direction -> setup zone -> confirmation -> entry.
Each role has a fixed job, and a lower timeframe can never override a higher one."""
from dataclasses import dataclass, field

from analysis import bias


def in_zone(price, z, atr, pad):
    return z["bottom"] - pad * atr <= price <= z["top"] + pad * atr


def best_zone(v, d, price, pad):
    want = "bull" if d > 0 else "bear"
    obs = [z for z in v["ob"] if z["type"] == want and in_zone(price, z, v["atr"], pad)]
    fvgs = [z for z in v["fvg"] if z["type"] == want and in_zone(price, z, v["atr"], pad)]
    if not obs and not fvgs:
        return None
    ob = max(obs, key=lambda z: (z["quality"], z["idx"])) if obs else None
    fvg = max(fvgs, key=lambda z: (z["quality"], z["idx"])) if fvgs else None
    base = ob or fvg
    overlap = bool(ob and fvg and ob["bottom"] <= fvg["top"] and fvg["bottom"] <= ob["top"])
    return {"top": base["top"], "bottom": base["bottom"], "ob": ob, "fvg": fvg, "overlap": overlap,
            "quality": base["quality"], "tf": v["tf"]}


@dataclass
class MTFResult:
    ok: bool
    reason: str = ""
    score: float = 0.0
    macro: float = 0.0
    structural: float = 0.0
    zone: dict = None
    confirmed: bool = False
    triggered: bool = False
    notes: list = field(default_factory=list)
    alignment: list = field(default_factory=list)   # per role: tf, bias, structure, state
    conflicts: list = field(default_factory=list)
    code: str = ""


def evaluate(views, roles, d, price, cfg):
    hc, st = cfg.hierarchy, cfg.strategy
    need = [views.get(roles[r]) for r in ("macro", "structural", "zone", "confirm", "entry")]
    if any(v is None for v in need):
        return MTFResult(False, "missing timeframe data", code="DATA")
    vm, vs, vz, vc, ve = need
    align, conflicts = [], []
    for role, v in zip(("macro", "structural", "zone", "confirm", "entry"), need):
        b = bias(v) * d
        state = "aligned" if b > 0 else "conflict" if b < 0 else "neutral"
        ev = v["last_event"]
        recent = ev is not None and v["df_len"] - 1 - ev["idx"] <= 15
        align.append({"role": role, "tf": roles[role], "bias": round(b, 2), "structure": v["struct_regime"],
                      "event": f"{'bull' if ev['dir'] > 0 else 'bear'} {ev['type']}" if recent else "", "state": state})
        if state == "conflict" and role in ("macro", "structural", "confirm"):
            conflicts.append(f"{roles[role]} ({role}) {'bearish' if d > 0 else 'bullish'} against the trade")
        if recent and ev["dir"] == -d and role in ("confirm", "entry"):
            conflicts.append(f"{roles[role]} fresh {'bearish' if d > 0 else 'bullish'} {ev['type']}")
    sb = bias(vs) * d
    if sb <= -hc.structural_veto:
        return MTFResult(False, f"{roles['structural']} structure opposes the trade", alignment=align, conflicts=conflicts, code="HTF_CONFLICT")
    mb = bias(vm) * d
    zone = best_zone(vz, d, price, st.zone_match_pad_atr)
    zone_pts = 4.0
    if zone is None and hc.zone_fallback_to_confirm and roles["confirm"] != roles["zone"]:
        zone = best_zone(vc, d, price, st.zone_match_pad_atr)
        zone_pts = 2.0
    if zone is None:
        return MTFResult(False, f"price not in an aligned {roles['zone']} zone", alignment=align, conflicts=conflicts, code="NO_ZONE")

    L = vc["df_len"] - 1
    ev = vc["last_event"]
    conf_ev = bool(ev and ev["dir"] == d and L - ev["idx"] <= hc.confirm_lookback and ev["quality"] >= st.min_bos_quality)
    sw = vc["sweep"]
    conf_sw = bool(sw and sw["dir"] == d and L - sw["bar"] <= 12)
    conf_disp = any(de["dir"] == d and L - de["idx"] <= 10 for de in vc.get("disp_events", []))
    confirmed = conf_ev or conf_sw or conf_disp

    Le = ve["df_len"] - 1
    ee = ve["last_event"]
    trig_ev = bool(ee and ee["dir"] == d and Le - ee["idx"] <= hc.trigger_lookback)
    trig_mom = ve["ms"]["momentum_dir"] == d and (ve["close"] - ve["ema20"]) * d > 0
    triggered = trig_ev or trig_mom

    raw = (6 * max(0.0, mb) - 4 * max(0.0, -mb) + 8 * max(0.0, sb) + zone_pts + 4 * confirmed + 3 * triggered)
    notes = [f"macro {roles['macro']} {mb:+.2f}", f"structure {roles['structural']} {sb:+.2f}",
             f"zone on {zone['tf']}"]
    if confirmed:
        notes.append(f"{roles['confirm']} confirmed (" + ",".join(n for n, f in (("event", conf_ev), ("sweep", conf_sw), ("displacement", conf_disp)) if f) + ")")
    if triggered:
        notes.append(f"{roles['entry']} entry trigger")
    return MTFResult(True, "", max(0.0, raw) * st.w_mtf / 25.0, mb, sb, zone, confirmed, triggered, notes, align, conflicts)
