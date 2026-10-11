"""Turn a 0-100 score into P(TP before SL), expected R and a data-sufficiency confidence, learned from
backtested trades. Deliberately simple and transparent: binned rates, Bayesian shrinkage toward the overall
average, monotone smoothing across score bins, and a per-setup adjustment. Fit only on TRAINING data."""
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

BINS = [0, 65, 70, 75, 80, 85, 101]


def _pav(values, weights):
    """Pool-adjacent-violators: weighted non-decreasing fit."""
    blocks = [[v, w, 1] for v, w in zip(values, weights)]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][0] > blocks[i + 1][0]:
            v1, w1, n1 = blocks[i]
            v2, w2, n2 = blocks[i + 1]
            w = w1 + w2
            blocks[i] = [(v1 * w1 + v2 * w2) / w if w else (v1 + v2) / 2, w, n1 + n2]
            del blocks[i + 1]
            i = max(i - 1, 0)
        else:
            i += 1
    out = []
    for v, _, n in blocks:
        out += [v] * n
    return out


class Calibration:
    def __init__(self, model):
        self.m = model

    @classmethod
    def fit(cls, trades, prior_strength=20.0, period=""):
        """trades: DataFrame with columns score, setup, r."""
        t = trades.dropna(subset=["r", "score"])
        if len(t) == 0:
            raise ValueError("no trades to calibrate on")
        k = prior_strength
        p0, r0 = float((t["r"] > 0).mean()), float(t["r"].mean())
        idx = np.digitize(t["score"].values, BINS) - 1
        n, p, er = [], [], []
        for b in range(len(BINS) - 1):
            g = t[idx == b]
            nb = len(g)
            n.append(nb)
            p.append((float((g["r"] > 0).sum()) + p0 * k) / (nb + k))
            er.append((float(g["r"].sum()) + r0 * k) / (nb + k))
        w = [x + k for x in n]
        buckets = {}
        cols = [c for c in ("setup", "regime", "session") if c in t.columns]
        if len(cols) == 3:
            for key, g in t.groupby(cols):
                s_mean = float(t.loc[t["setup"] == key[0], "r"].mean())
                buckets["|".join(map(str, key))] = {"n": int(len(g)),
                                                     "delta_r_vs_setup": (float(g["r"].mean()) - s_mean) * len(g) / (len(g) + 2 * k)}
        ref = {"score_hist": np.histogram(t["score"], bins=BINS)[0].tolist(),
               "regime_counts": t["regime"].value_counts().to_dict() if "regime" in t else {},
               "session_counts": t["session"].value_counts().to_dict() if "session" in t else {},
               "mean_r": r0, "win_rate": p0}
        setups = {}
        for s, g in t.groupby("setup"):
            setups[s] = {"n": int(len(g)), "shrunk_delta_r": (float(g["r"].mean()) - r0) * len(g) / (len(g) + k),
                         "shrunk_delta_p": (float((g["r"] > 0).mean()) - p0) * len(g) / (len(g) + k)}
        return cls({"bins": BINS, "n": n, "p_win": _pav(p, w), "exp_r": _pav(er, w), "p0": p0, "r0": r0, "k": k,
                    "setups": setups, "buckets": buckets, "reference": ref, "n_total": int(len(t)), "period": period,
                    "created": datetime.now(timezone.utc).isoformat()})

    def predict(self, score, setup=None, regime=None, session=None):
        m = self.m
        b = int(np.clip(np.digitize([score], m["bins"])[0] - 1, 0, len(m["n"]) - 1))
        p, er, n = m["p_win"][b], m["exp_r"][b], m["n"][b]
        s = m["setups"].get(setup) if setup else None
        if s:
            p, er = p + s["shrunk_delta_p"], er + s["shrunk_delta_r"]
            n = min(n, s["n"]) if s["n"] else n
        bk = m.get("buckets", {}).get(f"{setup}|{regime}|{session}") if setup and regime and session else None
        if bk:
            er += bk["delta_r_vs_setup"]
        return {"p_win": round(float(np.clip(p, 0, 1)), 3), "exp_r": round(float(er), 3),
                "confidence": round(n / (n + m["k"]), 3), "n": int(n)}

    def save(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(self.m, indent=2))

    @classmethod
    def load(cls, path):
        p = Path(path)
        return cls(json.loads(p.read_text())) if p.exists() else None

    def reliability_table(self, trades):
        """Predicted vs realized on (out-of-sample) trades, plus the Brier score."""
        rows, briers = [], []
        for _, r in trades.iterrows():
            pr = self.predict(r["score"], r.get("setup"))
            won = 1.0 if r["r"] > 0 else 0.0
            briers.append((pr["p_win"] - won) ** 2)
            rows.append((pr["p_win"], won, pr["exp_r"], r["r"]))
        if not rows:
            return {"brier": None, "rows": []}
        a = np.array(rows)
        out = []
        for lo, hi in ((0, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 1.01)):
            m = (a[:, 0] >= lo) & (a[:, 0] < hi)
            if m.any():
                out.append({"pred_p": f"{lo:.1f}-{min(hi, 1):.1f}", "n": int(m.sum()), "realized_win": round(float(a[m, 1].mean()), 3),
                            "pred_exp_r": round(float(a[m, 2].mean()), 3), "realized_r": round(float(a[m, 3].mean()), 3)})
        return {"brier": round(float(np.mean(briers)), 4), "rows": out}
