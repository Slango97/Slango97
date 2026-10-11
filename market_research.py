"""Per-market walk-forward research (V5.6 roadmap step 1).

For every market of the watchlist this runs the SAME signal / policy / risk / management code as live trading
through anchored walk-forward folds, then judges the out-of-sample (OOS) trades of the DEFAULT configuration
against the Strategies Summary acceptance criteria:

    OOS trades >= LEARNING__MIN_OOS_TRADES, profit factor >= 1.3, annualised Sharpe >= 0.8,
    max drawdown < 15% (at the configured risk per trade), still PF >= 1.0 with spread x1.5 / slippage x2,
    Monte Carlo P(total < 0) <= LEARNING__MC_MAX_P_NEGATIVE.

Typical use on the MT5 workstation:

    python export_history.py --markets WATCHLIST --years 3 --out history     # broker data, one folder per market
    python market_research.py --history history --folds 5 --workers 4 --publish

`--publish` writes data/market_evidence.json, which the adaptive selector and the engine read when
BRAIN__REQUIRE_MARKET_EVIDENCE=true (markets without a fresh PASS are never selected or traded).

    python market_research.py --synthetic 20000 --markets EURUSD,XAUUSD     # plumbing check ONLY

Synthetic runs are labelled as such and can never be published: they say nothing about real markets.
"""
import argparse
import json
import math
import pickle
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from backtest import BTConfig, Backtester, load_csv, synthetic_m5
from config import Config
from costs import CostModel
from instruments import profile_for
from market_specs import canonical, commission_in_profit_currency, spec_for
from optimize import Optimizer, monte_carlo, r_summary

# ---------------------------------------------------------------- data discovery


def discover(history, markets=None):
    """{market: (csv_path, meta_dict)} from history/<MARKET>/<SYM>_M1|M5.csv (+export_meta.json) or flat files."""
    root = Path(history)
    found = {}
    for csv in sorted(root.rglob("*_M[15].csv")):
        sym = csv.name.rsplit("_", 1)[0]
        mk = canonical(sym) or canonical(csv.parent.name)
        if not mk or (markets and mk not in markets):
            continue
        meta = {}
        for mp in (csv.parent / "export_meta.json", root / "export_meta.json"):
            if mp.exists():
                try:
                    m = json.loads(mp.read_text())
                    if canonical(m.get("symbol")) == mk:
                        meta = m
                        break
                except Exception:
                    pass
        prev = found.get(mk)
        if prev is None or (csv.name.endswith("_M1.csv") and not prev[0].name.endswith("_M1.csv")):
            found[mk] = (csv, meta)
    return found


def synthetic_market(market, n, seed):
    """Synthetic bars scaled to the market's price level and spread. PLUMBING ONLY."""
    level = {"XAUUSD": 2400, "XAGUSD": 30, "EURUSD": 1.08, "GBPUSD": 1.27, "AUDUSD": 0.66, "USDJPY": 150,
             "USDCAD": 1.37, "USDCHF": 0.88, "US30": 42000, "NAS100": 20000, "USOIL": 75, "BTCUSD": 60000}.get(market, 1.0)
    df = synthetic_m5(n, seed=seed)
    # synthetic_m5 moves ~0.02% per bar around 2000; real M5 bars move ~0.05-0.1%: widen to a realistic scale.
    amp = 4.0
    for c in ("open", "high", "low", "close"):
        df[c] = level * (1.0 + amp * (df[c] - 2000.0) / 2000.0)
    df["spread"] = np.nan                       # use the market spec's typical spread
    return df


# ---------------------------------------------------------------- metrics


def _naive(col):
    t = pd.to_datetime(col)
    return t.dt.tz_convert(None) if t.dt.tz is not None else t


