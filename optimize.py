"""Evidence engine. Works on cached stage-1 signals, so each parameter trial is a fast re-simulation.

    python optimize.py --csv XAUUSD_M1.csv --timeframe M1 --offset 2 --group entry --folds 5 --out opt
    python optimize.py --synthetic 20000 --group management --folds 4

Rules this tool enforces:
- one parameter GROUP at a time (entry, management, sizing), never everything at once
- parameters are chosen on past data only and judged on the following, unseen window (walk-forward)
- the optimized result is always compared with the untouched defaults on the same unseen windows
- components are ablated: if switching one off does not hurt out-of-sample results, consider removing it
- Monte Carlo resampling shows how bad the drawdown could plausibly get
- the calibration is fitted on the training share only; the edge book is built from out-of-sample trades only
"""
import argparse
import itertools
import json
import math
import pickle
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from backtest import BTConfig, Backtester, load_csv, load_events, synthetic_m5
from calibration import Calibration
from config import Config
from setups import EdgeBook

GROUPS = {
    "entry": {"strategy.min_score": [60.0, 65.0, 70.0, 75.0], "strategy.min_score_gap": [10.0, 15.0, 20.0],
              "policy.conflict_reject": [0.4, 0.5, 0.6]},
    "management": {"styles.be_r_scale": [0.8, 1.0, 1.3], "styles.trail_mode": ["structure", "atr", "hybrid"],
                   "styles.partial": [True, False]},
    "sizing": {"sizing.mode": ["fixed", "tiered"]},
}
SWITCHES = ("use_regime_filter", "use_micro_gate", "use_hierarchy_confirmation", "use_contradiction")


def set_param(cfg, key, value):
    section, name = key.split(".", 1)
    if section == "styles":
        styles = {}
        for k, v in cfg.strategy.styles.items():
            if name == "be_r_scale":
                styles[k] = replace(v, be_r=round(v.be_r * value, 3))
            elif name == "partial":
                styles[k] = v if value else replace(v, partial=None)
            else:
                styles[k] = replace(v, **{name: value})
        return replace(cfg, strategy=replace(cfg.strategy, styles=styles))
    return replace(cfg, **{section: replace(getattr(cfg, section), **{name: value})})


def apply(cfg, params):
    for k, v in params.items():
        cfg = set_param(cfg, k, v)
    return cfg


def grid(group):
    keys = list(GROUPS[group])
    return [dict(zip(keys, combo)) for combo in itertools.product(*(GROUPS[group][k] for k in keys))]


def r_drawdown(r):
    if len(r) == 0:
        return 0.0
    eq = np.cumsum(r)
    return float(np.max(np.maximum.accumulate(np.r_[0.0, eq])[1:] - eq))


def objective(tr, min_trades):
    if tr is None or len(tr) < min_trades:
        return -math.inf
    r = tr["r"].dropna().values
    return float(r.mean() * math.sqrt(len(r)) - 0.05 * r_drawdown(r))


def r_summary(tr):
    if tr is None or len(tr) == 0:
        return {"n": 0}
    r = tr["r"].dropna()
    wins, losses = r[r > 0].sum(), -r[r < 0].sum()
    return {"n": int(len(r)), "exp_r": round(float(r.mean()), 4), "win_rate": round(float((r > 0).mean()), 3),
            "pf": round(float(wins / losses), 3) if losses else None, "total_r": round(float(r.sum()), 2),
            "max_dd_r": round(r_drawdown(r.values), 2)}


