"""Classify the current XAUUSD environment BEFORE looking for trades. Each regime enables only the setup types
that fit it; ABNORMAL, NEWS_DRIVEN and EXTREME stand the agent down."""
from dataclasses import dataclass, field

BLACKOUT_PHASES = ("PRE", "RELEASE", "STABILIZING")


@dataclass
class Regime:
    label: str = "RANGE"
    direction: int = 0
    confidence: float = 0.5
    reasons: list = field(default_factory=list)
    allowed: tuple = ()
    no_trade: bool = False
    size_down: bool = False


def _mk(label, rc, direction=0, confidence=0.5, reasons=None):
    return Regime(label, direction, round(confidence, 2), reasons or [], tuple(rc.allowed.get(label, ())),
                  label in rc.no_trade, label in rc.size_down)


def classify(views, cfg, micro=None, news=None):
    rc = cfg.regime
    v, ctx, m15 = views.get(rc.regime_tf), views.get(rc.context_tf), views.get("M15")
    if v is None:
        return _mk("RANGE", rc, reasons=["regime timeframe unavailable"], confidence=0.0)
    if micro is not None and micro.band == "ABNORMAL":
        return _mk("ABNORMAL", rc, confidence=1.0, reasons=[f"market quality {micro.score:.0f}: " + ", ".join(micro.flags)])
    phase = getattr(news, "event_phase", "CLEAR") if news is not None else "CLEAR"
    if phase in BLACKOUT_PHASES:
        return _mk("NEWS_DRIVEN", rc, confidence=1.0, reasons=[f"event {phase}: {getattr(news, 'blackout_reason', '')}"])
    if news is not None and getattr(news, "risk_level", "low") == "extreme" and getattr(news, "status", "") == "OK":
        return _mk("NEWS_DRIVEN", rc, confidence=0.8, reasons=["intelligence reports extreme event risk"])
    if v["vol_regime"] == "EXTREME" or (m15 and m15["vol_regime"] == "EXTREME"):
        return _mk("EXTREME", rc, confidence=1.0, reasons=["extreme volatility"])

    L = v["df_len"] - 1
    ev = v["last_event"]
    age = L - ev["idx"] if ev else 999
    disp_break = any(de["breaks"] and L - de["idx"] <= rc.breakout_lookback for de in v.get("disp_events", []))
    if ev and ev["type"] == "BOS" and age <= rc.breakout_lookback and (ev["kind"] == "displacement" or ev["quality"] >= 75 or disp_break):
        return _mk("BREAKOUT", rc, ev["dir"], ev["quality"] / 100, [f"{rc.regime_tf} displacement BOS {age} bars ago"])
    if ev and ev["type"] == "CHoCH" and age <= rc.reversal_lookback and ev["quality"] >= rc.reversal_min_quality:
        return _mk("REVERSAL", rc, ev["dir"], ev["quality"] / 100, [f"{rc.regime_tf} CHoCH q{ev['quality']}"])
    sw = v["sweep"] or (m15["sweep"] if m15 else None)
    if sw and sw["weight"] >= 9 and m15 and m15["last_event"]:
        e2 = m15["last_event"]
        if e2["type"] == "CHoCH" and e2["dir"] == sw["dir"] and m15["df_len"] - 1 - e2["idx"] <= 15:
            return _mk("REVERSAL", rc, sw["dir"], 0.7, [f"{sw['kind']} swept then M15 CHoCH"])

    ms = v["ms"]
    aligned = ctx is not None and ctx["ms"]["trend_dir"] == ms["trend_dir"] != 0
    if ms["trend_dir"] and aligned and ms["trend_strength"] >= rc.strong_trend_strength and v["adx"] >= rc.strong_trend_adx:
        return _mk("STRONG_TREND", rc, ms["trend_dir"], ms["trend_strength"] / 100,
                   [f"{rc.regime_tf}+{rc.context_tf} trend, strength {ms['trend_strength']}, ADX {v['adx']:.0f}"])
    if v["vol_regime"] == "HIGH":
        return _mk("EXPANSION", rc, ms["trend_dir"], 0.6, ["high volatility expansion"])
    if ms["trend_dir"] and ms["trend_strength"] >= rc.weak_trend_strength:
        return _mk("WEAK_TREND", rc, ms["trend_dir"], ms["trend_strength"] / 100, [f"trend strength {ms['trend_strength']}"])
    if v.get("bb_width_pct", 0.5) <= rc.compression_bbw_pct and v["adx"] < 20:
        return _mk("COMPRESSION", rc, 0, 0.6, ["volatility compression"])
    return _mk("RANGE", rc, 0, 0.5, ["no directional structure"])
