import numpy as np
import pandas as pd

POOL_WEIGHT = {"PYH": 11, "PYL": 11, "PMH": 10, "PML": 10, "PDH": 10, "PDL": 10, "PWH": 10, "PWL": 10, "EQH": 9, "EQL": 9,
               "ASIA_H": 8, "ASIA_L": 8, "LONDON_H": 8, "LONDON_L": 8, "SWING": 6}


def swings(df, n=3):
    """Fractal swing points, vectorised (same result as the V4 loop, ~10x faster)."""
    from numpy.lib.stride_tricks import sliding_window_view
    hi, lo = df["high"].values, df["low"].values
    L = len(df)
    sh = np.zeros(L, bool)
    sl = np.zeros(L, bool)
    if L < 2 * n + 1:
        return sh, sl
    whi = sliding_window_view(hi, n).max(axis=1)
    wlo = sliding_window_view(lo, n).min(axis=1)
    i = np.arange(n, L - n)
    sh[i] = (hi[i] >= whi[i - n]) & (hi[i] > whi[i + 1])
    sl[i] = (lo[i] <= wlo[i - n]) & (lo[i] < wlo[i + 1])
    return sh, sl


def _break_quality(i, level, d, o, h, l, c, a, L):
    """0-100 quality of a structure break at bar i: close beyond level, displacement, and follow-through."""
    rng = max(h[i] - l[i], 1e-9)
    beyond = (c[i] - level) * d
    disp = min(1.0, abs(c[i] - o[i]) / (1.5 * a[i]))
    closepos = (c[i] - l[i]) / rng if d > 0 else (h[i] - c[i]) / rng
    q = 40 + 20 * min(1.0, beyond / (0.5 * a[i])) + 25 * disp * (1.0 if closepos >= 0.7 else 0.6)
    holds = i + 1 < L and (c[i + 1] - level) * d > 0
    if holds:
        q += 15
    kind = "displacement" if disp >= 1 and closepos >= 0.7 else "strong" if beyond >= 0.25 * a[i] else "close"
    return int(min(100, q)), kind, holds


def structure(df, atr_s, n=3):
    """Swing labels (HH/HL/LH/LL), BOS/CHoCH events with quality, and a combined structural trend.

    A swing is only used n bars after it forms (no look-ahead). A break needs a candle CLOSE beyond
    the swing; wick-only pokes are treated as liquidity sweeps, not structure breaks.
    """
    sh, sl = swings(df, n)
    o, h, l, c = (df[x].values for x in ("open", "high", "low", "close"))
    a = atr_s.values
    L = len(df)
    trend_bos = 0
    last_sh = last_sl = None
    prev_h = prev_l = None
    events, sh_list, sl_list, labels = [], [], [], []
    for t in range(L):
        k = t - n
        if k >= 0:
            eps = 0.05 * a[t]
            if sh[k]:
                lab = "H0" if prev_h is None else "HH" if h[k] > prev_h + eps else "LH" if h[k] < prev_h - eps else "EH"
                labels.append((k, "H", float(h[k]), lab))
                prev_h = h[k]
                last_sh = [h[k], k, False]
                sh_list.append((k, float(h[k])))
            if sl[k]:
                lab = "L0" if prev_l is None else "HL" if l[k] > prev_l + eps else "LL" if l[k] < prev_l - eps else "EL"
                labels.append((k, "L", float(l[k]), lab))
                prev_l = l[k]
                last_sl = [l[k], k, False]
                sl_list.append((k, float(l[k])))
        if last_sh and not last_sh[2] and c[t] > last_sh[0]:
            q, kind, holds = _break_quality(t, last_sh[0], 1, o, h, l, c, a, L)
            events.append({"type": "BOS" if trend_bos >= 0 else "CHoCH", "dir": 1, "idx": t,
                           "level": float(last_sh[0]), "quality": q, "kind": kind, "holds": holds})
            trend_bos, last_sh[2] = 1, True
        elif last_sl and not last_sl[2] and c[t] < last_sl[0]:
            q, kind, holds = _break_quality(t, last_sl[0], -1, o, h, l, c, a, L)
            events.append({"type": "BOS" if trend_bos <= 0 else "CHoCH", "dir": -1, "idx": t,
                           "level": float(last_sl[0]), "quality": q, "kind": kind, "holds": holds})
            trend_bos, last_sl[2] = -1, True
    lh = [x[3] for x in labels if x[1] == "H"]
    ll = [x[3] for x in labels if x[1] == "L"]
    last_h, last_l = (lh[-1] if lh else None), (ll[-1] if ll else None)
    if last_h == "HH" and last_l == "HL":
        regime, t_lab = "BULLISH", 1
    elif last_h == "LH" and last_l == "LL":
        regime, t_lab = "BEARISH", -1
    else:
        regime, t_lab = "RANGE", 0
    trend = trend_bos if t_lab in (0, trend_bos) else 0
    return {"trend": trend, "trend_bos": trend_bos, "regime": regime, "labels": labels[-8:],
            "last_event": events[-1] if events else None, "events": events[-6:], "all_events": events,
            "sh": sh_list, "sl": sl_list}