def monte_carlo(r, n_sims=3000, seed=0, dd_threshold=10.0, block_len=5):
    """Monte Carlo with block bootstrap to preserve short-range trade clustering.
    iid resampling remains available with block_len=1.
    """
    r = np.asarray(r, dtype=float)
    if len(r) < 10:
        return {"note": "fewer than 10 trades: Monte Carlo not meaningful"}
    rng = np.random.default_rng(seed)
    block_len = max(1, min(int(block_len), len(r)))
    n = len(r)
    if block_len == 1:
        sims = rng.choice(r, size=(n_sims, n), replace=True)
    else:
        starts = rng.integers(0, n, size=(n_sims, int(np.ceil(n / block_len))))
        pieces = []
        base_idx = np.arange(block_len)
        for j in range(starts.shape[1]):
            idx = (starts[:, j, None] + base_idx[None, :]) % n
            pieces.append(r[idx])
        sims = np.concatenate(pieces, axis=1)[:, :n]
    eq = np.cumsum(sims, axis=1)
    dd = np.max(np.maximum.accumulate(np.concatenate([np.zeros((n_sims, 1)), eq], axis=1), axis=1)[:, 1:] - eq, axis=1)
    tot = eq[:, -1]
    return {"trades": n, "block_len": block_len, "total_r_p5": round(float(np.percentile(tot, 5)), 2),
            "total_r_p50": round(float(np.percentile(tot, 50)), 2), "total_r_p95": round(float(np.percentile(tot, 95)), 2),
            "p_negative": round(float((tot < 0).mean()), 3), "max_dd_r_p50": round(float(np.percentile(dd, 50)), 2),
            "max_dd_r_p95": round(float(np.percentile(dd, 95)), 2),
            f"p_dd_over_{dd_threshold:g}R": round(float((dd > dd_threshold).mean()), 3)}


_POOL_STATE = {}


def _trial(args):
    params, lo, hi = args
    opt = _POOL_STATE["opt"]
    tr = opt.window(apply(opt.btr.cfg, params), lo, hi)
    return objective(tr, opt.min_trades), params, r_summary(tr)


def _trial_init(opt):
    _POOL_STATE["opt"] = opt


