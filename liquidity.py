"""One map of where resting liquidity sits relative to price: previous day/week extremes, Asia/London ranges,
equal highs/lows and swing points across timeframes. Buy-side liquidity (stops above highs) sits above price,
sell-side liquidity (stops below lows) sits below."""
from dataclasses import dataclass, field

from smc import POOL_WEIGHT

TF_BONUS = {"M5": 0.0, "M15": 0.0, "H1": 0.5, "H4": 1.0, "D1": 1.5}
HIGH_KINDS = ("PDH", "PWH", "ASIA_H", "LONDON_H", "EQH", "PMH", "PYH")


@dataclass
class Pool:
    kind: str
    level: float
    side: str               # buy (above price) | sell (below price)
    tf: str
    weight: float
    dist_atr: float
    swept: bool = False


@dataclass
class LiquidityMap:
    price: float
    atr: float
    pools: list = field(default_factory=list)

    def side(self, side, untouched=True):
        ps = [p for p in self.pools if p.side == side and (not untouched or not p.swept)]
        return sorted(ps, key=lambda p: p.dist_atr)

    def nearest(self, side):
        ps = self.side(side)
        return ps[0] if ps else None

    def draw(self, horizon_atr=3.0):
        """+1 if buy-side liquidity pulls harder (closer / heavier), -1 if sell-side, 0 if balanced."""
        pull = {s: sum(p.weight / (1 + p.dist_atr) for p in self.side(s) if p.dist_atr <= horizon_atr) for s in ("buy", "sell")}
        if max(pull.values()) == 0:
            return 0
        diff = (pull["buy"] - pull["sell"]) / max(pull.values())
        return 1 if diff > 0.25 else -1 if diff < -0.25 else 0

    def targets(self, d, min_weight=0.0):
        """Liquidity the trade would run toward: buy-side above for longs, sell-side below for shorts."""
        return [p for p in self.side("buy" if d > 0 else "sell") if p.weight >= min_weight]

    def stop_side(self, d):
        return self.side("sell" if d > 0 else "buy")

    def narrative(self, d=0):
        b, s = self.nearest("buy"), self.nearest("sell")
        parts = []
        if b:
            parts.append(f"buy-side {b.kind}@{b.level:.2f} ({b.dist_atr:.1f} ATR above)")
        if s:
            parts.append(f"sell-side {s.kind}@{s.level:.2f} ({s.dist_atr:.1f} ATR below)")
        dr = self.draw()
        if dr:
            parts.append(f"draw toward {'buy' if dr > 0 else 'sell'}-side liquidity")
        return "; ".join(parts) or "no nearby liquidity"


def build_map(views, price, tfs=("M15", "H1", "H4"), ref_tf="H1"):
    ref = views.get(ref_tf) or next(iter(views.values()))
    atr = max(ref["atr"], 1e-9)
    raw = []

    def add(kind, level, tf, high_type):
        side = "buy" if level > price else "sell"
        swept = (high_type and level <= price) or (not high_type and level >= price)
        raw.append(Pool(kind, float(level), side, tf, POOL_WEIGHT.get(kind, 6) + TF_BONUS.get(tf, 0.0),
                        abs(level - price) / atr, swept))

    for tf in tfs:
        v = views.get(tf)
        if not v:
            continue
        for _, p in v["swing_highs"][-6:]:
            add("SWING", p, tf, True)
        for _, p in v["swing_lows"][-6:]:
            add("SWING", p, tf, False)
        for e in v.get("eq_levels", []):
            add(e["kind"], e["level"], tf, e["kind"] == "EQH")
    intraday = views.get("M15") or views.get("M5")
    if intraday:
        k = intraday.get("key_levels", {})
        cdh, cdl = k.get("CDH"), k.get("CDL")
        for kind in ("PDH", "PDL", "PWH", "PWL", "ASIA_H", "ASIA_L", "LONDON_H", "LONDON_L"):
            if kind in k:
                hi = kind in HIGH_KINDS
                add(kind, k[kind], "D1" if kind.startswith("P") else "M15", hi)
                if hi and cdh is not None and cdh > k[kind] and kind in ("PDH", "PWH"):
                    raw[-1].swept = True
                if not hi and cdl is not None and cdl < k[kind] and kind in ("PDL", "PWL"):
                    raw[-1].swept = True
    macro = (views.get("D1") or {}).get("macro_levels") or {}
    for kind in ("PMH", "PML", "PYH", "PYL"):
        if kind in macro:
            hi = kind.endswith("H")
            add(kind, macro[kind], "D1", hi)
            cur = macro.get("CMH" if kind.startswith("PM") and hi else "CML" if kind.startswith("PM") else "CYH" if hi else "CYL")
            if cur is not None and ((hi and cur > macro[kind]) or (not hi and cur < macro[kind])):
                raw[-1].swept = True
    raw.sort(key=lambda p: p.level)
    merged = []
    for p in raw:
        if merged and abs(p.level - merged[-1].level) <= 0.15 * atr and p.side == merged[-1].side:
            q = merged[-1]
            if p.weight > q.weight:
                p.swept = p.swept and q.swept
                merged[-1] = p
            else:
                q.swept = q.swept and p.swept
                q.weight = max(q.weight, p.weight) + 0.5
        else:
            merged.append(p)
    return LiquidityMap(price=price, atr=atr, pools=merged)
