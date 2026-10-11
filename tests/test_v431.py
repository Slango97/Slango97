"""V4.3.1: regression tests for every defect found in the V4.3 review and for the ported News Brain ideas."""
from helpers import Config, NS, TUE_10, long_setup_views, tmpdir  # noqa: F401

import os
import time
from dataclasses import replace
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import test_mt5_engine as T
import test_v42 as V42
from analysis import MarketState
from news import NewsContext
from decision import Decision


def _entry(p, sl, tp, zone):
    return Decision(action="BUY", direction=1, style="day", setup="FVG_RETEST", tf="H1", entry=p, sl=sl, tp=tp, rr=2.0,
                    score=80, technical=80, risk_pct=0.5, session="LONDON_OPEN", vol_regime="NORMAL", regime="STRONG_TREND",
                    tags=["FVG_RETEST"], invalidation={"zone": zone})


def _policy_once(dec):
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return dec if calls["n"] == 1 else Decision(action="WAIT", wait_reason="NO_SETUP")
    return fake

CFG = Config()
world = T.world


# ---------- clock drift vs quiet markets ----------
def test_quiet_market_is_not_clock_drift_but_a_skewed_clock_is():
    c = T.make_client(T.make_cfg())
    world.frozen_tick_time = int(time.time()) - 25          # last tick 25 s ago, never changes: a quiet market
    for _ in range(3):
        c.observe_tick()
    assert c.server_time_drift_s() == 0.0                   # unknown, not "25 s drift"
    world.frozen_tick_time, world.tick_time_offset = None, -20
    for _ in range(3):
        c.observe_tick()
        time.sleep(1.05)
    assert c.server_time_drift_s() > 15                     # ticks keep arriving but stamped 20 s behind: real skew


# ---------- one explicit price basis ----------
def test_counterfactual_uses_the_correct_side_for_bid_bars():
    from backtest import counterfactual
    H, L, C, SP = np.array([2000.0, 2006.2]), np.array([2000.0, 1999.0]), np.array([2000.0, 2006.0]), np.array([0.4, 0.4])
    mid = 2000.0 + 0.2                                       # decision mid when bars are BID-based
    # entry = ask = 2000.4, risk 3.4. Bid bars: bid high 2006.2 reaches the 2006.1 target -> (2006.1 - 2000.4) / 3.4
    assert abs(counterfactual(H, L, C, SP, 0, 1, mid, 1997.0, 2006.1, 1, bid_shift=0.0) - 5.7 / 3.4) < 1e-9
    # Mid bars: bid high is only 2006.0 -> target missed, marked to market at bid close 2005.8 -> 5.4 / 3.4
    assert abs(counterfactual(H, L, C, SP, 0, 1, mid, 1997.0, 2006.1, 1, bid_shift=0.5) - 5.4 / 3.4) < 1e-9
    s = counterfactual(np.array([2000.0, 2002.9]), np.array([2000.0, 1999.0]), np.array([2000.0, 2001.0]), np.array([0.4, 0.4]),
                       0, -1, mid, 2003.0, 1994.0, 1, bid_shift=0.0)
    assert s == -1.0                                         # short stop: ASK high = 2002.9 + 0.4 >= 2003


def test_backtest_defaults_to_bid_bars_and_mid_is_selectable():
    from backtest import BTConfig, Backtester, synthetic_m5
    assert BTConfig().price_basis == "bid"
    sim = synthetic_m5(3000, seed=9)
    for basis in ("bid", "mid"):
        btr = Backtester(CFG, BTConfig(decision_step=12, bars=300, price_basis=basis))
        prep = btr.prepare(sim, 5)
        btr.simulate(prep, btr.signals(prep))               # both run end to end


