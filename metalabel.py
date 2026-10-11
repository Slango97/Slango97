"""Meta-labelling: the rule engine proposes candidates (primary model); a learned filter estimates which of them
actually reach their target before their stop (secondary model). Labels come from triple-barrier outcomes
(stop / target / time) of EVERY candidate in the backtest, traded or blocked.

Validation is purged + embargoed K-fold in time order (Lopez de Prado): training samples whose label window overlaps
the test fold are removed, plus an embargo after it, so no future information leaks into training.
Dependency-free (NumPy logistic regression with L2) and fully inspectable.

    python metalabel.py train --csv history/XAUUSD_M1.csv --timeframe M1 --offset 2 [--register]
"""
import argparse
import json

import numpy as np

STYLES = ("scalp", "day", "swing", "position")
SETUPS = ("LIQUIDITY_SWEEP_REVERSAL", "BREAKOUT_RETEST", "BOS_PULLBACK", "ORDER_BLOCK_RETEST", "FVG_RETEST",
          "TREND_CONTINUATION", "TREND_PULLBACK", "MEAN_REVERSION")
REGIMES = ("STRONG_TREND", "WEAK_TREND", "RANGE", "COMPRESSION", "BREAKOUT", "REVERSAL", "EXPANSION")
SESSIONS = ("ASIA", "TOKYO", "SYDNEY", "LONDON_OPEN", "LONDON_CONT", "OVERLAP", "NY_OPEN", "NY_CONT", "NY_CLOSE", "OFF")
VOLS = ("LOW", "NORMAL", "HIGH")
COMPONENTS = ("mtf", "structure", "zone", "location", "liquidity", "momentum", "room", "volatility", "session")


def features(c):
    """Numeric feature dict from a candidate Decision (identical live and in research)."""
    f = {f"c_{k}": float(c.components.get(k, 0.0)) for k in COMPONENTS}
    near = min((b["r"] for b in (c.barriers or []) if b.get("r") is not None), default=10.0)
    f.update(technical=float(c.technical), rr=float(c.rr), conflict=float(c.conflict),
             mq=float(c.market_quality) / 100, confirmed=float(bool(c.confirmed)), triggered=float(bool(c.triggered)),
             direction=float(c.direction), nearest_barrier_r=float(min(near, 10.0)),
             mtf_conflicts=float(len(c.mtf_conflicts or [])), n_tags=float(len(c.tags or [])))
    for group, vals, cur in (("style", STYLES, c.style), ("setup", SETUPS, c.setup), ("regime", REGIMES, c.regime),
                             ("session", SESSIONS, c.session), ("vol", VOLS, c.vol_regime)):
        for v in vals:
            f[f"{group}_{v}"] = 1.0 if cur == v else 0.0
    return f


def auc(y, p):
    y, p = np.asarray(y), np.asarray(p)
    pos, neg = y == 1, y == 0
    if pos.sum() == 0 or neg.sum() == 0:
        return None
    ranks = np.argsort(np.argsort(p)) + 1.0
    return float((ranks[pos].sum() - pos.sum() * (pos.sum() + 1) / 2) / (pos.sum() * neg.sum()))


class MetaModel:
    def __init__(self, names=None, w=None, mu=None, sd=None, l2=1.0):
        self.names, self.w, self.mu, self.sd, self.l2 = names or [], w, mu, sd, l2

    def fit(self, X, y, names, l2=1.0, iters=60):
        X, y = np.asarray(X, float), np.asarray(y, float)
        self.names, self.l2 = list(names), l2
        self.mu, self.sd = X.mean(0), X.std(0)
        self.sd[self.sd < 1e-9] = 1.0
        Z = np.column_stack([np.ones(len(X)), (X - self.mu) / self.sd])
        w = np.zeros(Z.shape[1])
        reg = np.full(Z.shape[1], l2)
        reg[0] = 0.0
        for _ in range(iters):                                  # Newton / IRLS with L2
            p = 1 / (1 + np.exp(-np.clip(Z @ w, -30, 30)))
            g = Z.T @ (p - y) + reg * w
            H = (Z * (p * (1 - p))[:, None]).T @ Z + np.diag(reg + 1e-9)
            step = np.linalg.solve(H, g)
            w -= step
            if np.abs(step).max() < 1e-7:
                break
        self.w = w
        return self

    def proba(self, X):
        X = np.atleast_2d(np.asarray(X, float))
        Z = np.column_stack([np.ones(len(X)), (X - self.mu) / self.sd])
        return 1 / (1 + np.exp(-np.clip(Z @ self.w, -30, 30)))

    def predict(self, feat):
        return float(self.proba([[feat.get(n, 0.0) for n in self.names]])[0])

    def importance(self, top=12):
        c = sorted(zip(self.names, self.w[1:]), key=lambda x: -abs(x[1]))
        return [(n, round(float(v), 3)) for n, v in c[:top]]

    def to_json(self):
        return {"names": self.names, "w": self.w.tolist(), "mu": self.mu.tolist(), "sd": self.sd.tolist(), "l2": self.l2}

    @classmethod
    def from_json(cls, d):
        return cls(d["names"], np.array(d["w"]), np.array(d["mu"]), np.array(d["sd"]), d.get("l2", 1.0))


