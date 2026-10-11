from helpers import Config, FakeClient, NS, candles, long_setup_views, pos, tmpdir, utc, zigzag  # noqa: F401

import os
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pandas as pd

from analysis import MarketState
from analytics import execution_report, mfe_mae_report
from calibration import Calibration
from confluence import assess
from decision import apply_policy, decide, generate
from diagnostics import diagnose
from indicators import atr
from journal import Journal
from levels import compute as compute_levels
from liquidity import build_map
from microstructure import score_market
from news import NewsContext
from optimize import Optimizer, apply, grid, monte_carlo, r_drawdown
from portfolio import correlation, portfolio_risk_pct
from regime import Regime, classify as classify_regime
from risk import RiskCore, RiskEngine
from sessions import session_context
from setups import EdgeBook, classify as classify_setup
from smc import displacement, structure
from state import State

CFG = Config()


def ms(views, session="LONDON_OPEN", spread=0.25, **kw):
    m = MarketState(views=views, session=session, time=None, spread=spread)
    for k, v in kw.items():
        setattr(m, k, v)
    return m


def micro(score=95.0, band="NORMAL"):
    return SimpleNamespace(score=score, band=band, flags=[], range_ratio=1.0, spread_ratio=1.0)


# ---------------- regime ----------------
def test_regime_classification_priorities():
    v = long_setup_views()
    assert classify_regime(v, CFG, micro(10, "ABNORMAL")).label == "ABNORMAL"
    assert classify_regime(v, CFG, micro(), NewsContext(event_phase="PRE")).label == "NEWS_DRIVEN"
    v["H1"]["vol_regime"] = "EXTREME"
    r = classify_regime(v, CFG, micro())
    assert r.label == "EXTREME" and r.no_trade
    v = long_setup_views()
    v["H1"]["last_event"] = {"type": "BOS", "dir": 1, "idx": 297, "level": 1990, "quality": 85, "kind": "displacement", "holds": True}
    assert classify_regime(v, CFG, micro()).label == "BREAKOUT"
    v["H1"]["last_event"] = {"type": "CHoCH", "dir": -1, "idx": 295, "level": 1990, "quality": 70, "kind": "strong", "holds": True}
    r = classify_regime(v, CFG, micro())
    assert r.label == "REVERSAL" and r.direction == -1
    v = long_setup_views()
    assert classify_regime(v, CFG, micro()).label == "STRONG_TREND"                     # fixture: strength 80, ADX 26
    for tf in ("H1", "H4"):
        v[tf]["ms"] = {**v[tf]["ms"], "trend_dir": 0, "trend_strength": 10}
    v["H1"].update(adx=15.0, bb_width_pct=0.1)
    assert classify_regime(v, CFG, micro()).label == "COMPRESSION"
    v["H1"]["bb_width_pct"] = 0.6
    assert classify_regime(v, CFG, micro()).label == "RANGE"


def test_regime_gate_enables_only_matching_setups_and_halts_when_no_trade():
    v = long_setup_views()
    rng = Regime("RANGE", 0, 0.5, [], tuple(CFG.regime.allowed["RANGE"]))
    d = decide(ms(v, regime=rng), 2000.0, NewsContext(), CFG)
    assert not d.is_entry and d.wait_reason in ("REGIME", "NO_SETUP")
    trend = Regime("STRONG_TREND", 1, 0.8, [], tuple(CFG.regime.allowed["STRONG_TREND"]))
    assert decide(ms(v, regime=trend), 2000.0, NewsContext(), CFG).is_entry
    ext = Regime("EXTREME", 0, 1.0, ["x"], (), True)
    d = decide(ms(v, regime=ext), 2000.0, NewsContext(), CFG)
    assert d.action == "HALT" and d.wait_reason == "REGIME"
    off = replace(CFG, policy=replace(CFG.policy, use_regime_filter=False))
    assert decide(ms(v, regime=rng), 2000.0, NewsContext(), off).is_entry               # ablation switch works


