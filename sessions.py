"""Session intelligence: fine session phases plus today's Asia / London / NY ranges and the sweeps and
breakouts between them (index must be UTC)."""
import numpy as np

from config import session_phase


def session_context(df, atr_val, phases, ts=None):
    if len(df) < 2:
        return {"phase": "OFF", "events": []}
    idx = df.index
    ts = ts if ts is not None else idx[-1]
    day = idx.normalize()
    mins = (idx.hour * 60 + idx.minute).values
    today = np.asarray(day == day[-1])
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    out = {"phase": session_phase(ts, phases), "events": []}
    windows = {"asia": (0, 420), "london": (420, 720), "ny": (720, 1260)}
    rng = {}
    for name, (a, b) in windows.items():
        m = today & (mins >= a) & (mins < b)
        if m.any():
            rng[name] = (float(h[m].max()), float(l[m].min()), float(c[np.where(m)[0][-1]]))
            out[f"{name}_high"], out[f"{name}_low"] = rng[name][0], rng[name][1]
    pad = 0.2 * atr_val
    for prev, cur in (("asia", "london"), ("london", "ny")):
        if prev in rng and cur in rng:
            ph, pl, _ = rng[prev]
            ch, cl, cc = rng[cur]
            P, C = prev.upper(), cur.upper()
            if ch > ph:
                out["events"].append(f"{C}_BREAKOUT_UP_{P}" if cc > ph + pad else f"{C}_SWEPT_{P}_HIGH")
            if cl < pl:
                out["events"].append(f"{C}_BREAKOUT_DOWN_{P}" if cc < pl - pad else f"{C}_SWEPT_{P}_LOW")
    return out
