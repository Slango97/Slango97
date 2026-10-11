"""V4 acceptance: macro context, target feasibility, caution window, M1 refinement logic, registry, drift,
learning gates, backtest speed-ups, costs, rejected opportunities, sessions."""
from helpers import Config, long_setup_views, tmpdir  # noqa: F401

import json
import os
from dataclasses import replace

import numpy as np
import pandas as pd

from backtest import BTConfig, Backtester, counterfactual, synthetic_m5
from config import phases_for
from context import macro_context, macro_levels
from costs import CostModel
from decision import Decision, apply_policy
from drift import drift_report, psi
from learning import apply_production, evaluate_gates
from levels import compute as compute_levels
from liquidity import build_map
from news import NewsContext
from refine import m1_check
from registry import PromotionError, Registry

CFG = Config()


def daily(n=600, start="2023-01-02"):
    idx = pd.date_range(start, periods=n, freq="1D")
    rng = np.random.default_rng(3)
    c = 1800 + np.cumsum(rng.normal(0, 8, n))
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) + 3, "low": np.minimum(o, c) - 3, "close": c, "tick_volume": 100.0}, index=idx)


def test_monthly_and_yearly_reference_levels():
    df = daily()
    lv = macro_levels(df)
    cur, prev = df[df.index.to_period("M") == df.index[-1].to_period("M")], df[df.index.to_period("M") == df.index[-1].to_period("M") - 1]
    assert lv["MO"] == cur["open"].iloc[0] and lv["PMH"] == prev["high"].max() and lv["PML"] == prev["low"].min()
    py = df[df.index.year == df.index[-1].year - 1]
    assert lv["PYH"] == py["high"].max() and lv["YO"] == df[df.index.year == df.index[-1].year]["open"].iloc[0]
    ctx = macro_context(lv, df["close"].iloc[-1], 15.0)
    assert any("monthly open" in n for n in ctx["notes"]) and any("nearest macro extreme" in n for n in ctx["notes"])
    v = long_setup_views()
    v["D1"]["macro_levels"] = {"PMH": 2030.0, "PML": 1950.0, "CMH": 2010.0, "CML": 1990.0, "PYH": 2100.0, "PYL": 1800.0, "CYH": 2020.0, "CYL": 1900.0}
    kinds = {p.kind for p in build_map(v, 2000.0).pools}
    assert {"PMH", "PML", "PYH", "PYL"} <= kinds


def test_structure_aware_target_feasibility():
    v = long_setup_views()
    S, vz, vs = CFG.strategy.styles["day"], v["H1"], v["H4"]
    zone = {"top": 2001.0, "bottom": 1993.0, "ob": vz["ob"][0], "fvg": None, "overlap": False, "quality": 80, "tf": "H1"}
    free, _ = compute_levels("TREND_PULLBACK", 1, 2000.0, zone, vz, vs, None, "STRONG_TREND", S, CFG, 0.25)
    risk = free["risk"]
    lv, why = compute_levels("TREND_PULLBACK", 1, 2000.0, zone, vz, vs, None, "STRONG_TREND", S, CFG, 0.25, macro={"PMH": 2000 + 1.2 * risk})
    assert lv is None and "PMH" in why and "unrealistic" in why
    lv, _ = compute_levels("TREND_PULLBACK", 1, 2000.0, zone, vz, vs, None, "STRONG_TREND", S, CFG, 0.25, macro={"PMH": 2000 + 2.4 * risk})
    assert lv and "capped before PMH" in lv["target_kind"] and lv["rr"] < 2.4 and lv["barriers"][0]["name"] == "PMH"
    vs2 = dict(vs, ob=[{"type": "bear", "top": 2000 + 2.2 * risk, "bottom": 2000 + 1.0 * risk, "idx": 1, "disp": 2, "tests": 0, "quality": 80}])
    lv, why = compute_levels("TREND_PULLBACK", 1, 2000.0, zone, vz, vs2, None, "STRONG_TREND", S, CFG, 0.25)
    assert lv is None and "order block" in why
    off = replace(CFG, features=replace(CFG.features, target_feasibility_v4=False))
    assert compute_levels("TREND_PULLBACK", 1, 2000.0, zone, vz, vs2, None, "STRONG_TREND", S, off, 0.25)[0] is not None


def test_caution_window_blocks_scalps_but_is_not_a_blackout():
    c = Decision(direction=1, style="scalp", setup="FVG_RETEST", tf="M15", entry=2000, sl=1995, tp=2010, score=80, technical=80,
                 confirmed=True, triggered=True, tags=["FVG_RETEST"], roles={"confirm": "M5", "entry": "M5"})
    ms = type("M", (), {"session": "LONDON_OPEN", "regime": None, "micro": None})()
    d = apply_policy([c], ms, NewsContext(event_phase="CAUTION"), CFG)
    assert not d.is_entry and d.action == "WAIT" and any(b["code"] == "NEWS" for b in c.blockers)
    comps = {"mtf": 25.0, "structure": 8.0, "zone": 18.0, "location": 8.0, "liquidity": 8.0, "momentum": 8.0, "room": 4.0, "volatility": 4.0, "session": 4.0}
    c2 = Decision(**{**c.__dict__, "style": "day", "blockers": [], "candidates": [], "components": comps})
    assert apply_policy([c2], ms, NewsContext(event_phase="CAUTION"), CFG).is_entry


