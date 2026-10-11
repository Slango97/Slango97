"""Trade-quality analytics that turn records into findings (never into automatic parameter changes):
- MFE / MAE: is the stop too tight, break-even too early, the target too greedy?
- Execution: how much R is lost between signal and fill, by session phase."""
import numpy as np
import pandas as pd


def _pct(s, q):
    s = s.dropna()
    return round(float(np.percentile(s, q)), 3) if len(s) else None


def mfe_mae_report(tr, min_n=20):
    out = {"findings": []}
    if tr is None or len(tr) == 0 or "mfe_r" not in tr:
        return out
    t = tr.dropna(subset=["mfe_r", "mae_r", "r"])
    win, loss = t[t["r"] > 0], t[t["r"] < 0]
    out.update(n=len(t), avg_mfe_r=round(float(t["mfe_r"].mean()), 3), avg_mae_r=round(float(t["mae_r"].mean()), 3),
               edge_ratio=round(float(t["mfe_r"].mean() / max(-t["mae_r"].mean(), 1e-9)), 2),
               winners_mae_p80=_pct(-win["mae_r"], 80), losers_mfe_p50=_pct(loss["mfe_r"], 50),
               losers_reaching_1r=round(float((loss["mfe_r"] >= 1.0).mean()), 3) if len(loss) else None,
               winners_reaching_tp_share=round(float((win["mfe_r"] >= win["r"] * 0.95).mean()), 3) if len(win) else None)
    f = out["findings"]
    if len(win) >= min_n and out["winners_mae_p80"] is not None and out["winners_mae_p80"] < 0.4:
        f.append(f"80% of winners never went more than {out['winners_mae_p80']:.2f}R against: the stop may be wider than needed")
    if len(win) >= min_n and out["winners_mae_p80"] is not None and out["winners_mae_p80"] > 0.85:
        f.append("winners routinely come close to the stop: stops look tight, expect noise stop-outs")
    if len(loss) >= min_n and out["losers_reaching_1r"] is not None and out["losers_reaching_1r"] >= 0.3:
        f.append(f"{out['losers_reaching_1r']:.0%} of losers were +1R first: break-even / partials may come too late")
    if "be_regret" in t:
        be = t[t["be_regret"].notna()]
        if len(be) >= min_n:
            share = float(be["be_regret"].astype(bool).mean())
            out["be_regret_share"] = round(share, 3)
            if share >= 0.35:
                f.append(f"{share:.0%} of break-even exits would later have hit the original target: BE may be too aggressive")
    if not f and len(t) >= min_n:
        f.append("no clear MFE/MAE problem")
    if len(t) < min_n:
        f.append(f"only {len(t)} trades: too few for MFE/MAE conclusions")
    return out


def execution_report(tr, by="session", min_n=10):
    out = {"by": by, "rows": [], "findings": []}
    if tr is None or len(tr) == 0 or "slippage_r" not in tr:
        return out
    for k, g in tr.groupby(by):
        row = {by: k, "n": len(g), "slippage_r": round(float(g["slippage_r"].mean()), 4),
               "spread": round(float(g["spread"].mean()), 3) if "spread" in g else None,
               "avg_r": round(float(g["r"].mean()), 3)}
        if "latency_ms" in g and g["latency_ms"].notna().any():
            row["latency_ms"] = round(float(g["latency_ms"].mean()), 1)
        out["rows"].append(row)
        if len(g) >= min_n and row["slippage_r"] >= 0.1:
            out["findings"].append(f"{k}: execution costs {row['slippage_r']:.2f}R per trade on average")
    total = float(tr["slippage_r"].mean())
    out["avg_slippage_r"] = round(total, 4)
    if len(tr) >= min_n and "r" in tr:
        out["r_before_costs"] = round(float((tr["r"] + tr["slippage_r"]).mean()), 3)
    return out