def test_confirmation_and_market_quality_gates():
    v = long_setup_views()
    v["M15"]["last_event"], v["M15"]["sweep"] = None, None                              # nothing confirms on M15
    d = decide(ms(v), 2000.0, NewsContext(), CFG)
    # V5.2 moderate-veto policy: a missing confirmation is a soft risk reduction
    # when an independent lower-timeframe trigger and strong confluence remain.
    assert d.is_entry and d.policy_risk_mult < 1.0
    off = replace(CFG, policy=replace(CFG.policy, use_hierarchy_confirmation=False))
    assert decide(ms(v), 2000.0, NewsContext(), off).is_entry
    d = decide(ms(long_setup_views(), micro=micro(30, "NO_TRADE")), 2000.0, NewsContext(), CFG)
    assert d.action == "WAIT" and d.wait_reason == "MARKET_QUALITY"


def test_contradictions_turn_a_buy_into_wait():
    v = long_setup_views()
    hostile = NewsContext(status="OK", bias=-0.6, confidence=0.4, drivers=[{"driver": "yields up", "effect": "bearish"},
                                                                          {"driver": "hawkish Fed", "effect": "bearish"}],
                          event_bias=-1.0)
    v["D1"]["ema_trend"] = v["D1"]["struct_trend"] = -1
    c = assess(1, v, CFG.hierarchy.roles["day"], None, hostile, CFG)
    assert c.index >= CFG.policy.conflict_reject and c.verdict == "CONFLICTING" and "event_surprise" in c.opposing
    d = decide(ms(v, news=hostile), 2000.0, NewsContext(status="NO_DATA"), CFG)
    assert not d.is_entry and d.wait_reason in ("CONFLICT", "NO_SETUP")


def test_generate_then_policy_equals_decide_and_does_not_mutate_cache():
    v = long_setup_views()
    m = ms(v)
    cands, rej = generate(m, 2000.0, CFG)
    snapshot = [(c.setup, c.score, dict(c.components)) for c in cands]
    copies = [replace(c, components=dict(c.components), reasons=list(c.reasons), tags=list(c.tags)) for c in cands]
    d1 = apply_policy(copies, m, NewsContext(bias=1, confidence=1, status="OK"), CFG)
    assert d1.is_entry and [(c.setup, c.score, dict(c.components)) for c in cands] == snapshot


# ---------------- microstructure ----------------
def bars(n=80, spread=0.25):
    rng = np.random.default_rng(0)
    c = 2000 + np.cumsum(rng.normal(0, 0.3, n))
    o = np.r_[c[0], c[:-1]]
    return o, np.maximum(o, c) + 0.2, np.minimum(o, c) - 0.2, c, np.full(n, 100.0)


def test_market_quality_bands():
    mc = CFG.micro
    o, h, l, c, tv = bars()
    q = score_market(mc, 0.25, 0.25, o, h, l, c, tv, 0.40)
    assert q.band == "NORMAL" and q.score >= 90
    bad = score_market(mc, 1.2, 0.25, o, h, l, c, tv, 0.40)
    assert bad.score < q.score and bad.band in ("NO_TRADE", "ABNORMAL", "CAUTIOUS") and any("spread" in f for f in bad.flags)
    h2, l2 = h.copy(), l.copy()
    h2[-1], l2[-1] = c[-1] + 6, c[-1] - 6
    tv2 = tv.copy()
    tv2[-2:] = 2000
    spike = score_market(mc, 0.25, 0.25, o, h2, l2, c, tv2, 0.40)
    assert spike.components["velocity"] < 0 and spike.components["volume_spike"] < 0 and spike.range_ratio > 2


# ---------------- liquidity / displacement / sessions ----------------
def test_liquidity_map_sides_swept_status_and_draw():
    v = long_setup_views()
    v["M15"]["key_levels"] = {"PDH": 2010.0, "PDL": 1985.0, "CDH": 2012.0, "CDL": 1990.0, "ASIA_H": 2004.0, "ASIA_L": 1996.0}
    v["H1"]["eq_levels"] = [{"kind": "EQL", "level": 1993.0, "touches": 2, "last_idx": 280}]
    lm = build_map(v, 2000.0)
    kinds = {p.kind: p for p in lm.pools}
    assert kinds["PDH"].side == "buy" and kinds["PDH"].swept                         # today's high already beat PDH
    assert kinds["PDL"].side == "sell" and not kinds["PDL"].swept
    assert lm.nearest("buy").level > 2000 > lm.nearest("sell").level
    assert lm.draw() in (-1, 0, 1) and "liquidity" in lm.narrative() or lm.narrative()