def purged_folds(t_start, t_end, k=5, embargo_frac=0.01):
    """Yield (train_idx, test_idx): contiguous time folds, purging overlaps and embargoing after each test fold."""
    order = np.argsort(t_start)
    n = len(order)
    span = float(np.max(t_end) - np.min(t_start)) or 1.0
    emb = embargo_frac * span
    for test in np.array_split(order, k):
        if not len(test):
            continue
        lo, hi = t_start[test].min(), t_end[test].max()
        keep = (t_end < lo) | (t_start > hi + emb)
        train = np.setdiff1d(np.where(keep)[0], test)
        yield train, test


def _pick_threshold(p, r, thresholds, min_filtered=30):
    p, r = np.asarray(p, float), np.asarray(r, float)
    if len(r) == 0:
        return [], None
    base = float(r.mean())
    best, rows = None, []
    for th in thresholds:
        sel = p >= th
        if int(sel.sum()) < min_filtered:
            continue
        er = float(r[sel].mean())
        row = {"threshold": float(th), "n": int(sel.sum()), "exp_r": round(er, 4), "gain": round(er - base, 4)}
        rows.append(row)
        if best is None or er > best["exp_r"]:
            best = row
    return rows, best


def _inner_threshold(X, y, ts, te, cf, names, k=3, min_filtered=30):
    ps, rs = [], []
    for tr, va in purged_folds(ts, te, max(2, min(k, len(y) // 40))):
        if len(tr) < 30 or len(set(y[tr])) < 2:
            continue
        model = MetaModel().fit(X[tr], y[tr], names)
        ps.append(model.proba(X[va])); rs.append(cf[va])
    if not ps:
        return None
    _, best = _pick_threshold(np.concatenate(ps), np.concatenate(rs), (0.35,0.4,0.45,0.5,0.55,0.6,0.65,0.7), min_filtered)
    return best


def cross_validate(X, y, t, names, cf_r=None, k=5, l2=1.0, min_filtered=30):
    """Nested purged CV: outer folds produce unbiased predictions; threshold selection occurs only in inner folds."""
    X, y = np.asarray(X, float), np.asarray(y, int)
    t_start, t_end = np.asarray(t[0], float), np.asarray(t[1], float)
    cf = np.asarray(cf_r, float) if cf_r is not None else None
    oos = np.full(len(y), np.nan); filtered = np.zeros(len(y), dtype=bool); inner_thresholds=[]
    for tr, te in purged_folds(t_start, t_end, k):
        if len(tr) < 30 or len(set(y[tr])) < 2:
            continue
        model = MetaModel().fit(X[tr], y[tr], names, l2)
        oos[te] = model.proba(X[te])
        if cf is not None:
            best = _inner_threshold(X[tr], y[tr], t_start[tr], t_end[tr], cf[tr], names, max(2, min(3,k-1)), min_filtered)
            if best:
                inner_thresholds.append(best["threshold"]); filtered[te] = oos[te] >= best["threshold"]
    m = ~np.isnan(oos)
    if m.sum() < 20:
        return {"note":"not enough out-of-sample predictions"}
    p, yy = oos[m], y[m]; base=float(yy.mean())
    out={"n":int(m.sum()),"oos_auc":round(auc(yy,p),4) if auc(yy,p) is not None else None,
         "oos_brier":round(float(np.mean((p-yy)**2)),4),"base_rate_brier":round(float(np.mean((base-yy)**2)),4),"base_win_rate":round(base,3)}
    if cf is not None:
        r=cf[m]; fm=filtered[m]
        out["all_candidates_exp_r"]=round(float(r.mean()),4)
        out["inner_thresholds"]=inner_thresholds
        out["outer_filtered_n"]=int(fm.sum())
        out["outer_filtered_exp_r"]=round(float(r[fm].mean()),4) if fm.any() else None
        out["outer_filtered_gain"]=round(float(r[fm].mean()-r.mean()),4) if fm.any() else None
        out["recommended_threshold"]=round(float(np.median(inner_thresholds)),3) if inner_thresholds else None
        out["best_threshold"]=out["recommended_threshold"]  # backward-compatible reporting alias; never selected from outer OOS
    out["importance"]=MetaModel().fit(X[m],y[m],names,l2).importance()
    return out


def dataset_from_backtest(btr):
    """(X, y, (t_start, t_end), names, cf_r) from the candidate rows collected by Backtester.simulate()."""
    rows = [r for r in (btr.candidate_rows or []) if r.get("cf_r") is not None]
    if not rows:
        return np.zeros((0, 0)), np.zeros(0), (np.zeros(0), np.zeros(0)), [], np.zeros(0)
    names = sorted(rows[0]["features"])
    X = np.array([[r["features"].get(n, 0.0) for n in names] for r in rows])
    y = np.array([1 if r["cf_r"] > 0 else 0 for r in rows])
    return X, y, (np.array([r["i"] for r in rows], float), np.array([r["i_end"] for r in rows], float)), names, \
        np.array([r["cf_r"] for r in rows])


def train(btr, cfg, register=False, fingerprint=None):
    X, y, t, names, cf = dataset_from_backtest(btr)
    if len(y) < 100:
        return {"note": f"only {len(y)} labelled candidates: not enough to train"}, None
    cv = cross_validate(X, y, t, names, cf)
    model = MetaModel().fit(X, y, names)
    outer_er, outer_gain = cv.get("outer_filtered_exp_r"), cv.get("outer_filtered_gain")
    gates = [{"name": "oos_auc", "passed": (cv.get("oos_auc") or 0) >= 0.55, "value": cv.get("oos_auc"), "threshold": 0.55, "required": True},
             {"name": "brier_beats_base_rate", "passed": cv.get("oos_brier", 1) < cv.get("base_rate_brier", 0),
              "value": cv.get("oos_brier"), "threshold": cv.get("base_rate_brier"), "required": True},
             {"name": "outer_filtered_expectancy_gain", "required": True,
              "passed": outer_er is not None and outer_gain is not None and outer_gain >= cfg.learning.meta_min_filtered_gain and outer_er > 0,
              "value": outer_er, "gain": outer_gain, "threshold": f"outer gain >= {cfg.learning.meta_min_filtered_gain}R and exp_r > 0"},
             {"name": "outer_filtered_samples", "passed": int(cv.get("outer_filtered_n") or 0) >= cfg.learning.meta_min_filtered_trades,
              "value": int(cv.get("outer_filtered_n") or 0), "threshold": cfg.learning.meta_min_filtered_trades, "required": True},
             {"name": "samples", "passed": len(y) >= 300, "value": int(len(y)), "threshold": 300, "required": True}]
    passed = all(g["passed"] for g in gates)
    meta = {"gates_passed": passed, "gates": gates, "cv": cv, "recommended_min_prob": cv.get("recommended_threshold"),
            "data_fingerprint": fingerprint}
    vid = None
    if register:
        from registry import Registry
        vid = Registry(cfg.learning.registry_dir).register("meta_model", model.to_json(), meta)
    return {"cv": cv, "gates": gates, "gates_passed": passed, "registered": vid}, model


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("train")
    for x in ("--csv", "--events", "--start", "--end"):
        t.add_argument(x)
    t.add_argument("--synthetic", type=int, default=0)
    t.add_argument("--timeframe", choices=["M1", "M5"], default="M1")
    t.add_argument("--offset", type=float, default=0.0)
    t.add_argument("--decision-step", type=int, default=1)
    t.add_argument("--workers", type=int, default=0)
    t.add_argument("--register", action="store_true")
    a = ap.parse_args()
    from backtest import BTConfig, Backtester, load_csv, load_events, synthetic_m5
    from config import Config
    cfg = Config.from_env()
    sim, step = (synthetic_m5(a.synthetic), 5) if a.synthetic else (load_csv(a.csv, a.offset), 1 if a.timeframe == "M1" else 5)
    if a.start:
        sim = sim[sim.index >= a.start]
    if a.end:
        sim = sim[sim.index < a.end]
    btr = Backtester(cfg, BTConfig(decision_step=a.decision_step), load_events(a.events) if a.events else [])
    prep = btr.prepare(sim, step)
    btr.simulate(prep, btr.signals(prep, progress=True, workers=a.workers))
    res, _ = train(btr, cfg, a.register, {"n": len(prep.idx), "first": str(prep.idx[0]), "last": str(prep.idx[-1])})
    print(json.dumps(res, indent=2, default=str))
    if res.get("registered"):
        print("Registered. Review, then: python learning.py approve <id> --by <name>; enable with POLICY__USE_META_MODEL=true")


if __name__ == "__main__":
    main()