def format_analytics(mm, ex):
    lines = ["", "MFE / MAE", "-" * 60]
    for k in ("n", "avg_mfe_r", "avg_mae_r", "edge_ratio", "winners_mae_p80", "losers_mfe_p50", "losers_reaching_1r",
              "winners_reaching_tp_share", "be_regret_share"):
        if mm.get(k) is not None:
            lines.append(f"{k:<28}{mm[k]}")
    lines += [f"* {x}" for x in mm.get("findings", [])]
    lines += ["", f"EXECUTION BY {ex.get('by', '').upper()}", "-" * 60]
    if ex.get("rows"):
        lines.append(pd.DataFrame(ex["rows"]).to_string(index=False))
    lines += [f"* {x}" for x in ex.get("findings", [])]
    return "\n".join(lines)


def session_report(prep, tr, SP=None):
    """Measured session behaviour: spread, volatility, liquidity proxy, and (if traded) expectancy and slippage."""
    SP = prep.SP if SP is None else SP
    rows = []
    rng = prep.H - prep.L
    for ph in sorted(set(prep.phase)):
        m = prep.phase == ph
        row = {"session": ph, "bars": int(m.sum()), "avg_spread": round(float(np.mean(SP[m])), 3),
               "avg_range": round(float(np.mean(rng[m])), 3), "avg_tick_volume": round(float(np.mean(prep.TV[m])), 1)}
        if tr is not None and len(tr) and "session" in tr:
            g = tr[tr["session"] == ph]
            row.update(trades=len(g), exp_r=round(float(g["r"].mean()), 3) if len(g) else None,
                       slippage_r=round(float(g["slippage_r"].mean()), 4) if len(g) and "slippage_r" in g else None)
        rows.append(row)
    return rows


def blocker_report(stats, tr=None):
    """Is each blocker removing losing trades? Compare what blocked candidates would have made with what was traded."""
    if not stats:
        return ""
    traded = float(tr["r"].mean()) if tr is not None and len(tr) else None
    lines = ["", "", "REJECTED OPPORTUNITIES (counterfactual R of blocked candidates)", "-" * 60]
    for k, v in sorted(stats.items(), key=lambda kv: -kv[1]["n"]):
        verdict = ""
        if v["n"] >= 30 and traded is not None:
            verdict = "  <- blocked trades did BETTER than traded ones: review this blocker" if v["avg_cf_r"] > traded else "  ok: filters weaker trades"
        lines.append(f"{k:<16} n={v['n']:<5} avg R {v['avg_cf_r']:+.3f}  win {v['cf_win_rate']:.0%}{verdict}")
    if traded is not None:
        lines.append(f"(traded trades average {traded:+.3f}R)")
    return "\n".join(lines)


def gate_funnel(rejected):
    """Which single gate costs the most trades? For every blocker: how often it fires at all, how often it is the
    ONLY blocker (removing just that gate would have let the trade through), and what those trades would have made."""
    if rejected is None or len(rejected) == 0:
        return []
    rows = []
    lists = rejected["blockers"].astype(str).str.split(",")
    codes = sorted({c for l in lists for c in l if c})
    for code in codes:
        anym = lists.apply(lambda l: code in l)
        sole = lists.apply(lambda l: l == [code])
        cf = rejected.loc[sole, "cf_r"].dropna()
        rows.append({"gate": code, "fires": int(anym.sum()), "sole_blocker": int(sole.sum()),
                     "sole_avg_cf_r": round(float(cf.mean()), 3) if len(cf) else None,
                     "sole_win_rate": round(float((cf > 0).mean()), 3) if len(cf) else None})
    return sorted(rows, key=lambda r: -r["sole_blocker"])


def format_funnel(rows):
    if not rows:
        return ""
    lines = ["", "GATE FUNNEL (sole blocker = removing only this gate would have allowed the trade)", "-" * 60]
    for r in rows:
        tail = f" -> those trades avg {r['sole_avg_cf_r']:+.3f}R, win {r['sole_win_rate']:.0%}" if r["sole_avg_cf_r"] is not None else ""
        lines.append(f"{r['gate']:<16} fires {r['fires']:<6} sole {r['sole_blocker']:<6}{tail}")
    return "\n".join(lines)