def test_displacement_detection_real_vs_ordinary():
    rows = [(100 + (i % 2) * 0.2, 100.6, 99.6, 100.2 - (i % 2) * 0.2) for i in range(40)]
    rows += [(100.1, 104.6, 100.0, 104.5), (104.5, 105.5, 103.9, 105.2)]
    df = candles(rows)
    df.iloc[-2, df.columns.get_loc("tick_volume")] = 400.0
    a = atr(df)
    scores, real = displacement(df, a, structure(df, a)["all_events"])
    assert real and real[-1]["idx"] == 40 and real[-1]["dir"] == 1 and real[-1]["breaks"] and real[-1]["fvg"]
    assert scores[30] < 60


def test_session_context_detects_london_sweep_of_asia():
    idx = pd.date_range("2025-03-04 00:00", periods=12 * 12, freq="5min")
    n = len(idx)
    hi = np.full(n, 2005.0)
    lo = np.full(n, 1995.0)
    cl = np.full(n, 2000.0)
    london = (idx.hour >= 7) & (idx.hour < 12)
    hi[np.where(london)[0][3]] = 2008.0                                  # pokes above the Asia high, closes inside
    df = pd.DataFrame({"open": cl, "high": hi, "low": lo, "close": cl, "tick_volume": 100.0}, index=idx)
    ctx = session_context(df, 3.0, CFG.sessions.phases)
    assert ctx["asia_high"] == 2005.0 and "LONDON_SWEPT_ASIA_HIGH" in ctx["events"]


# ---------------- levels / setups ----------------
def test_dynamic_targets_follow_setup_type():
    v = long_setup_views()
    v["M15"]["key_levels"] = {"PDH": 2045.0, "PDL": 1985.0, "CDH": 2010.0, "CDL": 1990.0}
    lm = build_map(v, 2000.0)
    S, vz, vs = CFG.strategy.styles["day"], v["H1"], v["H4"]
    zone = {"top": 2001.0, "bottom": 1993.0, "ob": vz["ob"][0], "fvg": None, "overlap": False, "quality": 80, "tf": "H1"}
    lv, _ = compute_levels("LIQUIDITY_SWEEP_REVERSAL", 1, 2000.0, zone, vz, vs, lm, "REVERSAL", S, CFG, 0.25)
    assert lv and "liquidity" in lv["target_kind"]
    ev = {"type": "BOS", "dir": 1, "idx": 290, "level": 2002.0, "quality": 80}
    lv, _ = compute_levels("BREAKOUT_RETEST", 1, 2000.0, zone, vz, vs, lm, "BREAKOUT", S, CFG, 0.25, event=ev)
    assert lv and lv["target_kind"].startswith("measured move")
    lv, why = compute_levels("TREND_PULLBACK", 1, 2000.0, zone, vz, vs, lm, "STRONG_TREND", S, CFG, 0.25)
    assert lv and lv["target_kind"].startswith("structure") and lv["rr"] <= S.max_rr
    high = dict(vz, vol_regime="HIGH")
    lv_hi, _ = compute_levels("TREND_PULLBACK", 1, 2000.0, zone, high, vs, lm, "EXPANSION", S, CFG, 0.25)
    lv_lo, _ = compute_levels("TREND_PULLBACK", 1, 2000.0, zone, dict(vz, vol_regime="LOW"), vs, lm, "RANGE", S, CFG, 0.25)
    assert lv_hi["risk"] > lv_lo["risk"]                                     # wider stop in high volatility


def test_setup_classifier_tags():
    v = long_setup_views()
    vz, vc, vs = v["H1"], v["M15"], v["H4"]
    zone = {"top": 2001.0, "bottom": 1993.0, "ob": vz["ob"][0], "fvg": None, "overlap": False, "quality": 80, "tf": "H1"}
    primary, tags = classify_setup(1, 2000.0, zone, vz, vc, vs, CFG)
    assert "BOS_PULLBACK" in tags and "ORDER_BLOCK_RETEST" in tags and "TREND_PULLBACK" in tags
    vc2 = dict(vc, last_event={"type": "CHoCH", "dir": 1, "idx": 293, "level": 1995, "quality": 70, "kind": "strong"})
    primary, tags = classify_setup(1, 2000.0, zone, vz, vc2, vs, CFG)
    assert primary == "LIQUIDITY_SWEEP_REVERSAL"


