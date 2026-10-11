"""One command that turns your broker's history into an evidence report with a pass/fail readiness checklist.

    python evidence.py --csv history/XAUUSD_M1.csv --timeframe M1 --offset 2 --out evidence
    python evidence.py --synthetic 9000 --out evidence_demo          # plumbing check only (meaningless numbers)

Optional: --events events.csv, --aux usd_index=history/USDX_H1.csv:-1 (see macro_feeds.py),
--calibrate-costs data/trades.jsonl (fit slippage from your fills first), --broker-diag (needs MT5), --register.
Writes EVIDENCE_REPORT.md, evidence.json, trades / rejected-opportunity CSVs and the cached signals.
"""
import argparse
import json
import pickle
import time
from pathlib import Path

import pandas as pd

from analytics import blocker_report, execution_report, mfe_mae_report
from backtest import BTConfig, Backtester, load_csv, load_events, synthetic_m5
from config import Config, VERSION
from optimize import Optimizer, monte_carlo, r_summary

CHECKS = [("oos_trades", ">=", 100), ("oos_exp_r", ">", 0.05), ("oos_pf", ">=", 1.1), ("mc_p_negative", "<=", 0.2),
          ("stress_exp_r", ">", 0.0), ("meta_auc", ">=", 0.55)]


def run(a):
    cfg = Config.from_env()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rep, t0 = {"version": VERSION, "created": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())}, time.time()
    if a.calibrate_costs:
        from costs import calibrate_from_trades
        from journal import Journal
        closes = [r for r in Journal(".").read_jsonl(a.calibrate_costs) if r.get("type") == "close"]
        cm, info = calibrate_from_trades(closes)
        rep["cost_calibration"] = info
        if cm is not None:
            cm.save(cfg.execution.cost_model_file)
    if a.broker_diag:
        try:
            import MetaTrader5 as mt5
            from broker_diag import diagnose
            from symbol_resolver import discover_gold_symbol
            mt5.initialize()
            cand = discover_gold_symbol(mt5, preferred="XAUUSD", aliases=cfg.system.symbol_aliases, pin=cfg.system.symbol)
            sym = cand.name if cand else cfg.system.symbol
            rep["broker_diagnosis"] = diagnose(mt5, sym, cfg).to_dict()
        except Exception as ex:
            rep["broker_diagnosis"] = {"status": "NOT_RUN", "error": str(ex)}
    sim, step = (synthetic_m5(a.synthetic), 5) if a.synthetic else (load_csv(a.csv, a.offset), 1 if a.timeframe == "M1" else 5)
    if a.start:
        sim = sim[sim.index >= a.start]
    if a.end:
        sim = sim[sim.index < a.end]
    aux = None
    if a.aux:
        from macro_feeds import load_aux_specs
        aux = load_aux_specs(a.aux)
    btr = Backtester(cfg, BTConfig(decision_step=a.decision_step), load_events(a.events) if a.events else [], aux=aux)
    prep = btr.prepare(sim, step)
    cache = out / "signals.pkl"
    fp = {"n": len(prep.idx), "first": str(prep.idx[0]), "last": str(prep.idx[-1]), "step": a.decision_step}
    sig = None
    if cache.exists():
        blob = pickle.loads(cache.read_bytes())
        sig = blob["signals"] if blob.get("fingerprint") == fp else None
    if sig is None:
        sig = btr.signals(prep, progress=True, workers=a.workers)
        cache.write_bytes(pickle.dumps({"signals": sig, "fingerprint": fp}))
    tr, eq, summ = btr.simulate(prep, sig)
    rep["data"] = fp
    rep["full_period"] = {k: v for k, v in summ.items() if not isinstance(v, (dict, list))}
    rep["no_trade_reasons"] = summ.get("no_trade_reasons", {})
    rep["rejected_opportunities"] = summ.get("rejected_opportunities", {})
    rep["mfe_mae"], rep["execution"] = mfe_mae_report(tr), execution_report(tr, "session")
    from analytics import gate_funnel
    rep["gate_funnel"] = gate_funnel(btr.last_rejected)
    tr.to_csv(out / "trades.csv", index=False)
    if btr.last_rejected is not None and len(btr.last_rejected):
        btr.last_rejected.to_csv(out / "rejected_opportunities.csv", index=False)
    opt = Optimizer(btr, prep, sig, a.min_trades, workers=a.workers)
    wf = {}
    for g in a.groups.split(","):
        w, o, b = opt.walk_forward(g, a.folds)
        wf[g] = {k: v for k, v in w.items() if k != "folds"}
        wf[g]["folds"] = [{k: f[k] for k in ("fold", "test", "chosen", "oos_optimized", "oos_defaults")} for f in w["folds"]]
        base_oos = b
    rep["walk_forward"] = wf
    rep["ablation"] = opt.ablation(a.folds)
    rep["monte_carlo_oos"] = monte_carlo(base_oos["r"].dropna().values if len(base_oos) else [], n_sims=cfg.learning.mc_sims, block_len=cfg.learning.mc_block_len)
    saved = (btr.costs.spread_mult, btr.costs.slippage_mult)
    btr.costs.spread_mult, btr.costs.slippage_mult = cfg.learning.stress_spread_mult, cfg.learning.stress_slippage_mult
    stress = [opt.window(cfg, lo, hi) for lo, hi in opt.folds(a.folds)[1:]]
    btr.costs.spread_mult, btr.costs.slippage_mult = saved
    stress = [x for x in stress if len(x)]
    rep["stress_oos"] = r_summary(pd.concat(stress, ignore_index=True)) if stress else {"n": 0}
    try:
        from metalabel import cross_validate, dataset_from_backtest
        X, y, t_window, names, cf_r = dataset_from_backtest(btr)
        rep["meta_label_cv"] = cross_validate(X, y, t_window, names, cf_r=cf_r) if len(y) >= 50 else {"note": f"only {len(y)} labelled candidates"}
    except Exception as ex:
        rep["meta_label_cv"] = {"note": f"not run: {ex}"}
    o = r_summary(base_oos) if len(base_oos) else {"n": 0}
    vals = {"oos_trades": o.get("n", 0), "oos_exp_r": o.get("exp_r"), "oos_pf": o.get("pf"),
            "mc_p_negative": rep["monte_carlo_oos"].get("p_negative"), "stress_exp_r": rep["stress_oos"].get("exp_r"),
            "meta_auc": rep["meta_label_cv"].get("oos_auc")}
    ops = {">=": lambda a, b: a >= b, ">": lambda a, b: a > b, "<=": lambda a, b: a <= b}
    rep["readiness"] = [{"check": k, "value": vals[k], "need": f"{op} {th}", "pass": vals[k] is not None and ops[op](vals[k], th)}
                        for k, op, th in CHECKS]
    bd = rep.get("broker_diagnosis")
    if bd:
        rep["readiness"].append({"check": "broker_diagnosis", "value": bd.get("status"), "need": "OK or WARN", "pass": bd.get("status") in ("OK", "WARN")})
    rep["verdict"] = "EVIDENCE SUPPORTS DEMO" if all(r["pass"] for r in rep["readiness"]) else "NOT READY: keep researching / DRY_RUN"
    rep["runtime_s"] = round(time.time() - t0, 1)
    if a.register:
        from learning import build_candidate
        from registry import Registry
        res = build_candidate(btr, prep, sig, cfg, tuple(a.groups.split(",")), a.folds, None, Registry(cfg.learning.registry_dir), fp)
        rep["registered"] = res["ids"]
    (out / "evidence.json").write_text(json.dumps(rep, indent=2, default=str))
    (out / "EVIDENCE_REPORT.md").write_text(markdown(rep, tr))
    return rep