def displacement(df, atr_s, events=(), lookback=40):
    """Score candles 0-100 for displacement: large body vs ATR, body dominance, tick-volume expansion,
    structure break and FVG creation. Returns (all_scores_by_idx, real_displacements)."""
    o, h, l, c = (df[x].values for x in ("open", "high", "low", "close"))
    v = df["tick_volume"].values.astype(float) if "tick_volume" in df else np.ones(len(df))
    a = atr_s.values
    L = len(df)
    ev_idx = {e["idx"] for e in events}
    scores, real = {}, []
    for i in range(max(21, L - lookback), L):
        body = abs(c[i] - o[i])
        rng = max(h[i] - l[i], 1e-9)
        body_atr, body_ratio = body / a[i], body / rng
        base = v[i - 20:i]
        sd = base.std()
        vol_z = (v[i] - base.mean()) / sd if sd > 0 else 0.0
        d = 1 if c[i] > o[i] else -1
        breaks = i in ev_idx or (c[i] > h[i - 10:i].max() if d > 0 else c[i] < l[i - 10:i].min())
        fvg = i + 1 < L and ((l[i + 1] > h[i - 1]) if d > 0 else (h[i + 1] < l[i - 1]))
        sc = (35 * min(1.0, body_atr / 2.0) + 20 * max(0.0, (body_ratio - 0.5) / 0.5)
              + 15 * min(1.0, max(0.0, vol_z) / 2.0) + 15 * bool(breaks) + 15 * bool(fvg))
        scores[i] = int(sc)
        if body_atr >= 1.2 and body_ratio >= 0.6 and sc >= 60:
            real.append({"idx": i, "dir": d, "score": int(sc), "body_atr": round(float(body_atr), 2),
                         "vol_z": round(float(vol_z), 2), "breaks": bool(breaks), "fvg": bool(fvg)})
    return scores, real


def fair_value_gaps(df, atr_s, min_size_atr=0.1, lookback=150, disp_map=None):
    o, h, l, c = (df[x].values for x in ("open", "high", "low", "close"))
    a = atr_s.values
    L = len(df)
    out = []
    for i in range(max(2, L - lookback), L):
        if l[i] > h[i - 2]:
            typ, bottom, top = "bull", h[i - 2], l[i]
        elif h[i] < l[i - 2]:
            typ, bottom, top = "bear", h[i], l[i - 2]
        else:
            continue
        size = top - bottom
        if size < min_size_atr * a[i]:
            continue
        mid = i - 1
        later = slice(i + 1, L)
        if i + 1 < L:
            if typ == "bull":
                lowest = l[later].min()
                if lowest <= bottom:
                    continue
                fill = max(0.0, (top - lowest) / size)
            else:
                highest = h[later].max()
                if highest >= top:
                    continue
                fill = max(0.0, (highest - bottom) / size)
        else:
            fill = 0.0
        fill = min(1.0, fill)
        disp = abs(c[mid] - o[mid]) / a[mid]
        disp_q = disp_map[mid] / 100 if disp_map and mid in disp_map else min(1, disp / 2.0)
        age = L - 1 - mid
        quality = 35 * min(1, size / a[i] / 0.6) + 30 * disp_q + 25 * (1 - fill) + 10 * max(0, 1 - age / 100)
        out.append({"type": typ, "top": float(top), "bottom": float(bottom), "idx": mid,
                    "size_atr": round(float(size / a[i]), 3), "age": age, "fill_pct": round(float(fill), 3),
                    "disp": round(float(disp), 2), "quality": int(quality),
                    "status": "unmitigated" if fill == 0 else "partial"})
    return out