# ---------------- calibration / edge book / sizing ----------------
def fake_trades(n=400, seed=1):
    rng = np.random.default_rng(seed)
    score = rng.uniform(60, 95, n)
    p = 0.3 + (score - 60) / 35 * 0.35                                      # higher score -> more wins
    win = rng.random(n) < p
    setup = np.where(rng.random(n) < 0.5, "FVG_RETEST", "BREAKOUT_RETEST")
    r = np.where(win, 2.0, -1.0) - np.where(setup == "BREAKOUT_RETEST", 0.6, 0.0)
    return pd.DataFrame({"score": score, "setup": setup, "r": r})


def test_calibration_is_monotone_shrunk_and_testable():
    tr = fake_trades()
    cal = Calibration.fit(tr.iloc[:300])
    assert all(a <= b + 1e-12 for a, b in zip(cal.m["p_win"], cal.m["p_win"][1:]))
    lo, hi = cal.predict(66), cal.predict(90)
    assert hi["p_win"] > lo["p_win"] and 0 < hi["confidence"] < 1
    assert cal.predict(80, "BREAKOUT_RETEST")["exp_r"] < cal.predict(80, "FVG_RETEST")["exp_r"]
    rel = cal.reliability_table(tr.iloc[300:])
    assert 0 <= rel["brier"] <= 1 and rel["rows"]
    p = os.path.join(tmpdir(), "cal.json")
    cal.save(p)
    assert Calibration.load(p).predict(80) == cal.predict(80)


def test_expected_r_gate_and_edge_book():
    v = long_setup_views()
    cal = Calibration({"bins": [0, 101], "n": [500], "p_win": [0.3], "exp_r": [-0.2], "p0": 0.3, "r0": -0.2, "k": 20, "setups": {}})
    on = replace(CFG, policy=replace(CFG.policy, use_calibration=True))
    d = decide(ms(v), 2000.0, NewsContext(), on, calibration=cal)
    assert not d.is_entry and d.wait_reason == "EXPECTANCY"
    good = Calibration({**cal.m, "p_win": [0.6], "exp_r": [0.4]})
    d = decide(ms(v), 2000.0, NewsContext(), on, calibration=good)
    assert d.is_entry and d.p_win == 0.6 and d.exp_r == 0.4
    tr = fake_trades()
    p = os.path.join(tmpdir(), "edge.json")
    book = EdgeBook.build(tr, p)
    eb = EdgeBook.load(p, min_trades=50)
    assert book["source"] == "walk_forward_oos" and eb.allowed("FVG_RETEST") and not eb.allowed("BREAKOUT_RETEST")
    with open(p, "w") as f:
        f.write('{"source": "in_sample", "setups": {}}')
    assert EdgeBook.load(p) is None                                           # in-sample evidence is refused


def test_sizing_tiers_never_exceed_base_and_step_down():
    v = long_setup_views()
    tiered = replace(CFG, sizing=replace(CFG.sizing, mode="tiered"))
    base = CFG.risk.risk_per_trade_pct
    d = decide(ms(v), 2000.0, NewsContext(), tiered)
    assert d.is_entry and d.risk_pct <= base + 1e-9
    caut = decide(ms(v, micro=micro(60, "CAUTIOUS")), 2000.0, NewsContext(), tiered)
    assert caut.is_entry and caut.risk_pct < d.risk_pct or d.risk_tier == "weak"
    assert decide(ms(v), 2000.0, NewsContext(), CFG).risk_pct == base         # fixed mode: unchanged


# ---------------- portfolio / throttle / diagnostics ----------------
def test_portfolio_correlation_and_aggregate_risk():
    pc = CFG.portfolio
    assert correlation("XAUUSD", "XAGUSD", pc) > 0.8 and correlation("XAUUSD", "USDJPY", pc) < 0
    same = portfolio_risk_pct([("XAUUSD", 0.5), ("XAGUSD", 0.5)], pc)
    hedge = portfolio_risk_pct([("XAUUSD", 0.5), ("XAGUSD", -0.5)], pc)
    assert same > 0.9 and hedge < 0.3


