"""Pure trade-management logic: given a position, its stored state and the latest analysis, decide
what to do. Used identically by the live TradeManager and by the backtester."""
import math
from dataclasses import dataclass


@dataclass
class PosView:
    direction: int
    price: float          # current exit-side price (bid for longs, ask for shorts)
    sl: float
    volume: float
    age_h: float
    volume_min: float = 0.01
    volume_step: float = 0.01


@dataclass
class Action:
    kind: str             # close | partial | move_sl
    value: float = 0.0    # new SL for move_sl
    volume: float = 0.0   # for partial
    reason: str = ""
    flag: str = ""        # state flag to set on success: be | partial | trail


def floor_step(vol, step, vmin):
    v = math.floor(vol / step + 1e-9) * step
    return round(v, 8) if v >= vmin else 0.0


def better(d, new, cur):
    return cur == 0 or (new > cur if d > 0 else new < cur)


def trail_candidates(d, view, S, price, atr):
    structure = []
    swings = view["swing_lows"] if d > 0 else view["swing_highs"]
    for _, p in swings[-5:]:
        structure.append(p - d * 0.25 * atr)
    atr_c = [price - d * S.trail_atr_mult * atr]
    ema_c = [view["ema20"] - d * 0.25 * atr]
    return {"structure": structure, "atr": atr_c, "ema": ema_c, "hybrid": structure + atr_c}.get(S.trail_mode, structure)


def plan(st, pos, view, news_blackout, spread, min_stop, S):
    """Return a list of Actions for one open position."""
    d, entry, risk = st["direction"], st["entry"], st["risk"]
    if risk <= 0:
        return []
    r = (pos.price - entry) * d / risk
    atr = view["atr"] if view else risk
    if pos.age_h > S.max_hold_h and r < 0.5:
        return [Action("close", reason=f"time stop after {pos.age_h:.1f}h at {r:.2f}R")]
    acts = []
    tp1 = st.get("tp1")
    tp1_hit = bool(tp1) and (pos.price - tp1) * d >= 0
    if tp1:
        if tp1_hit and not st.get("partial"):
            frac = st.get("tp1_frac", 0.5)
            vol = floor_step(pos.volume * frac, pos.volume_step, pos.volume_min)
            if 0 < vol < pos.volume:
                acts.append(Action("partial", volume=vol, reason=f"TP1 reached ({r:.2f}R): close {frac:.0%}", flag="partial"))
    elif S.partial and not st.get("partial") and r >= S.partial[0]:
        vol = floor_step(pos.volume * S.partial[1], pos.volume_step, pos.volume_min)
        if 0 < vol < pos.volume:
            acts.append(Action("partial", volume=vol, reason=f"partial {S.partial[1]:.0%} at {r:.2f}R", flag="partial"))

    cur_sl = pos.sl
    be = st.get("be") or (pos.sl != 0 and (pos.sl - entry) * d >= 0)
    if not be and tp1:
        if tp1_hit or st.get("partial") or (news_blackout and r >= 0.3):
            new = entry + d * max(spread, 1e-9)       # "the exact entry price + transaction spread costs"
            if better(d, new, cur_sl) and (pos.price - new) * d >= min_stop:
                why = "after TP1 (entry + spread)" if (tp1_hit or st.get("partial")) else "news risk"
                acts.append(Action("move_sl", new, reason="break-even " + why, flag="be"))
                cur_sl, be = new, True
    elif not be:
        held = view is not None and (view["close"] - entry) * d >= 0.5 * risk
        if (r >= S.be_r and held) or (news_blackout and r >= 0.3):
            new = entry + d * max(spread, 0.1 * atr)
            if better(d, new, cur_sl) and (pos.price - new) * d >= min_stop:
                why = "news risk" if news_blackout and r < S.be_r else f"{r:.2f}R reached"
                acts.append(Action("move_sl", new, reason="break-even, " + why, flag="be"))
                cur_sl, be = new, True
    if be and view:
        floor = max(min_stop, 0.5 * atr)
        valid = [c for c in trail_candidates(d, view, S, pos.price, atr)
                 if (pos.price - c) * d >= floor and better(d, c, cur_sl)]
        if valid:
            acts.append(Action("move_sl", max(valid) if d > 0 else min(valid),
                               reason=f"trail ({S.trail_mode})", flag="trail"))
    return acts