def order_blocks(df, atr_s, events, disp_mult=1.5, lookback=200, disp_map=None):
    """Last opposite candle before a displacement candle that breaks the recent range."""
    o, h, l, c = (df[x].values for x in ("open", "high", "low", "close"))
    v = df["tick_volume"].values.astype(float) if "tick_volume" in df else np.ones(len(df))
    a = atr_s.values
    L = len(df)
    found = {}
    for i in range(max(12, L - lookback), L):
        if abs(c[i] - o[i]) < disp_mult * a[i]:
            continue
        if c[i] > o[i] and c[i] > h[i - 10:i].max():
            typ, test = "bull", (lambda j: c[j] < o[j])
        elif c[i] < o[i] and c[i] < l[i - 10:i].min():
            typ, test = "bear", (lambda j: c[j] > o[j])
        else:
            continue
        for j in range(i - 1, max(i - 6, 0), -1):
            if test(j):
                found[j] = {"type": typ, "top": float(h[j]), "bottom": float(l[j]), "idx": j, "disp": i}
                break
    out = []
    for z in found.values():
        i = z["disp"]
        later = slice(i + 1, L)
        size = max(z["top"] - z["bottom"], 1e-9)
        if z["type"] == "bull":
            if (c[later] < z["bottom"]).any():
                continue
            tests = int((l[later] <= z["top"]).sum())
            mitig = float(np.clip((z["top"] - l[later].min()) / size, 0, 1)) if i + 1 < L else 0.0
            want = 1
        else:
            if (c[later] > z["top"]).any():
                continue
            tests = int((h[later] >= z["bottom"]).sum())
            mitig = float(np.clip((h[later].max() - z["bottom"]) / size, 0, 1)) if i + 1 < L else 0.0
            want = -1
        bos = any(e["dir"] == want and i <= e["idx"] <= i + 2 for e in events)
        strength = abs(c[i] - o[i]) / a[i]
        base = v[max(0, i - 20):i]
        vol_ratio = float(v[i] / base.mean()) if len(base) and base.mean() > 0 else 1.0
        fresh = 25 if tests == 0 else 15 if tests == 1 else 8 if tests == 2 else 0
        disp_q = disp_map[i] / 100 if disp_map and i in disp_map else min(1, strength / 2.5)
        quality = (30 * disp_q + (25 if bos else 0) + fresh + 10 * (1 - mitig)
                   + 10 * min(1, vol_ratio / 1.5))
        z.update(tests=tests, mitigation_pct=round(mitig, 3), disp_strength=round(float(strength), 2),
                 bos_confirmed=bos, vol_ratio=round(vol_ratio, 2), quality=int(quality))
        out.append(z)
    return sorted(out, key=lambda z: z["idx"])


def sr_levels(sh_list, sl_list, atr_val, tol_mult=0.5, recent=40):
    pts = sorted(p for _, p in (sh_list[-recent:] + sl_list[-recent:]))
    if not pts:
        return []
    tol = tol_mult * atr_val
    clusters = [[pts[0]]]
    for p in pts[1:]:
        if p - np.mean(clusters[-1]) <= tol:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    return [{"level": float(np.mean(c)), "touches": len(c)} for c in clusters if len(c) >= 2]


def equal_levels(sh_list, sl_list, atr_val, tol_mult=0.1, recent=30):
    """Equal highs / equal lows: resting liquidity that price is drawn to."""
    out = []
    for kind, lst, pick in (("EQH", sh_list[-recent:], max), ("EQL", sl_list[-recent:], min)):
        pts = sorted(lst, key=lambda x: x[1])
        cl = []
        for idx, p in pts:
            if cl and p - cl[-1][-1][1] <= tol_mult * atr_val:
                cl[-1].append((idx, p))
            else:
                cl.append([(idx, p)])
        for g in cl:
            if len(g) >= 2:
                out.append({"kind": kind, "level": float(pick(p for _, p in g)), "touches": len(g),
                            "last_idx": max(i for i, _ in g)})
    return out