def acceptance_metrics(trades, risk_pct, periods_per_year=252):
    """Account-level OOS statistics from R-multiples at a fixed risk per trade (currency-agnostic)."""
    if trades is None or len(trades) == 0:
        return {"n": 0}
    tr = trades.dropna(subset=["r"]).sort_values("close_time")
    r = tr["r"].astype(float).values
    f = risk_pct / 100.0
    growth = np.maximum(1e-9, 1.0 + f * r)
    eq = np.cumprod(growth)
    peak = np.maximum.accumulate(np.r_[1.0, eq])[1:]
    max_dd = float(np.max(1.0 - eq / peak)) * 100 if len(eq) else 0.0
    days = _naive(tr["close_time"]).dt.normalize()
    daily = pd.Series(np.log(growth), index=days.values).groupby(level=0).sum()
    start = _naive(tr["open_time"]).min()
    full = pd.date_range(start.normalize(), daily.index.max(), freq="D" if periods_per_year > 300 else "B")
    daily = daily.reindex(full, fill_value=0.0)
    sd = float(daily.std(ddof=1)) if len(daily) > 2 else 0.0
    sharpe = float(daily.mean() / sd * math.sqrt(periods_per_year)) if sd > 0 else None
    wins, losses = r[r > 0].sum(), -r[r < 0].sum()
    years = max(len(full) / periods_per_year, 1e-9)
    cagr = float(eq[-1] ** (1 / years) - 1) * 100 if eq[-1] > 0 else -100.0
    return {"n": int(len(r)), "win_rate": round(float((r > 0).mean()), 3), "exp_r": round(float(r.mean()), 4),
            "pf": round(float(wins / losses), 3) if losses > 0 else (None if wins == 0 else float("inf")),
            "sharpe": None if sharpe is None else round(sharpe, 3), "max_dd_pct": round(max_dd, 2),
            "return_pct": round(float(eq[-1] - 1) * 100, 2), "cagr_pct": round(cagr, 2),
            "calmar": round(cagr / max_dd, 3) if max_dd > 0 else None, "days": int(len(full))}


def verdict(m, stress, mc, cfg):
    """PASS / FAIL / INSUFFICIENT with every failed criterion listed."""
    L = cfg.learning
    if m.get("n", 0) < L.min_oos_trades:
        return "INSUFFICIENT", [f"only {m.get('n', 0)} OOS trades (need {L.min_oos_trades})"]
    why = []
    pf = m.get("pf")
    if pf is None or pf < L.market_min_pf:
        why.append(f"PF {pf} < {L.market_min_pf}")
    if m.get("sharpe") is None or m["sharpe"] < L.market_min_sharpe:
        why.append(f"Sharpe {m.get('sharpe')} < {L.market_min_sharpe}")
    if m.get("max_dd_pct", 100) >= L.market_max_dd_pct:
        why.append(f"max DD {m.get('max_dd_pct')}% >= {L.market_max_dd_pct}%")
    spf = stress.get("pf")
    if spf is None or spf < L.market_stress_min_pf:
        why.append(f"stress PF {spf} < {L.market_stress_min_pf} (spread x{L.stress_spread_mult}, slippage x{L.stress_slippage_mult})")
    pneg = mc.get("p_negative")
    if pneg is None or pneg > L.mc_max_p_negative:
        why.append(f"Monte Carlo P(loss) {pneg} > {L.mc_max_p_negative}")
    return ("PASS" if not why else "FAIL"), why


# ---------------------------------------------------------------- one market


def bt_config(spec, sim, cfg):
    price = float(sim["close"].median())
    prof = profile_for(spec.market)
    cap = None
    if prof.asset_class in ("index", "energy", "crypto"):       # mirror the live dynamic spread cap
        m5 = sim[["open", "high", "low", "close"]].resample("5min").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
        from indicators import atr
        cap = cfg.execution.max_spread_atr_frac * float(atr(m5).median()) / spec.point
    return BTConfig(contract_size=spec.contract_size, point=spec.point, spread_default=spec.spread,
                    slippage=spec.slippage, commission_per_lot_side=commission_in_profit_currency(spec, price),
                    spread_cap_points=cap, trades_weekends=spec.trades_weekends or prof.trades_weekends)