def m1view(event=None, sweep=None, mom=0, close=2000.0, ema=1999.0):
    return {"df_len": 200, "last_event": event, "sweep": sweep, "ms": {"momentum_dir": mom}, "close": close, "ema20": ema}


def test_m1_refinement_confirms_waits_and_aborts():
    dec = Decision(direction=1, entry=2000.0, sl=1995.0, tp=2010.0)
    ev = lambda d, idx=198: {"type": "BOS", "dir": d, "idx": idx}
    assert m1_check(dec, m1view(event=ev(1)), 2000.5, CFG).action == "GO"
    assert m1_check(dec, m1view(event=ev(-1)), 2000.5, CFG).action == "WAIT"
    assert m1_check(dec, m1view(mom=1), 2000.2, CFG).action == "GO"
    assert m1_check(dec, m1view(event=ev(1)), 2002.5, CFG).action == "WAIT"        # 0.5R: no chasing
    assert m1_check(dec, m1view(event=ev(1)), 1996.5, CFG).action == "ABORT"       # 0.7R toward the stop
    assert m1_check(dec, m1view(), 1994.0, CFG).action == "ABORT"
    assert m1_check(dec, None, 2000.0, CFG).action == "WAIT"


def test_registry_requires_passed_gates_and_approval_and_can_roll_back():
    reg = Registry(tmpdir())
    bad = reg.register("params", {"strategy.min_score": 70.0}, {"gates_passed": False})
    try:
        reg.promote(bad, "me")
        raise AssertionError
    except PromotionError:
        pass
    a = reg.register("params", {"strategy.min_score": 70.0}, {"gates_passed": True})
    try:
        reg.promote(a, "")
        raise AssertionError
    except PromotionError:
        pass
    reg.promote(a, "alice", "WFO passed")
    b = reg.register("params", {"strategy.min_score": 72.0}, {"gates_passed": True})
    reg.promote(b, "alice")
    assert reg.production("params")["id"] == b and reg.m["versions"][a]["status"] == "retired"
    assert reg.rollback("params", "alice", "live drift") == a and reg.production("params")["payload"] == {"strategy.min_score": 70.0}
    p = os.path.join(reg.root, "artifacts", f"{a}.json")
    blob = json.loads(open(p).read())
    blob["payload"]["strategy.min_score"] = 10.0
    open(p, "w").write(json.dumps(blob))
    try:
        reg.production("params")
        raise AssertionError("tampering not detected")
    except PromotionError:
        pass


def test_approved_parameters_apply_at_startup_only_via_registry():
    d = tmpdir()
    cfg = replace(CFG, learning=replace(CFG.learning, registry_dir=d))
    assert apply_production(cfg) == (cfg, None)
    reg = Registry(d)
    vid = reg.register("params", {"strategy.min_score": 71.0}, {"gates_passed": True})
    reg.promote(vid, "alice")
    new, pv = apply_production(cfg)
    assert pv == vid and new.strategy.min_score == 71.0 and cfg.strategy.min_score == 65.0


def test_drift_detection_outcomes_and_distributions():
    lc = CFG.learning
    model = {"r0": 0.3, "p0": 0.5, "reference": {"score_hist": [0, 40, 30, 20, 8, 2], "regime_counts": {"STRONG_TREND": 50, "RANGE": 50}}}
    good = [{"r_multiple": x, "exp_r": 0.3, "p_win": 0.5} for x in [2.0, -1.0] * 25]
    assert drift_report(model, good, [], lc)["status"] == "OK"
    bad = [{"r_multiple": -1.0, "exp_r": 0.3, "p_win": 0.5}] * 40 + [{"r_multiple": 2.0, "exp_r": 0.3, "p_win": 0.5}] * 5
    rep = drift_report(model, bad, [], lc)
    assert rep["status"] == "DRIFT" and rep["details"]["z_expectancy"] < -2
    recs = [{"regime": {"label": "EXTREME"}, "candidates": [{"score": 90}]}] * 60
    assert drift_report(model, good, recs, lc)["status"] == "WATCH"
    assert psi([10, 20, 30], [10, 20, 30]) < 1e-9 and psi({"a": 50, "b": 50}, {"a": 95, "b": 5}) > 0.25


