"""Controlled learning pipeline (research side only - the live engine never runs this):

LIVE results -> analytics -> candidate (parameters / calibration / edge book) -> walk-forward OOS -> ablation
-> calibration check -> stress (spread/slippage) -> sensitivity -> Monte Carlo -> stability -> registry
-> explicit human approval -> production (reversible with rollback).

    python learning.py candidate --csv XAUUSD_M1.csv --timeframe M1 --offset 2 [--live data/trades.jsonl]
    python learning.py status
    python learning.py approve <version_id> --by "your name" --reason "..."
    python learning.py rollback --kind params --by "your name"
    python learning.py drift
"""
import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
import importlib.metadata as metadata
from dataclasses import asdict

import pandas as pd

from config import VERSION, Config
from optimize import Optimizer, apply, monte_carlo, r_summary
from registry import Registry
from setups import EdgeBook


def _sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def code_fingerprint(root=None):
    root = Path(root or __file__).resolve().parent
    h = hashlib.sha256()
    for p in sorted(root.glob("*.py")):
        h.update(p.name.encode()); h.update(p.read_bytes())
    return h.hexdigest()

def dependency_fingerprint():
    names = ("numpy", "pandas", "requests", "MetaTrader5", "anthropic", "openai")
    versions = {}
    for n in names:
        try:
            versions[n] = metadata.version(n)
        except metadata.PackageNotFoundError:
            versions[n] = "missing"
    return _sha256_text(json.dumps(versions, sort_keys=True))

def config_fingerprint(cfg):
    return _sha256_text(json.dumps(asdict(cfg), sort_keys=True, default=str))

def feature_version(cfg):
    payload = {"version": VERSION, "features": asdict(cfg.features), "config": config_fingerprint(cfg),
               "code": code_fingerprint(), "deps": dependency_fingerprint()}
    return VERSION + "+" + _sha256_text(json.dumps(payload, sort_keys=True))[:12]


def _cat(frames):
    frames = [f for f in frames if f is not None and len(f)]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["r"])


def gate(name, passed, value, threshold, required=True):
    return {"name": name, "passed": bool(passed), "value": value, "threshold": threshold, "required": required}


def _numeric_perturbations(params, defaults, pct):
    base = dict(defaults)
    base.update(params)
    out = []
    for k, v in base.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            for f in (1 - pct, 1 + pct):
                out.append({**base, k: type(v)(v * f) if isinstance(v, float) else v})
    return out


def evaluate_gates(lc, wf, stress, sens_share, mc, cal_rep, abl):
    o = wf["oos_optimized"]
    g = [gate("walk_forward_trades", o.get("n", 0) >= lc.min_oos_trades, o.get("n", 0), lc.min_oos_trades),
         gate("walk_forward_expectancy", (o.get("exp_r") or -9) >= lc.min_oos_exp_r, o.get("exp_r"), lc.min_oos_exp_r),
         gate("walk_forward_profit_factor", (o.get("pf") or 0) >= lc.min_oos_pf, o.get("pf"), lc.min_oos_pf),
         gate("not_worse_than_defaults", (o.get("exp_r") or -9) >= (wf["oos_defaults"].get("exp_r") or -9) - 1e-9,
              o.get("exp_r"), wf["oos_defaults"].get("exp_r")),
         gate("parameter_stability", wf["param_stability"] >= lc.min_param_stability, wf["param_stability"], lc.min_param_stability),
         gate("stress_costs", (stress.get("exp_r") or -9) > 0, stress.get("exp_r"), "> 0"),
         gate("sensitivity", sens_share >= lc.sensitivity_pass_share, round(sens_share, 2), lc.sensitivity_pass_share),
         gate("monte_carlo_p_negative", mc.get("p_negative", 1) <= lc.mc_max_p_negative, mc.get("p_negative"), lc.mc_max_p_negative),
         gate("monte_carlo_dd95", mc.get("max_dd_r_p95", 1e9) <= lc.mc_max_dd_r, mc.get("max_dd_r_p95"), lc.mc_max_dd_r)]
    rel = (cal_rep or {}).get("oos_reliability") or {}
    g.append(gate("calibration_brier", rel.get("brier") is not None and rel["brier"] <= rel.get("baseline_brier", 1) + 0.005,
                  rel.get("brier"), rel.get("baseline_brier")))
    harmful = [k for k, v in (abl or {}).get("without", {}).items() if v.get("keep") is False]
    g.append(gate("ablation_review", True, harmful or "all components keep or inconclusive", "informational", required=False))
    return g, all(x["passed"] for x in g if x["required"])