def test_limit_fills_need_the_executable_side_to_trade_through():
    """A BUY limit at 2000 must not fill when only the BID low touches it (V4.3 filled it a spread too early)."""
    import backtest as B
    sim = B.synthetic_m5(3000, seed=1)
    cfg = replace(CFG, features=replace(CFG.features, limit_entries=True, m1_refinement=False),
                  execution=replace(CFG.execution, limit_fill_through_points=0.0, limit_min_improve_atr=0.0))
    btr = B.Backtester(cfg, B.BTConfig(decision_step=6, bars=300))
    prep = btr.prepare(sim, 5)
    prep.SP[:] = 0.5
    sig = btr.signals(prep)
    i0 = sig[len(sig) // 2].i
    lp = round(float(prep.C[i0]) - 3.0, 2)
    dec = _entry(float(prep.C[i0]), lp - 6, lp + 12, [lp - 0.5, lp + 0.5])
    prep.L[i0 + 1:i0 + 3] = lp - 0.2                         # bid low 0.2 below the limit; ask low = lp + 0.3 > lp
    prep.H[i0 + 1:i0 + 3] = np.maximum(prep.H[i0 + 1:i0 + 3], lp + 1)
    orig, orig_lp = B.apply_policy, __import__("levels").limit_price
    try:
        B.apply_policy = _policy_once(dec)
        __import__("levels").limit_price = lambda *a, **k: lp
        sub = [s for s in sig if s.i >= i0][:4]
        _, _, summ = btr.simulate(prep, sub, cfg, i0, i0 + 4)
        assert summ["limit_entries"].get("placed") == 1 and not summ["limit_entries"].get("filled")
    finally:
        B.apply_policy, __import__("levels").limit_price = orig, orig_lp


# ---------- fill policy ----------
def _filled(stack_mutator=None, volume_mult=1.0):
    cfg, client, st, ex = V42._limit_stack()
    ms, dec = T.setup_decision(client, cfg)
    out = ex.execute(dec, ms, [])
    o = next(x for x in world.pending_orders if x.ticket == out.ticket)
    o.volume_initial = round(o.volume_initial * volume_mult, 2)
    T.fake_mt5.fill_pending(out.ticket)
    if stack_mutator:
        stack_mutator(st)
    ex._current_news = NewsContext(status="OK", calendar_ok=True)
    return ex.manage_pending()


def test_fill_kept_on_soft_drift_rejected_on_hard_limits_and_oversize():
    assert any(e[0] == "filled" for e in _filled(lambda st: world.__setattr__("equity", world.equity * 0.995)))
    assert any(e[0] == "filled_rejected" for e in _filled(volume_mult=3.0))
    def daily_loss(st):
        st.risk["day_start_equity"] = world.equity * 1.05     # 5% down on the day: hard limit
    assert any(e[0] == "filled_rejected" for e in _filled(daily_loss))


def test_limits_never_rest_into_rollover_or_late_friday():
    cfg, client, st, ex = V42._limit_stack()
    client.utc_now = lambda: TUE_10.replace(hour=20, minute=50)
    exp = ex._limit_expiry()
    assert exp is not None and exp - time.time() <= 10 * 60 + 1          # cut at 21:00, not 30 min later
    client.utc_now = lambda: TUE_10.replace(hour=20, minute=59)
    assert ex._limit_expiry() is None
    client.utc_now = lambda: datetime(2025, 3, 7, 18, 45, tzinfo=timezone.utc)   # Friday, cutoff 19:00
    assert ex._limit_expiry() - time.time() <= 15 * 60 + 1


def test_dry_run_simulates_limit_orders_without_sending_anything():
    cfg, client, j, st, al, risk, ex = T.stack("DRY_RUN")
    lim = replace(cfg, features=replace(cfg.features, limit_entries=True), execution=replace(cfg.execution, limit_min_improve_atr=0.05))
    for o in (ex, risk, ex.mgr):
        o.cfg = lim
    ms, dec = T.setup_decision(client, lim)
    out = ex.execute(dec, ms, [])
    assert out.dry_run and "limit" in out.reason and st.data["pending_sim"] and not world.calls
    lp = next(iter(st.data["pending_sim"].values()))["price"]
    world.tick_for_test(lp - 0.5)
    ev = ex.manage_pending()
    assert len(ev) == 1 and ev[0][0] == "sim_filled"
    assert not st.data["pending_sim"] and not world.calls
    ev = [r["kind"] for r in j.read_jsonl(j.events_path)]
    assert "dry_run_limit_placed" in ev and "dry_run_limit_filled" in ev


# ---------- M1 + limit parity ----------
def test_backtest_runs_m1_refinement_before_placing_a_limit():
    import backtest as B
    from refine import RefineResult
    m5 = B.synthetic_m5(20000, seed=5)
    m1 = m5.iloc[:20000].copy()
    m1.index = pd.date_range("2024-01-01", periods=len(m1), freq="1min")
    m1 = m1[m1.index.dayofweek < 5]
    cfg = replace(CFG, features=replace(CFG.features, limit_entries=True, m1_refinement=True),
                  execution=replace(CFG.execution, limit_min_improve_atr=0.0))
    btr = B.Backtester(cfg, B.BTConfig(decision_step=24, bars=300))
    prep = btr.prepare(m1, 1)
    sig = btr.signals(prep)
    i0 = sig[len(sig) // 2].i
    p = float(prep.C[i0])
    dec = _entry(p, p - 6, p + 12, [p - 3, p - 1])
    orig_pol, orig_chk = B.apply_policy, B.m1_check
    try:
        B.apply_policy = _policy_once(dec)
        B.m1_check = lambda *a, **k: RefineResult("GO", "test")
        sub = [s for s in sig if s.i >= i0][:3]
        _, _, summ = btr.simulate(prep, sub, cfg, i0, i0 + 60)
        assert summ["m1_refinement"].get("armed") == 1 and summ["m1_refinement"].get("go") == 1
        assert summ["limit_entries"].get("placed") == 1          # the limit came AFTER the M1 confirmation, as live
    finally:
        B.apply_policy, B.m1_check = orig_pol, orig_chk


# ---------- deterministic headline reasoning ----------
def test_headline_rules_handle_negation_reversal_echo_and_mixed_news():
    from news_rules import HEADLINE_CONF_CAP, assess_headlines, headline_votes
    assert headline_votes("Fed not expected to hike rates this year") == [("fed_policy", 1)]
    assert headline_votes("Fed rules out rate cuts") == [("fed_policy", -1)]
    assert headline_votes("US inflation slowed less than expected") == [("inflation", -1)]
    assert headline_votes("US inflation rose less than expected") == [("inflation", 1)]
    assert headline_votes("Gold surged to a record high") == []
    assert headline_votes("Jobless claims rose; dollar slides") == [("jobless", 1), ("usd", 1)]
    mixed = assess_headlines([NS(title="Fed signals higher for longer", weight=1.0), NS(title="Missile strikes escalate war", weight=1.0)])
    assert mixed.conflicts and abs(mixed.bias) < 0.5
    same = assess_headlines([NS(title=t, weight=1.0) for t in ("Treasury yields rise", "Treasury yields climb again", "Bond yields jump")])
    assert same.families == {"yields": -1} and same.confidence <= HEADLINE_CONF_CAP     # one vote per family
    many = assess_headlines([NS(title=t, weight=1.0) for t in ("Fed signals rate cuts", "Dollar slides", "Treasury yields fall",
                                                             "Inflation cooled more than expected", "Jobless claims rose")])
    assert many.bias == 1.0 and many.confidence <= HEADLINE_CONF_CAP


def test_headline_rules_can_only_add_caution():
    from confluence import assess
    from decision import decide
    v = long_setup_views()
    roles = CFG.hierarchy.roles["day"]
    base = assess(1, v, roles, None, NewsContext(), CFG).index
    against = assess(1, v, roles, None, NewsContext(rules_bias=-1.0, rules_confidence=0.45), CFG).index
    support = assess(1, v, roles, None, NewsContext(rules_bias=1.0, rules_confidence=0.45), CFG).index
    assert against > base and support == base                 # opposing headlines add caution; supportive ones add nothing
    ms = MarketState(v, "LONDON_OPEN", None, 0.25)
    d = decide(ms, 2000.0, NewsContext(rules_bias=-1.0, rules_confidence=0.45), CFG)
    assert d.wait_reason != "INTEL"                           # headline rules alone can never veto


def test_news_engine_exposes_headline_rules():
    import json as _j
    from datetime import timedelta
    from email.utils import format_datetime
    from news import NewsEngine
    now = datetime.now(timezone.utc)
    rss = ("<rss><channel>" + "".join(f"<item><title>{t}</title><pubDate>{format_datetime(now - timedelta(minutes=9))}</pubDate></item>"
                                      for t in ("Fed not expected to hike, dollar slides", "Treasury yields fall")) + "</channel></rss>").encode()
    nc = replace(CFG.news, calendar_cache_file=os.path.join(tmpdir(), "c.json"))
    n = NewsEngine(replace(CFG, news=nc), http_get=lambda u: _j.dumps([]).encode() if "faireconomy" in u else rss, llm=None)
    n.refresh()
    c = n.get()
    assert c.rules_bias > 0 and c.rules_confidence > 0 and {"fed_policy", "usd", "yields"} <= set(c.rules_families)


# ---------- analytics / hygiene ----------
def test_gate_funnel_counts_sole_blockers():
    from analytics import gate_funnel
    df = pd.DataFrame({"blockers": ["QUALITY", "QUALITY", "QUALITY,REGIME", "REGIME"], "cf_r": [1.0, -1.0, 2.0, -1.0]})
    rows = {r["gate"]: r for r in gate_funnel(df)}
    assert rows["QUALITY"]["fires"] == 3 and rows["QUALITY"]["sole_blocker"] == 2 and rows["QUALITY"]["sole_avg_cf_r"] == 0.0
    assert rows["REGIME"]["sole_blocker"] == 1


def test_release_refuses_runtime_data():
    import release_manifest as RM
    from pathlib import Path
    root = Path(tmpdir())
    (root / "data").mkdir()
    (root / "data" / ".gitkeep").write_text("")
    (root / "a.py").write_text("x=1\n")
    saved = RM.ROOT
    RM.ROOT = root
    try:
        RM.generate()
        (root / "data" / "calendar_cache.json").write_text("{}")
        try:
            RM.generate()
            raise AssertionError("release with stray data was built")
        except SystemExit as ex:
            assert "calendar_cache.json" in str(ex)
        assert any("calendar_cache.json" in x for x in RM.verify()[2])
    finally:
        RM.ROOT = saved