def key_levels(df, bounds):
    """Previous day / week extremes and completed Asia and London session ranges (index must be UTC)."""
    out = {}
    if len(df) < 2:
        return out
    idx = df.index
    day = idx.normalize()
    days = day.unique()
    hi, lo, hrs = df["high"].values, df["low"].values, idx.hour.values
    cur = days[-1]
    mc = np.asarray(day == cur)
    out["CDH"], out["CDL"] = float(hi[mc].max()), float(lo[mc].min())
    if len(days) >= 2:
        m = np.asarray(day == days[-2])
        out["PDH"], out["PDL"] = float(hi[m].max()), float(lo[m].min())
    last_hour = idx[-1].hour
    for name in ("ASIA", "LONDON"):
        a_, b_ = bounds[name]
        for dd in ((cur, days[-2]) if last_hour >= b_ and len(days) >= 2 else (days[-2],) if len(days) >= 2 else ()):
            m = np.asarray(day == dd) & (hrs >= a_) & (hrs < b_)
            if m.any():
                out[name + "_H"], out[name + "_L"] = float(hi[m].max()), float(lo[m].min())
                break
    wk = day - pd.to_timedelta(day.dayofweek, unit="D")
    weeks = wk.unique()
    if len(weeks) >= 2:
        m = np.asarray(wk == weeks[-2])
        out["PWH"], out["PWL"] = float(hi[m].max()), float(lo[m].min())
    return out


def build_pools(sh_list, sl_list, eq, keys):
    pools = [{"kind": "SWING", "side": "high", "level": p, "formed": i} for i, p in sh_list[-12:]]
    pools += [{"kind": "SWING", "side": "low", "level": p, "formed": i} for i, p in sl_list[-12:]]
    pools += [{"kind": e["kind"], "side": "high" if e["kind"] == "EQH" else "low", "level": e["level"],
               "formed": e["last_idx"]} for e in eq]
    for k, lvl in keys.items():
        if k in ("CDH", "CDL"):
            continue
        pools.append({"kind": k, "side": "high" if k.endswith("H") else "low", "level": lvl, "formed": None})
    return pools


def find_sweep(df, pools, lookback=12):
    """Wick through a liquidity pool that closes back inside. dir is the implied trade direction."""
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    L = len(df)
    for t in range(L - 1, max(L - 1 - lookback, 1), -1):
        best = None
        for p in pools:
            f, lvl = p["formed"], p["level"]
            if f is not None and not (t - 150 < f < t - 2):
                continue
            if p["side"] == "high" and h[t] > lvl and c[t] < lvl:
                if f is not None and h[f + 1:t].max() >= lvl:
                    continue
                cand = {"dir": -1, "extreme": float(h[t])}
            elif p["side"] == "low" and l[t] < lvl and c[t] > lvl:
                if f is not None and l[f + 1:t].min() <= lvl:
                    continue
                cand = {"dir": 1, "extreme": float(l[t])}
            else:
                continue
            cand.update(level=lvl, kind=p["kind"], weight=POOL_WEIGHT.get(p["kind"], 6), bar=t)
            if best is None or cand["weight"] > best["weight"]:
                best = cand
        if best:
            return best
    return None


def fib_range(sh_list, sl_list):
    if not sh_list or not sl_list:
        return None
    (ih, hi), (il, lo) = sh_list[-1], sl_list[-1]
    if hi <= lo:
        return None
    rng = hi - lo
    leg = "up" if il < ih else "down"
    levels = {r: (hi - r * rng if leg == "up" else lo + r * rng) for r in (0.236, 0.382, 0.5, 0.618, 0.705, 0.786)}
    return {"leg": leg, "hi": hi, "lo": lo, "eq": (hi + lo) / 2, "levels": levels}


def trendline(points, L, atr_val, kind, min_touches=3, tol_mult=0.3, recent=7):
    """Trendline through at least `min_touches` swing points. Two-point lines are discarded as fragile."""
    pts = points[-recent:]
    if len(pts) < min_touches:
        return None
    tol = tol_mult * atr_val
    best = None
    for x in range(len(pts) - 1):
        for y in range(x + 1, len(pts)):
            (i1, p1), (i2, p2) = pts[x], pts[y]
            if i2 == i1:
                continue
            slope = (p2 - p1) / (i2 - i1)
            if (kind == "up" and slope <= 0) or (kind == "down" and slope >= 0):
                continue
            touches = [q for q in pts if abs(q[1] - (p1 + slope * (q[0] - i1))) <= tol]
            if len(touches) < min_touches:
                continue
            cand = (len(touches), max(q[0] for q in touches), slope, p1, i1)
            if best is None or cand[:2] > best[:2]:
                best = cand
    if not best:
        return None
    n, last, slope, p1, i1 = best
    return {"slope": float(slope), "value": float(p1 + slope * (L - 1 - i1)), "touches": n, "last_touch": last}