def build_candidate(btr, prep, signals, cfg, groups=("entry", "management"), folds=5, live=None, registry=None, fingerprint=None):
    lc = cfg.learning
    params, wfs = {}, {}
    for grp in groups:                                   # one parameter group at a time, never all together
        btr.cfg = apply(cfg, params)
        opt = Optimizer(btr, prep, signals, lc.min_oos_trades // max(folds - 1, 1))
        wf, oos_opt, oos_base = opt.walk_forward(grp, folds)
        wfs[grp] = wf
        if wf["verdict"].startswith("optimization improved") and "noise" not in wf["verdict"]:
            chosen = Counter(json.dumps(f["chosen"], sort_keys=True) for f in wf["folds"] if isinstance(f["chosen"], dict))
            if chosen:
                params.update(json.loads(chosen.most_common(1)[0][0]))
    cand_cfg = apply(cfg, params)
    btr.cfg = cand_cfg
    opt = Optimizer(btr, prep, signals, 1)
    last = wfs[groups[-1]]
    windows = opt.folds(folds)[1:]
    oos = _cat(opt.window(cand_cfg, lo, hi) for lo, hi in windows)
    saved = (btr.costs.spread_mult, btr.costs.slippage_mult)
    btr.costs.spread_mult, btr.costs.slippage_mult = lc.stress_spread_mult, lc.stress_slippage_mult
    stress = r_summary(_cat(opt.window(cand_cfg, lo, hi) for lo, hi in windows))
    btr.costs.spread_mult, btr.costs.slippage_mult = saved
    defaults = {"strategy.min_score": cfg.strategy.min_score, "policy.conflict_reject": cfg.policy.conflict_reject}
    perts = _numeric_perturbations({k: v for k, v in params.items() if not k.startswith("styles.")}, defaults, lc.sensitivity_pct)
    ok = 0
    for p in perts:
        tr = _cat(opt.window(apply(cfg, p), lo, hi) for lo, hi in windows)
        ok += int(len(tr) > 0 and tr["r"].mean() > 0)
    sens_share = ok / len(perts) if perts else 0.0
    mc = monte_carlo(oos["r"].dropna().values if len(oos) else [], n_sims=lc.mc_sims, block_len=lc.mc_block_len)
    cal_rep, cal = opt.calibrate(train_frac=0.6)
    if cal is not None and cal_rep.get("oos_reliability"):
        idx = [s.i for s in signals]
        test = opt.window(cand_cfg, idx[int(len(idx) * 0.6)] + 1, idx[-1])
        if len(test):
            p0 = cal.m["p0"]
            cal_rep["oos_reliability"]["baseline_brier"] = round(float(((test["r"] > 0).astype(float) - p0).pow(2).mean()), 4)
    abl = opt.ablation(folds)
    gates, passed = evaluate_gates(lc, last, stress, sens_share, mc, cal_rep, abl)
    live_summary = None
    if live:
        lt = pd.DataFrame([r for r in live if r.get("type") == "close" and isinstance(r.get("r_multiple"), (int, float))])
        if len(lt):
            lt["r"] = lt["r_multiple"]
            live_summary = {"overall": r_summary(lt), "by_setup": {k: r_summary(g) for k, g in lt.groupby("setup")}}
    idx = [s.i for s in signals]
    meta = {"gates_passed": passed, "gates": gates, "params": params, "feature_version": feature_version(cfg),
            "code_fingerprint": code_fingerprint(), "config_fingerprint": config_fingerprint(cfg),
            "dependency_fingerprint": dependency_fingerprint(),
            "training_window": f"{prep.idx[idx[0]]:%Y-%m-%d}..{prep.idx[idx[-1]]:%Y-%m-%d}", "data_fingerprint": fingerprint,
            "oos": r_summary(oos), "walk_forward": {g: {k: v for k, v in w.items() if k != "folds"} for g, w in wfs.items()},
            "stress": stress, "sensitivity_share": round(sens_share, 2), "monte_carlo": mc, "live_summary": live_summary}
    out = {"meta": meta, "ids": {}}
    if registry is not None:
        out["ids"]["params"] = registry.register("params", params, meta)
        if cal is not None:
            out["ids"]["calibration"] = registry.register("calibration", cal.m, {**meta, "calibration_report": cal_rep})
        if len(oos):
            out["ids"]["edge_book"] = registry.register("edge_book", EdgeBook.build(oos), meta)
    return out


def apply_production(cfg):
    """Approved production parameters (if any) - applied at startup only, never mid-session."""
    if not (cfg.features.registry_models and cfg.features.apply_production_params):
        return cfg, None
    p = Registry(cfg.learning.registry_dir).production("params")
    if not p or not p["payload"]:
        return cfg, (p or {}).get("id")
    return apply(cfg, p["payload"]), p["id"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("candidate")
    for a in ("--csv", "--events", "--live", "--start", "--end"):
        c.add_argument(a)
    c.add_argument("--synthetic", type=int, default=0)
    c.add_argument("--timeframe", choices=["M1", "M5"], default="M5")
    c.add_argument("--offset", type=float, default=0.0)
    c.add_argument("--folds", type=int, default=5)
    c.add_argument("--groups", default="entry,management")
    c.add_argument("--decision-step", type=int, default=1)
    c.add_argument("--workers", type=int, default=0)
    for name in ("approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("vid")
        p.add_argument("--by", required=True)
        p.add_argument("--reason", default="")
    r = sub.add_parser("rollback")
    r.add_argument("--kind", choices=["params", "calibration", "edge_book"], required=True)
    r.add_argument("--by", required=True)
    r.add_argument("--reason", default="")
    sub.add_parser("status")
    sub.add_parser("drift")
    a = ap.parse_args()
    cfg = Config.from_env()
    reg = Registry(cfg.learning.registry_dir)
    if a.cmd == "status":
        print(json.dumps(reg.summary(), indent=2))
    elif a.cmd == "approve":
        reg.promote(a.vid, a.by, a.reason)
        print(f"promoted {a.vid}")
    elif a.cmd == "reject":
        reg.reject(a.vid, a.by, a.reason)
    elif a.cmd == "rollback":
        print(f"production {a.kind} is now {reg.rollback(a.kind, a.by, a.reason) or 'defaults'}")
    elif a.cmd == "drift":
        from drift import drift_report
        from journal import Journal
        prod = reg.production("calibration")
        if not prod:
            raise SystemExit("no production calibration to compare against")
        j = Journal(cfg.system.data_dir)
        closes = [x for x in j.read_jsonl(j.trades_path) if x.get("type") == "close"]
        print(json.dumps(drift_report(prod["payload"], closes, j.read_jsonl(j.dir / "decision_records.jsonl"), cfg.learning), indent=2))
    else:
        from backtest import BTConfig, Backtester, load_csv, load_events, synthetic_m5
        sim, step = (synthetic_m5(a.synthetic), 5) if a.synthetic else (load_csv(a.csv, a.offset), 1 if a.timeframe == "M1" else 5)
        if a.start:
            sim = sim[sim.index >= a.start]
        if a.end:
            sim = sim[sim.index < a.end]
        btr = Backtester(cfg, BTConfig(decision_step=a.decision_step), load_events(a.events) if a.events else [])
        prep = btr.prepare(sim, step)
        print("generating signals...")
        sig = btr.signals(prep, progress=True, workers=a.workers)
        live = None
        if a.live:
            from journal import Journal
            live = Journal(".").read_jsonl(a.live)
        fp = {"n": len(prep.idx), "first": str(prep.idx[0]), "last": str(prep.idx[-1])}
        out = build_candidate(btr, prep, sig, cfg, tuple(a.groups.split(",")), a.folds, live, reg, fp)
        print(json.dumps({"gates_passed": out["meta"]["gates_passed"], "ids": out["ids"],
                          "gates": [(g["name"], g["passed"], g["value"]) for g in out["meta"]["gates"]]}, indent=2, default=str))
        print("Nothing is live yet. Review the report, then: python learning.py approve <id> --by <name>")


if __name__ == "__main__":
    main()