def test_portfolio_check_blocks_stacked_correlated_risk():
    from decision import Decision

    def approve_with(xag_lots, xag_dir=1):
        xag = pos(9, d=xag_dir, entry=25.0, sl=24.0 if xag_dir > 0 else 26.0, vol=xag_lots)
        xag.symbol = "XAGUSD"
        c = FakeClient(10000.0, [])
        c.all_positions = lambda: [xag]                                      # $100 per lot per $1 => lots x 1% risk
        d = tmpdir()
        r = RiskEngine(CFG, c, State(os.path.join(d, "s.json")), Journal(d))
        r.refresh()
        return r.approve(Decision(action="BUY", direction=1, style="day", entry=2000, sl=1994, tp=2012, risk_pct=0.5), 2000.0, [])
    a = approve_with(1.2)                                                     # 1.2% long silver + 0.48% long gold
    assert not a.ok and "correlated" in a.reason and a.details["portfolio_risk_after"] > 1.5
    assert approve_with(0.5).ok                                               # small correlated position fits
    hedged = approve_with(1.2, xag_dir=-1)                                    # short silver offsets long gold
    assert hedged.ok and hedged.details["portfolio_risk_after"] < hedged.details["portfolio_risk_before"]


def test_throttle_diagnoses_and_waits_for_regime_change():
    streak = [{"regime": "RANGE", "setup": "FVG_RETEST", "slippage_r": 0.3, "spread": 0.6}] * 4 + [{"regime": "WEAK_TREND"}]
    hist = [{"slippage_r": 0.05, "spread": 0.25}] * 20 + streak
    diag = diagnose(streak, hist)
    assert diag["dominant_regime"] == "RANGE" and any("slippage" in f for f in diag["findings"])
    core = RiskCore(CFG)
    now = utc(12)
    assert core.throttle(now, utc(11), diag, "RANGE") > now                   # same regime: keep pausing
    assert core.throttle(now, utc(11), diag, "STRONG_TREND") is None          # regime changed: resume
    assert core.throttle(now, utc(13), diag, "RANGE") == utc(13)              # pause not over yet


# ---------------- analytics / optimizer ----------------
def test_mfe_mae_and_execution_findings():
    n = 60
    tr = pd.DataFrame({"r": [2.0] * 30 + [-1.0] * 30, "mfe_r": [2.1] * 30 + [1.2] * 30, "mae_r": [-0.2] * 30 + [-1.0] * 30,
                       "be_regret": [None] * 30 + [True] * 20 + [False] * 10, "slippage_r": [0.02] * 40 + [0.2] * 20,
                       "spread": [0.25] * n, "session": ["LONDON_OPEN"] * 40 + ["NY_OPEN"] * 20})
    mm = mfe_mae_report(tr)
    text = " ".join(mm["findings"])
    assert "wider than needed" in text and "+1R first" in text and "break-even" in text
    ex = execution_report(tr)
    assert any("NY_OPEN" in f for f in ex["findings"]) and ex["r_before_costs"] > tr["r"].mean()


def test_optimizer_helpers():
    assert len(grid("entry")) == 36 and r_drawdown(np.array([1, -1, -1, 2])) == 2.0
    cfg2 = apply(CFG, {"strategy.min_score": 70.0, "styles.trail_mode": "atr", "styles.partial": False, "styles.be_r_scale": 2.0})
    assert cfg2.strategy.min_score == 70 and all(s.trail_mode == "atr" and s.partial is None for s in cfg2.strategy.styles.values())
    assert cfg2.strategy.styles["day"].be_r == 2.0 and CFG.strategy.styles["day"].be_r == 1.0
    mc = monte_carlo(np.array([1.0, -1.0, 0.5, -0.5, 2.0, -1.0, 1.0, -1.0, 0.8, -0.2] * 5))
    assert mc["total_r_p5"] <= mc["total_r_p50"] <= mc["total_r_p95"] and 0 <= mc["p_negative"] <= 1


def test_walk_forward_runs_on_cached_signals():
    from backtest import BTConfig, Backtester, synthetic_m5
    btr = Backtester(CFG, BTConfig(decision_step=6, bars=300))
    prep = btr.prepare(synthetic_m5(5000, seed=4), 5)
    sig = btr.signals(prep)
    opt = Optimizer(btr, prep, sig, min_trades=1)
    wf, o, b = opt.walk_forward("sizing", k=3)
    assert len(wf["folds"]) == 2 and "verdict" in wf and 0 <= wf["param_stability"] <= 1
    ab = opt.ablation(3)
    assert set(ab["without"]) == {"use_regime_filter", "use_micro_gate", "use_hierarchy_confirmation", "use_contradiction"}
    tr1 = btr.simulate(prep, sig)[0]
    tr2 = btr.simulate(prep, sig)[0]
    pd.testing.assert_frame_equal(tr1, tr2)                                   # re-simulation is deterministic