def test_learning_gates_require_every_check():
    lc = CFG.learning
    wf = {"oos_optimized": {"n": 150, "exp_r": 0.2, "pf": 1.4}, "oos_defaults": {"exp_r": 0.1}, "param_stability": 0.75}
    good = dict(stress={"exp_r": 0.1}, sens_share=0.9, mc={"p_negative": 0.05, "max_dd_r_p95": 9.0},
                cal_rep={"oos_reliability": {"brier": 0.22, "baseline_brier": 0.24}}, abl={"without": {}})
    gates, ok = evaluate_gates(lc, wf, **good)
    assert ok and len([g for g in gates if g["required"]]) == 10
    for change in ({"stress": {"exp_r": -0.05}}, {"sens_share": 0.5}, {"mc": {"p_negative": 0.4, "max_dd_r_p95": 9.0}},
                   {"cal_rep": {"oos_reliability": {"brier": 0.30, "baseline_brier": 0.24}}}):
        assert not evaluate_gates(lc, wf, **{**good, **change})[1]
    few = dict(wf, oos_optimized={"n": 40, "exp_r": 0.5, "pf": 3.0})
    assert not evaluate_gates(lc, few, **good)[1]


def test_precomputed_and_parallel_signals_match_the_reference():
    sim = synthetic_m5(4500, seed=8)
    off = replace(CFG, features=replace(CFG.features, precomputed_indicators=False))
    a = Backtester(CFG, BTConfig(decision_step=9, bars=300))
    b = Backtester(off, BTConfig(decision_step=9, bars=300))
    pa, pb = a.prepare(sim, 5), b.prepare(sim, 5)
    sa, sb = a.signals(pa), b.signals(pb)
    assert [s.i for s in sa] == [s.i for s in sb]
    same = sum((x.regime.label if x.regime else None) == (y.regime.label if y.regime else None) for x, y in zip(sa, sb))
    assert same / len(sa) >= 0.95                              # causal precompute ~ windowed (EMA warm-up differs slightly)
    par = a.signals(pa, workers=2)
    key = lambda s: (s.i, s.regime.label if s.regime else None, len(s.cands), [round(c.score, 3) for c in s.cands])
    assert [key(s) for s in par] == [key(s) for s in sa]       # parallel == sequential


def test_cost_model_and_session_spreads():
    cm = CostModel()
    assert cm.slippage(0.25, 2.0, "NY_OPEN") > cm.slippage(0.25, 2.0, "LONDON_CONT")
    assert cm.slippage(0.25, 2.0, "LONDON_CONT", near_event=True) > cm.slippage(0.25, 2.0, "LONDON_CONT")
    assert cm.spread(float("nan"), 0.25, "SYDNEY") > cm.spread(float("nan"), 0.25, "LONDON_OPEN") and cm.spread(0.3, 0.25, "ASIA") == 0.3
    btr = Backtester(CFG, BTConfig())
    prep = btr.prepare(synthetic_m5(3000, seed=1).assign(spread=np.nan), 5)
    sp = btr.spreads(prep)
    btr.costs.spread_mult = 2.0
    assert np.allclose(btr.spreads(prep), 2 * sp) and sp.max() > sp.min()


def test_counterfactual_outcomes_of_rejected_candidates():
    H = np.array([100, 101, 103, 104, 99.0])
    L = np.array([99, 99.5, 100.5, 102, 95.0])
    C = np.array([100, 100.5, 102.5, 103.5, 96.0])
    SP = np.zeros(5)
    assert counterfactual(H, L, C, SP, 0, 1, 100.0, 98.0, 103.0, 10) == 1.5        # target first
    assert counterfactual(H, L, C, SP, 0, -1, 100.0, 102.0, 95.0, 10) == -1.0       # stop first
    assert abs(counterfactual(H, L, C, SP, 0, 1, 100.0, 90.0, 120.0, 3) - 0.35) < 1e-9   # open at horizon: (103.5-100)/10


def test_m1_backtest_replays_refinement_and_reports_sessions():
    m5 = synthetic_m5(30000, seed=5)
    m1 = m5.iloc[:30000].copy()
    m1.index = pd.date_range("2024-01-01", periods=len(m1), freq="1min")
    m1 = m1[m1.index.dayofweek < 5]
    btr = Backtester(CFG, BTConfig(decision_step=12, bars=300))
    prep = btr.prepare(m1, 1)
    assert prep.m1 is not None and "macd_hist" in prep.m1
    tr, eq, summ = btr.simulate(prep, btr.signals(prep))
    assert "m1_refinement" in summ
    from analytics import session_report
    rows = session_report(prep, tr)
    assert {r["session"] for r in rows} >= {"ASIA", "LONDON_OPEN", "NY_OPEN"}
    split = phases_for(replace(CFG.sessions, split_asia=True))
    assert [p[0] for p in split][0] == "TOKYO" and "SYDNEY" in [p[0] for p in split]
