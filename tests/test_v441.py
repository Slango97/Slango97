"""V4.4.1: regression tests for every defect found in the V4.4 review."""
from helpers import Config, TUE_10, tmpdir  # noqa: F401

import ast
import os
import re
from dataclasses import replace
from pathlib import Path


import test_mt5_engine as T
from decision import Decision
from news import NewsContext, NewsEngine
from risk import RiskCore, calendar_risk_multiplier, configured_calendar_families

CFG = Config()
ROOT = Path(__file__).resolve().parent.parent


# ---------- 1. the LLM switch ----------
def test_llm_opt_in_actually_builds_the_provider_chain():
    from intel import ProviderChain, make_chain
    assert make_chain(replace(CFG.intel, provider="openai")) is None                       # still opt-in by default
    off = NewsEngine(CFG, http_get=lambda u: b"[]")
    assert off._llm is None
    on_cfg = replace(CFG, features=replace(CFG.features, use_llm=True), intel=replace(CFG.intel, provider="anthropic"))
    on = NewsEngine(on_cfg, http_get=lambda u: b"[]")
    assert isinstance(on._llm, ProviderChain) and [n for n, _ in on._llm.providers] == ["anthropic"]
    os.environ.update(FEATURES__USE_LLM="true", INTEL__PROVIDER="anthropic")
    try:
        c = Config.from_env()
        assert c.features.use_llm and isinstance(NewsEngine(c, http_get=lambda u: b"[]")._llm, ProviderChain)
    finally:
        for k in ("FEATURES__USE_LLM", "INTEL__PROVIDER"):
            os.environ.pop(k, None)


def test_enabling_llm_without_a_provider_is_a_config_error():
    bad = replace(CFG, features=replace(CFG.features, use_llm=True))
    assert any("FEATURES__USE_LLM" in e for e in bad.validate())


def test_env_example_has_one_provider_line_and_llm_off():
    from config import load_env
    text = (ROOT / ".env.example").read_text()
    for key in ("INTEL__PROVIDER", "FEATURES__USE_LLM"):
        assert sum(1 for l in text.splitlines() if l.startswith(key + "=")) == 1, key
    before = dict(os.environ)
    try:
        load_env(str(ROOT / ".env.example"))
        c = Config.from_env()
        assert c.validate() == [] and c.intel.provider == "none" and not c.features.use_llm
    finally:
        os.environ.clear()
        os.environ.update(before)


# ---------- 2. single-vendor calendar policy ----------
def test_calendar_risk_policy_modes():
    mk = lambda **kw: replace(CFG, news=replace(CFG.news, **kw))
    d = mk(calendar_single_provider_mode="degraded_only")
    assert calendar_risk_multiplier(d, 1, 1) == (1.0, "")                 # ForexFactory-only setup: no permanent cut
    assert calendar_risk_multiplier(d, 2, 2) == (1.0, "")                 # redundancy intact
    m, why = calendar_risk_multiplier(d, 2, 1)
    assert m == 0.7 and "lost" in why                                     # redundancy lost: cut
    assert calendar_risk_multiplier(mk(calendar_single_provider_mode="always"), 1, 1)[0] == 0.7
    assert calendar_risk_multiplier(mk(calendar_single_provider_mode="off"), 2, 1) == (1.0, "")
    assert calendar_risk_multiplier(mk(calendar_allow_single_provider=False), 2, 1)[0] is None
    assert any("risk_mult" in e for e in mk(calendar_single_provider_risk_mult=1.3).validate())   # may never raise risk


def test_default_setup_has_two_key_free_families_and_no_permanent_cut():
    """V5.1: ForexFactory + the official BLS schedule = two independent families without any API key."""
    os.environ.pop("TRADING_ECONOMICS_API_KEY", None)
    assert configured_calendar_families(CFG) == 2
    assert calendar_risk_multiplier(CFG, 2, 2)[0] == 1.0 and calendar_risk_multiplier(CFG, 2, 1)[0] == 0.7
    ff_only = replace(CFG, news=replace(CFG.news, calendar_providers=tuple(p for p in CFG.news.calendar_providers if p[1] != "bls_html")))
    assert configured_calendar_families(ff_only) == 1 and calendar_risk_multiplier(ff_only, 1, 1)[0] == 1.0


def _entry_decision(p=2000.0):
    return Decision(action="BUY", direction=1, style="day", setup="FVG_RETEST", tf="H1", entry=p, sl=p - 6, tp=p + 12, rr=2.0,
                    score=80, technical=80, risk_pct=0.5, session="LONDON_OPEN", vol_regime="NORMAL", regime="STRONG_TREND",
                    tags=["FVG_RETEST"], invalidation={"zone": [p - 3, p - 1]})


def test_engine_applies_the_policy_and_can_block():
    import engine as E
    orig = E.decide
    try:
        E.decide = lambda *a, **k: _entry_decision()
        eng, cfg, client = T.started_engine("DEMO")
        eng.news_ctx = NewsContext(status="NO_DATA", calendar_ok=True, calendar_configured_families=2, calendar_remote_families=1)
        eng.candle_step()
        assert eng.armed is not None and abs(eng.armed.dec.risk_pct - 0.35) < 1e-9
        assert any("redundancy lost" in r for r in eng.armed.dec.reasons)
        eng.armed = None
        eng.news_ctx = NewsContext(status="NO_DATA", calendar_ok=True, calendar_configured_families=1, calendar_remote_families=1)
        eng.candle_step()
        assert abs(eng.armed.dec.risk_pct - 0.5) < 1e-9                    # single configured vendor: unchanged
        eng.armed = None
        eng.cfg = replace(eng.cfg, news=replace(eng.cfg.news, calendar_allow_single_provider=False))
        dec = eng.candle_step()
        assert eng.armed is None and dec.action == "WAIT" and dec.wait_reason == "NEWS"
    finally:
        E.decide = orig