class Optimizer:
    def __init__(self, btr, prep, signals, min_trades=30, workers=0):
        self.btr, self.prep, self.sig, self.min_trades, self.workers = btr, prep, signals, min_trades, workers

    def window(self, cfg, lo, hi):
        sig = [s for s in self.sig if lo <= s.i <= hi]
        if not sig:
            return pd.DataFrame()
        return self.btr.simulate(self.prep, sig, cfg, lo, hi)[0]

    def folds(self, k):
        idx = [s.i for s in self.sig]
        cuts = np.array_split(np.arange(len(idx)), k)
        return [(idx[c[0]], idx[c[-1]]) for c in cuts if len(c)]

    def best(self, group, base, lo, hi):
        combos = grid(group)
        if self.workers and self.workers > 1 and len(combos) > 2:
            import multiprocessing as mp
            saved, self.btr.cfg = self.btr.cfg, base
            ctx = mp.get_context("fork") if "fork" in mp.get_all_start_methods() else mp.get_context("spawn")
            with ctx.Pool(self.workers, initializer=_trial_init, initargs=(self,)) as pool:
                scored = pool.map(_trial, [(p, lo, hi) for p in combos])
            self.btr.cfg = saved
        else:
            scored = []
            for params in combos:
                tr = self.window(apply(base, params), lo, hi)
                scored.append((objective(tr, self.min_trades), params, r_summary(tr)))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored

    def walk_forward(self, group, k=5, anchored=True):
        base = self.btr.cfg
        fl = self.folds(k)
        rows, oos_opt, oos_base = [], [], []
        for j in range(1, len(fl)):
            lo = fl[0][0] if anchored else fl[j - 1][0]
            hi = fl[j - 1][1]
            ranked = self.best(group, base, lo, hi)
            chosen = ranked[0][1] if ranked and ranked[0][0] > -math.inf else {}
            t_lo, t_hi = fl[j]
            tr_opt = self.window(apply(base, chosen), t_lo, t_hi)
            tr_base = self.window(base, t_lo, t_hi)
            oos_opt.append(tr_opt)
            oos_base.append(tr_base)
            rows.append({"fold": j, "train": f"{self.prep.idx[lo]:%Y-%m-%d}..{self.prep.idx[hi]:%Y-%m-%d}",
                         "test": f"{self.prep.idx[t_lo]:%Y-%m-%d}..{self.prep.idx[t_hi]:%Y-%m-%d}",
                         "chosen": chosen or "defaults (no combo reached min trades)",
                         "train_best": ranked[0][2] if ranked else {}, "oos_optimized": r_summary(tr_opt),
                         "oos_defaults": r_summary(tr_base)})
        cat = lambda xs: pd.concat([x for x in xs if len(x)], ignore_index=True) if any(len(x) for x in xs) else pd.DataFrame()
        o, b = cat(oos_opt), cat(oos_base)
        chosen_sets = [json.dumps(r["chosen"], sort_keys=True, default=str) for r in rows]
        stability = max(chosen_sets.count(c) for c in set(chosen_sets)) / len(chosen_sets) if chosen_sets else 0
        return {"group": group, "folds": rows, "oos_optimized": r_summary(o), "oos_defaults": r_summary(b),
                "param_stability": round(stability, 2),
                "verdict": self._verdict(r_summary(o), r_summary(b), stability)}, o, b

    @staticmethod
    def _verdict(opt, base, stability):
        if opt.get("n", 0) < 30 or base.get("n", 0) < 30:
            return "not enough out-of-sample trades to conclude anything"
        gain = opt["exp_r"] - base["exp_r"]
        if gain > 0.05 and stability >= 0.5:
            return f"optimization improved OOS expectancy by {gain:+.3f}R with stable parameters: worth adopting carefully"
        if gain > 0.05:
            return f"OOS improved by {gain:+.3f}R but the chosen parameters change from fold to fold: likely noise"
        return f"optimization did not beat the defaults out-of-sample ({gain:+.3f}R): keep the defaults"

    def split(self, group, fractions=(0.6, 0.2, 0.2), top=5):
        idx = [s.i for s in self.sig]
        n = len(idx)
        a, b = int(n * fractions[0]), int(n * (fractions[0] + fractions[1]))
        tr_rng, va_rng, te_rng = (idx[0], idx[a - 1]), (idx[a], idx[b - 1]), (idx[b], idx[-1])
        ranked = [x for x in self.best(group, self.btr.cfg, *tr_rng) if x[0] > -math.inf][:top]
        if not ranked:
            return {"note": "no parameter set reached the minimum trade count on the training data"}
        val = sorted(((objective(self.window(apply(self.btr.cfg, p), *va_rng), max(5, self.min_trades // 3)), p) for _, p, _ in ranked),
                     key=lambda x: x[0], reverse=True)
        chosen = val[0][1]
        return {"chosen": chosen, "oos_once": r_summary(self.window(apply(self.btr.cfg, chosen), *te_rng)),
                "oos_defaults": r_summary(self.window(self.btr.cfg, *te_rng))}

    def ablation(self, k=5):
        fl = self.folds(k)[1:]
        base_cfg = self.btr.cfg

        def oos(cfg):
            parts = [self.window(cfg, lo, hi) for lo, hi in fl]
            parts = [p for p in parts if len(p)]
            return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        ref = r_summary(oos(base_cfg))
        out = {"all_components": ref, "without": {}}
        for sw in SWITCHES:
            s = r_summary(oos(replace(base_cfg, policy=replace(base_cfg.policy, **{sw: False}))))
            delta = (ref.get("exp_r", 0) or 0) - (s.get("exp_r", 0) or 0) if s.get("n") and ref.get("n") else None
            s["component_value_r"] = None if delta is None else round(delta, 4)
            s["keep"] = None if delta is None or min(s["n"], ref["n"]) < 30 else bool(delta > 0)
            out["without"][sw] = s
        return out

    def calibrate(self, train_frac=0.6, save=None):
        idx = [s.i for s in self.sig]
        cut = idx[int(len(idx) * train_frac)]
        train, test = self.window(self.btr.cfg, idx[0], cut), self.window(self.btr.cfg, cut + 1, idx[-1])
        if len(train) < 20:
            return {"note": f"only {len(train)} training trades: calibration skipped"}, None
        cal = Calibration.fit(train, period=f"{self.prep.idx[idx[0]]:%Y-%m-%d}..{self.prep.idx[cut]:%Y-%m-%d}")
        if save:
            cal.save(save)
        return {"trained_on": len(train), "tested_on": len(test), "oos_reliability": cal.reliability_table(test) if len(test) else None}, cal


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv")
    ap.add_argument("--synthetic", type=int, default=0)
    ap.add_argument("--timeframe", choices=["M1", "M5"], default="M5")
    ap.add_argument("--offset", type=float, default=0.0)
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--events")
    ap.add_argument("--group", choices=list(GROUPS), default="entry")
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--rolling", action="store_true", help="rolling instead of anchored training windows")
    ap.add_argument("--min-trades", type=int, default=30)
    ap.add_argument("--decision-step", type=int, default=1)
    ap.add_argument("--signals", help="reuse a pickle written by backtest.py --save-signals or a previous run")
    ap.add_argument("--save-models", action="store_true", help="write data/calibration.json and data/edge_book.json")
    ap.add_argument("--out", default="optimization")
    a = ap.parse_args()

    cfg = Config.from_env()
    sim, step = (synthetic_m5(a.synthetic), 5) if a.synthetic else (load_csv(a.csv, a.offset), 1 if a.timeframe == "M1" else 5)
    if a.start:
        sim = sim[sim.index >= a.start]
    if a.end:
        sim = sim[sim.index < a.end]
    btr = Backtester(cfg, BTConfig(decision_step=a.decision_step), load_events(a.events) if a.events else [])
    prep = btr.prepare(sim, step)
    fp = {"n": len(prep.idx), "first": str(prep.idx[0]), "last": str(prep.idx[-1]), "step": a.decision_step}
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    cache = Path(a.signals) if a.signals else out / "signals.pkl"
    sig = None
    if cache.exists():
        blob = pickle.loads(cache.read_bytes())
        if blob.get("fingerprint") == fp:
            sig = blob["signals"]
            print(f"reusing cached signals from {cache}")
    if sig is None:
        print("generating signals (slow, cached for next time)...")
        sig = btr.signals(prep, progress=True)
        cache.write_bytes(pickle.dumps({"signals": sig, "fingerprint": fp}))
    opt = Optimizer(btr, prep, sig, a.min_trades)
    wf, oos_opt, oos_base = opt.walk_forward(a.group, a.folds, anchored=not a.rolling)
    report = {"walk_forward": wf, "split_60_20_20": opt.split(a.group), "ablation": opt.ablation(a.folds),
              "monte_carlo_oos_defaults": monte_carlo(oos_base["r"].dropna().values if len(oos_base) else [])}
    cal_rep, cal = opt.calibrate(save="data/calibration.json" if a.save_models else None)
    report["calibration"] = cal_rep
    if a.save_models and len(oos_base):
        EdgeBook.build(oos_base, "data/edge_book.json", period=wf["folds"][0]["test"].split("..")[0] + ".." + wf["folds"][-1]["test"].split("..")[1])
    if len(oos_base):
        oos_base.to_csv(out / "oos_trades_defaults.csv", index=False)
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps({"verdict": wf["verdict"], "oos_defaults": wf["oos_defaults"], "oos_optimized": wf["oos_optimized"],
                      "param_stability": wf["param_stability"],
                      "ablation": {k: v.get("keep") for k, v in report["ablation"]["without"].items()},
                      "monte_carlo": report["monte_carlo_oos_defaults"]}, indent=2, default=str))
    print(f"full report: {out / 'report.json'}")


if __name__ == "__main__":
    main()