def research_market(market, sim, step, spec, cfg, *, folds=5, workers=0, cache_dir=None, decision_step=1):
    bt = replace(bt_config(spec, sim, cfg), decision_step=decision_step)
    btr = Backtester(cfg, bt)
    btr.costs = CostModel(base_slippage=spec.slippage)          # price-unit slippage of THIS market
    prep = btr.prepare(sim, step)
    fp = {"market": market, "n": len(prep.idx), "first": str(prep.idx[0]), "last": str(prep.idx[-1]),
          "step": decision_step, "spec": spec.to_dict()}
    sig = None
    cache = Path(cache_dir) / f"{market}_signals.pkl" if cache_dir else None
    if cache is not None and cache.exists():
        blob = pickle.loads(cache.read_bytes())
        if blob.get("fingerprint") == fp:
            sig = blob["signals"]
    if sig is None:
        sig = btr.signals(prep, workers=workers)
        if cache is not None:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(pickle.dumps({"signals": sig, "fingerprint": fp}))
    opt = Optimizer(btr, prep, sig, min_trades=30)
    wf, oos_opt, oos_base = opt.walk_forward("entry", folds, anchored=True)
    ppy = 365 if (spec.trades_weekends or profile_for(market).trades_weekends) else 252
    m = acceptance_metrics(oos_base, cfg.risk.risk_per_trade_pct, ppy)
    # Cost stress on the same OOS windows: wider spreads + worse fills.
    L = cfg.learning
    sbt = replace(bt, slippage=bt.slippage * L.stress_slippage_mult)
    sbtr = Backtester(cfg, sbt)
    sbtr.costs = replace(btr.costs, spread_mult=L.stress_spread_mult, slippage_mult=L.stress_slippage_mult)
    sprep = replace(prep, SP=prep.SP * L.stress_spread_mult)
    sopt = Optimizer(sbtr, sprep, sig, min_trades=30)
    parts = [sopt.window(cfg, lo, hi) for lo, hi in sopt.folds(folds)[1:]]
    parts = [p for p in parts if len(p)]
    stress = r_summary(pd.concat(parts, ignore_index=True)) if parts else {"n": 0}
    mc = monte_carlo(oos_base["r"].dropna().values if len(oos_base) else [], n_sims=L.mc_sims, block_len=L.mc_block_len)
    status, reasons = verdict(m, stress, mc, cfg)
    if spec.source != "broker_export":
        reasons = reasons + ["costs are DEFAULT estimates, not this broker's (export with export_history.py --markets)"]
    return {"market": market, "status": status, "reasons": reasons, "metrics": m, "stress": stress,
            "monte_carlo": mc, "walk_forward": {"verdict": wf["verdict"], "param_stability": wf["param_stability"],
                                                "oos_optimized": wf["oos_optimized"],
                                                "folds": [{k: f[k] for k in ("fold", "train", "test", "oos_defaults")}
                                                          for f in wf["folds"]]},
            "period": f"{prep.idx[0]:%Y-%m-%d}..{prep.idx[-1]:%Y-%m-%d}", "bars": int(len(prep.idx)),
            "spec": spec.to_dict()}, oos_base


# ---------------------------------------------------------------- report