def markdown(rep, tr):
    L = [f"# Evidence report ({rep['version']}, {rep['created']})", "", f"**Verdict: {rep['verdict']}**", "",
         "| Check | Value | Needed | Pass |", "|---|---|---|---|"]
    L += [f"| {r['check']} | {r['value']} | {r['need']} | {'yes' if r['pass'] else 'NO'} |" for r in rep["readiness"]]
    fp = rep["full_period"]
    L += ["", "## Full period (in-sample, defaults)", "", "| Metric | Value |", "|---|---|"]
    L += [f"| {k} | {v if not isinstance(v, float) else round(v, 4)} |" for k, v in fp.items()]
    for g, w in rep["walk_forward"].items():
        L += ["", f"## Walk-forward: {g}", "", f"Verdict: {w['verdict']}", "",
              f"Out-of-sample with defaults: {w['oos_defaults']}", "", f"Out-of-sample optimized: {w['oos_optimized']}",
              "", f"Parameter stability: {w['param_stability']}"]
    L += ["", "## Ablation (out-of-sample)", ""]
    L += [f"- without {k}: exp_r {v.get('exp_r')} (component value {v.get('component_value_r')}R, keep: {v.get('keep')})"
          for k, v in rep["ablation"]["without"].items()]
    L += ["", f"## Monte Carlo (block bootstrap)\n\n{rep['monte_carlo_oos']}", "", f"## Cost stress\n\n{rep['stress_oos']}",
          "", f"## Meta-label model (purged, embargoed CV)\n\n{rep['meta_label_cv']}",
          "", "## Rejected opportunities", "```", blocker_report(rep.get("rejected_opportunities"), tr).strip(), "```",
          "", "## Gate funnel (which filter costs the most trades)", "```",
          __import__("analytics").format_funnel(rep.get("gate_funnel")).strip(), "```",
          "", f"## MFE / MAE findings\n\n" + "\n".join(f"- {x}" for x in rep["mfe_mae"].get("findings", [])),
          "", "A passing report justifies DEMO, not LIVE. Compare demo results with these numbers before risking money."]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for x in ("--csv", "--events", "--start", "--end", "--aux", "--calibrate-costs"):
        ap.add_argument(x)
    ap.add_argument("--synthetic", type=int, default=0)
    ap.add_argument("--timeframe", choices=["M1", "M5"], default="M1")
    ap.add_argument("--offset", type=float, default=0.0)
    ap.add_argument("--decision-step", type=int, default=1)
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--groups", default="entry,management")
    ap.add_argument("--min-trades", type=int, default=30)
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--broker-diag", action="store_true")
    ap.add_argument("--register", action="store_true")
    ap.add_argument("--out", default="evidence")
    a = ap.parse_args()
    if not a.csv and not a.synthetic:
        raise SystemExit("give --csv (exported with export_history.py) or --synthetic")
    rep = run(a)
    print(json.dumps({"verdict": rep["verdict"], "readiness": rep["readiness"], "runtime_s": rep["runtime_s"]}, indent=2, default=str))
    print(f"report: {Path(a.out) / 'EVIDENCE_REPORT.md'}")


if __name__ == "__main__":
    main()
