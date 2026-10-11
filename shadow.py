"""Shadow testing: a registry candidate (parameters and/or meta model) decides on the SAME market state as
production on every candle. It is only recorded - it can never trade.

    python shadow.py report                                   # agreement statistics
    python shadow.py report --csv history/XAUUSD_M1.csv --offset 2   # + hypothetical outcomes of both sides
"""
import argparse
import json
import time
from collections import Counter
from pathlib import Path

from decision import decide


def _side(d):
    if d is None:
        return None
    return {"action": d.action, "style": d.style, "setup": d.setup, "direction": d.direction, "entry": round(d.entry, 2),
            "sl": round(d.sl, 2), "tp": round(d.tp, 2), "score": d.score, "meta_p": d.meta_p, "wait_reason": d.wait_reason}


class ShadowRunner:
    def __init__(self, cfg, registry, journal_dir):
        from optimize import apply
        self.cfg, self.path = cfg, Path(journal_dir) / "shadow_records.jsonl"
        self.versions, self.meta_model = {}, None
        sc = cfg.shadow
        self.shadow_cfg = cfg
        if sc.params_version:
            self.shadow_cfg = apply(cfg, registry.get(sc.params_version))       # integrity-checked read, never promoted
            self.versions["params"] = sc.params_version
        if sc.meta_version:
            from metalabel import MetaModel
            self.meta_model = MetaModel.from_json(registry.get(sc.meta_version))
            self.versions["meta_model"] = sc.meta_version
        from dataclasses import replace
        if self.meta_model is not None:
            self.shadow_cfg = replace(self.shadow_cfg, policy=replace(self.shadow_cfg.policy, use_meta_model=True))

    def evaluate(self, ms, mid, news, exclude, prod, calibration=None, edge_book=None, aux=None):
        sd = decide(ms, mid, news, self.shadow_cfg, exclude, calibration, edge_book, aux, self.meta_model)
        rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), "versions": self.versions,
               "production": _side(prod), "shadow": _side(sd),
               "agree": (prod.action, prod.direction, prod.style) == (sd.action, sd.direction, sd.style)}
        with open(self.path, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
        return rec


def report(records, bars=None, horizon_bars=2000):
    out = {"decisions": len(records), "agreement": round(sum(r["agree"] for r in records) / len(records), 3) if records else None}
    for side in ("production", "shadow"):
        out[f"{side}_entries"] = sum(1 for r in records if r[side] and r[side]["action"] in ("BUY", "SELL"))
    out["disagreements"] = dict(Counter(f"{r['production']['action']}->{r['shadow']['action']}" for r in records if not r["agree"]))
    if bars is not None and len(records):
        import numpy as np
        import pandas as pd
        from backtest import counterfactual
        H, L, C = bars["high"].values, bars["low"].values, bars["close"].values
        SP = np.zeros(len(C))
        for side in ("production", "shadow"):
            rs = []
            for r in records:
                s = r[side]
                if not s or s["action"] not in ("BUY", "SELL"):
                    continue
                i = int(bars.index.searchsorted(pd.Timestamp(r["t"])))
                if i < len(C) - 1:
                    cf = counterfactual(H, L, C, SP, i, s["direction"], s["entry"], s["sl"], s["tp"], horizon_bars)
                    if cf is not None:
                        rs.append(cf)
            out[f"{side}_hypothetical"] = {"n": len(rs), "exp_r": round(float(np.mean(rs)), 4) if rs else None}
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report")
    r.add_argument("--csv")
    r.add_argument("--offset", type=float, default=0.0)
    r.add_argument("--file", default="data/shadow_records.jsonl")
    a = ap.parse_args()
    recs = [json.loads(x) for x in Path(a.file).read_text().splitlines() if x.strip()] if Path(a.file).exists() else []
    bars = None
    if a.csv:
        from backtest import load_csv
        bars = load_csv(a.csv, a.offset)
    print(json.dumps(report(recs, bars), indent=2))


if __name__ == "__main__":
    main()