def test_backtest_sizes_with_the_same_policy_as_live():
    import backtest as B
    import test_v431 as V431
    sim = B.synthetic_m5(3000, seed=1)
    lots = {}
    for mode in ("off", "always"):
        cfg = replace(CFG, news=replace(CFG.news, calendar_single_provider_mode=mode,
                                        calendar_providers=tuple(p for p in CFG.news.calendar_providers if p[1] != "bls_html")),
                      features=replace(CFG.features, m1_refinement=False))
        btr = B.Backtester(cfg, B.BTConfig(decision_step=6, bars=300))
        prep = btr.prepare(sim, 5)
        sig = btr.signals(prep)
        i0 = sig[len(sig) // 2].i
        p = float(prep.C[i0])
        orig = B.apply_policy
        try:
            B.apply_policy = V431._policy_once(V431._entry(p, p - 6, p + 12, [p - 3, p - 1]))
            tr, _, _ = btr.simulate(prep, [s for s in sig if s.i >= i0][:3], cfg, i0, i0 + 6)
        finally:
            B.apply_policy = orig
        lots[mode] = float(tr["lots0"].iloc[0])
    assert 0.6 <= lots["always"] / lots["off"] <= 0.75                      # 0.70x after lot-step rounding


# ---------- 3. dead settings ----------
def test_every_config_field_is_used_somewhere():
    src = (ROOT / "config.py").read_text()
    code = "\n".join(p.read_text() for p in ROOT.rglob("*.py")          # every package (incl. brain/), not tests
                      if p.name != "config.py" and "tests" not in p.relative_to(ROOT).parts)
    unused = []
    for cls in [n for n in ast.parse(src).body if isinstance(n, ast.ClassDef)]:
        for st in cls.body:
            if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                name = st.target.id
                rest = src.replace(f"    {name}:", "", 1)
                if not re.search(r"\b" + re.escape(name) + r"\b", code) and not re.search(r"\b" + re.escape(name) + r"\b", rest):
                    unused.append(f"{cls.name}.{name}")
    assert unused == [], unused


def test_session_spread_caps_are_enforced():
    core = RiskCore(replace(CFG, execution=replace(CFG.execution, session_spread_caps={"ASIA": 20})))
    base = dict(n_open=0, trades_today=0, day_loss_pct=0, week_loss_pct=0, dd_pct=0, open_risk_pct=0)
    ok, why = core.gate(now=TUE_10.replace(hour=3), spread_points=30, **base)
    assert not ok and "cap 20" in why
    assert core.gate(now=TUE_10, spread_points=30, **base)[0]               # London: global cap 40 applies


# ---------- 5. documented supersession ----------
def test_superseded_register_names_real_frozen_tests():
    reg = ROOT / "tests" / "v3_baseline" / "SUPERSEDED.txt"
    names = [l.split("|")[0].strip() for l in reg.read_text().splitlines() if l.strip() and not l.startswith("#")]
    frozen = "\n".join(p.read_text() for p in (ROOT / "tests" / "v3_baseline").glob("test_*.py"))
    assert names and all(f"def {n}(" in frozen for n in names)


# ---------- 6. heartbeat robustness ----------
def test_heartbeat_tolerates_news_objects_without_headline_timestamps():
    eng, cfg, client = T.started_engine("DRY_RUN")
    assert not hasattr(eng.news, "heads_at")
    eng.heartbeat()
    ev = [r for r in eng.journal.read_jsonl(eng.journal.events_path) if r["kind"] == "heartbeat"]
    assert ev and ev[-1].get("news_age_h") is None


# ---------- found while building V4.4.1: variable shadowing crash (present since V4.2) ----------
def test_backtest_survives_a_rejected_candidate_followed_by_a_trade():
    """The rejected-candidate collector reused the name 'ex' and overwrote the execution config, so the NEXT fill
    crashed with AttributeError. On real data evidence.py would have died at the first trade after any rejection."""
    import backtest as B
    sim = B.synthetic_m5(3000, seed=2)
    cfg = replace(CFG, features=replace(CFG.features, m1_refinement=False))
    btr = B.Backtester(cfg, B.BTConfig(decision_step=6, bars=300))
    prep = btr.prepare(sim, 5)
    sig = btr.signals(prep)
    i0 = sig[len(sig) // 2].i
    p = float(prep.C[i0])
    blocked = _entry_decision(p)
    blocked.action, blocked.blockers = "WAIT", [{"code": "QUALITY", "message": "test"}]
    seq = [Decision(action="WAIT", wait_reason="QUALITY", candidates=[blocked]), _entry_decision(p)]
    orig = B.apply_policy
    try:
        B.apply_policy = lambda *a, **k: seq.pop(0) if seq else Decision(action="WAIT", wait_reason="NO_SETUP")
        tr, _, summ = btr.simulate(prep, [s for s in sig if s.i >= i0][:4], cfg, i0, i0 + 30)
    finally:
        B.apply_policy = orig
    assert len(btr.candidate_rows) >= 1 and len(tr) == 1 and summ["total_trades"] == 1
