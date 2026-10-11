"""M1 execution refinement under the M5 trigger. M1 never creates a trade; it only times (or cancels) one that
M5 and every higher gate already authorised: wait for M1 confirmation, refuse to chase, abort if the signal
is being invalidated before entry."""
from dataclasses import dataclass, field


@dataclass
class Armed:
    dec: object
    ms: object
    armed_at: object
    expires_at: object
    checks: int = 0
    last_reason: str = ""
    history: list = field(default_factory=list)


@dataclass
class RefineResult:
    action: str                 # GO | WAIT | ABORT
    reason: str
    details: dict = field(default_factory=dict)


def m1_check(dec, m1, price, cfg):
    rc, d = cfg.m1, dec.direction
    risk = abs(dec.entry - dec.sl)
    if risk <= 0:
        return RefineResult("ABORT", "invalid stop distance")
    if (price - dec.sl) * d <= 0 or (dec.tp - price) * d <= 0:
        return RefineResult("ABORT", "price already beyond stop or target")
    moved = (price - dec.entry) * d / risk
    if moved <= -rc.max_adverse_r:
        return RefineResult("ABORT", f"price moved {-moved:.2f}R toward the stop before entry", {"moved_r": moved})
    if moved >= rc.max_chase_r:
        return RefineResult("WAIT", f"not chasing: price already {moved:.2f}R toward target", {"moved_r": moved})
    if m1 is None:
        return RefineResult("WAIT", "no M1 data")
    L = m1["df_len"] - 1
    ev, sw = m1["last_event"], m1["sweep"]
    if ev and ev["dir"] == -d and L - ev["idx"] <= rc.against_lookback:
        return RefineResult("WAIT", f"M1 {ev['type']} against the trade", {"moved_r": moved})
    if ev and ev["dir"] == d and L - ev["idx"] <= rc.confirm_lookback:
        return RefineResult("GO", f"M1 {ev['type']} confirms", {"moved_r": moved})
    if sw and sw["dir"] == d and L - sw["bar"] <= rc.confirm_lookback:
        return RefineResult("GO", f"M1 {sw['kind']} sweep confirms", {"moved_r": moved})
    if rc.momentum_ok and m1["ms"]["momentum_dir"] == d and (m1["close"] - m1["ema20"]) * d > 0:
        return RefineResult("GO", "M1 momentum aligned above/below EMA20", {"moved_r": moved})
    return RefineResult("WAIT", "no M1 confirmation yet", {"moved_r": moved})
