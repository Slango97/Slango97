"""Drift detection: is the live market behaving like the data the production model was trained on?
Outcome drift (expectancy, win rate) and input drift (score / regime / session distribution, via PSI)."""
import math

BINS = [0, 65, 70, 75, 80, 85, 101]


def psi(ref, cur, eps=1e-4):
    keys = sorted(set(ref) | set(cur)) if isinstance(ref, dict) else range(len(ref))
    rt = sum((ref.get(k, 0) if isinstance(ref, dict) else ref[k]) for k in keys) or 1
    ct = sum((cur.get(k, 0) if isinstance(cur, dict) else cur[k]) for k in keys) or 1
    out = 0.0
    for k in keys:
        r = max((ref.get(k, 0) if isinstance(ref, dict) else ref[k]) / rt, eps)
        c = max((cur.get(k, 0) if isinstance(cur, dict) else cur[k]) / ct, eps)
        out += (c - r) * math.log(c / r)
    return out


def _hist(scores):
    h = [0] * (len(BINS) - 1)
    for s in scores:
        for i in range(len(BINS) - 1):
            if BINS[i] <= s < BINS[i + 1]:
                h[i] += 1
                break
    return h


def drift_report(model, closes, records, lc):
    findings, details = [], {}
    rs = [c["r_multiple"] for c in closes if isinstance(c.get("r_multiple"), (int, float))]
    n = len(rs)
    ref = model.get("reference", {})
    if n >= lc.drift_min_trades:
        exp = [c["exp_r"] for c in closes if isinstance(c.get("exp_r"), (int, float))]
        expected = sum(exp) / len(exp) if exp else model.get("r0", 0.0)
        mean = sum(rs) / n
        sd = math.sqrt(sum((x - mean) ** 2 for x in rs) / (n - 1)) or 1e-9
        z = (mean - expected) / (sd / math.sqrt(n))
        details.update(mean_r=round(mean, 3), expected_r=round(expected, 3), z_expectancy=round(z, 2))
        if z <= -lc.drift_z:
            findings.append(f"live expectancy {mean:+.2f}R is {abs(z):.1f} sd below the model's {expected:+.2f}R")
        pw = [c["p_win"] for c in closes if isinstance(c.get("p_win"), (int, float))]
        p = sum(pw) / len(pw) if pw else model.get("p0", 0.5)
        obs = sum(1 for x in rs if x > 0) / n
        zb = (obs - p) / math.sqrt(max(p * (1 - p), 1e-6) / n)
        details.update(win_rate=round(obs, 3), expected_win_rate=round(p, 3), z_win_rate=round(zb, 2))
        if zb <= -lc.drift_z:
            findings.append(f"live win rate {obs:.0%} is significantly below the expected {p:.0%}")
    dist_flags = []
    tops = [max(r["candidates"], key=lambda c: c["score"]) for r in records if r.get("candidates")]
    if len(tops) >= 50 and ref.get("score_hist"):
        v = psi(ref["score_hist"], _hist([c["score"] for c in tops]))
        details["psi_score"] = round(v, 3)
        if v > lc.drift_psi:
            dist_flags.append(f"score distribution shifted (PSI {v:.2f})")
    regimes = {}
    for r in records:
        lab = (r.get("regime") or {}).get("label")
        if lab:
            regimes[lab] = regimes.get(lab, 0) + 1
    if sum(regimes.values()) >= 50 and ref.get("regime_counts"):
        v = psi(ref["regime_counts"], regimes)
        details["psi_regime"] = round(v, 3)
        if v > lc.drift_psi:
            dist_flags.append(f"regime mix shifted (PSI {v:.2f})")
    status = "DRIFT" if findings else "WATCH" if dist_flags else "OK"
    return {"status": status, "findings": findings + dist_flags, "n_trades": n, "details": details}
