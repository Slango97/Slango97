"""When losses cluster, find out WHY before trading on: regime, volatility, spread, news, setup type or execution.
Findings are reported; parameters are never changed automatically."""
from collections import Counter


def diagnose(streak, history=None, dominance=0.6):
    """streak: recent losing-trade records (dicts); history: a wider sample for baselines."""
    findings = []
    out = {"dominant_regime": None, "findings": findings}
    n = len(streak)
    if n == 0:
        return out
    for key, label in (("regime", "regime"), ("setup", "setup"), ("session", "session"), ("vol_regime", "volatility regime")):
        vals = Counter(r.get(key) for r in streak if r.get(key))
        if vals:
            top, cnt = vals.most_common(1)[0]
            if cnt / n >= dominance:
                findings.append(f"{cnt}/{n} losses share {label} {top}")
                if key == "regime":
                    out["dominant_regime"] = top
    hist = history or []

    def avg(rows, k):
        xs = [r[k] for r in rows if isinstance(r.get(k), (int, float))]
        return sum(xs) / len(xs) if xs else None
    for k, label in (("slippage_r", "slippage"), ("spread", "entry spread")):
        a, b = avg(streak, k), avg(hist, k)
        if a is not None and b and a > 1.5 * b:
            findings.append(f"{label} during the streak is {a / b:.1f}x normal (execution problem?)")
    near = sum(1 for r in streak if r.get("event_phase") not in (None, "CLEAR"))
    if near / n >= 0.5:
        findings.append(f"{near}/{n} losses were taken near scheduled events")
    if not findings:
        findings.append("no common cause found: may be normal variance")
    return out