def markdown(summary):
    L = summary["criteria"]
    lines = [f"# Per-market walk-forward report — {summary['generated_at'][:16]} UTC", ""]
    if summary.get("synthetic"):
        lines += ["> **SYNTHETIC DATA — PLUMBING CHECK ONLY. These numbers say nothing about real markets and are "
                  "never published as evidence.**", ""]
    lines += [f"Criteria: OOS trades >= {L['min_trades']}, PF >= {L['min_pf']}, Sharpe >= {L['min_sharpe']}, "
              f"max DD < {L['max_dd_pct']}% at {L['risk_pct']}% risk/trade, stress PF >= {L['stress_min_pf']}, "
              f"Monte Carlo P(loss) <= {L['mc_max_p_negative']}.", "",
              "| Market | Status | OOS trades | Win % | Exp R | PF | Sharpe | Max DD % | Stress PF | MC P(loss) | Period |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for mk, row in summary["markets"].items():
        m, s, mc = row.get("metrics", {}), row.get("stress", {}), row.get("monte_carlo", {})
        lines.append(f"| {mk} | **{row['status']}** | {m.get('n', 0)} | "
                     f"{'' if m.get('win_rate') is None else round(100 * m['win_rate'], 1)} | {m.get('exp_r', '')} | "
                     f"{m.get('pf', '')} | {m.get('sharpe', '')} | {m.get('max_dd_pct', '')} | {s.get('pf', '')} | "
                     f"{mc.get('p_negative', '')} | {row.get('period', '')} |")
    lines.append("")
    for mk, row in summary["markets"].items():
        if row.get("reasons"):
            lines.append(f"- **{mk}**: " + "; ".join(row["reasons"]))
    passed = [k for k, v in summary["markets"].items() if v["status"] == "PASS"]
    lines += ["", f"Markets allowed when BRAIN__REQUIRE_MARKET_EVIDENCE=true: {', '.join(passed) or 'NONE'}"]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--history", default="history", help="folder written by export_history.py --markets")
    ap.add_argument("--markets", default="", help="comma list (default: the configured watchlist)")
    ap.add_argument("--synthetic", type=int, default=0, help="N synthetic M5 bars per market (plumbing check only)")
    ap.add_argument("--offset", type=float, default=None, help="server UTC offset (default: from export_meta.json)")
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--decision-step", type=int, default=1)
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--out", default="research/markets")
    ap.add_argument("--publish", action="store_true", help="write data/market_evidence.json (real data only)")
    a = ap.parse_args()
    cfg = Config.from_env()
    wanted = [canonical(x) or x.strip().upper() for x in (a.markets.split(",") if a.markets else cfg.brain.watchlist) if x.strip()]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if a.synthetic:
        sources = {mk: (None, {}) for mk in wanted}
    else:
        sources = discover(a.history, set(wanted))
    summary = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "synthetic": bool(a.synthetic),
               "criteria": {"min_trades": cfg.learning.min_oos_trades, "min_pf": cfg.learning.market_min_pf,
                            "min_sharpe": cfg.learning.market_min_sharpe, "max_dd_pct": cfg.learning.market_max_dd_pct,
                            "stress_min_pf": cfg.learning.market_stress_min_pf, "risk_pct": cfg.risk.risk_per_trade_pct,
                            "mc_max_p_negative": cfg.learning.mc_max_p_negative, "folds": a.folds},
               "markets": {}}
    for k, mk in enumerate(wanted):
        if mk not in sources:
            summary["markets"][mk] = {"status": "NO_DATA", "reasons": [f"no history found for {mk} under {a.history}"]}
            print(f"{mk}: NO_DATA")
            continue
        csv, meta = sources[mk]
        if a.synthetic:
            sim, step = synthetic_market(mk, a.synthetic, seed=11 + k), 5
            symbol = mk
        else:
            off = a.offset if a.offset is not None else float(meta.get("server_utc_offset_h_now") or 0.0)
            sim, step = load_csv(csv, off), (1 if csv.name.endswith("_M1.csv") else 5)
            symbol = meta.get("symbol") or csv.name.rsplit("_", 1)[0]
        if a.start:
            sim = sim[sim.index >= a.start]
        if a.end:
            sim = sim[sim.index < a.end]
        spec = spec_for(mk, meta)
        print(f"{mk}: {len(sim)} bars ({spec.source} costs) — generating signals and walk-forward...")
        try:
            row, oos = research_market(mk, sim, step, spec, cfg, folds=a.folds, workers=a.workers,
                                       cache_dir=out / "cache", decision_step=a.decision_step)
        except Exception as ex:
            summary["markets"][mk] = {"status": "ERROR", "reasons": [f"{type(ex).__name__}: {ex}"]}
            print(f"{mk}: ERROR {ex}")
            continue
        row["symbol"] = symbol
        summary["markets"][mk] = row
        if len(oos):
            oos.to_csv(out / f"{mk}_oos_trades.csv", index=False)
        print(f"{mk}: {row['status']} {row['metrics']} {'; '.join(row['reasons'])}")
    (out / "market_research.json").write_text(json.dumps(summary, indent=2, default=str))
    (out / "MARKET_RESEARCH_REPORT.md").write_text(markdown(summary))
    print(f"report: {out / 'MARKET_RESEARCH_REPORT.md'}")
    if a.publish:
        if a.synthetic:
            raise SystemExit("refusing to publish synthetic results as market evidence")
        target = Path(cfg.brain.market_evidence_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(summary, indent=2, default=str))
        tmp.replace(target)
        print(f"published: {target}")


if __name__ == "__main__":
    main()
