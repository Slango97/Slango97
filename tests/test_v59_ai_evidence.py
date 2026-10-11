"""V5.9: AI influence must be earned with out-of-sample evidence."""
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from helpers import long_setup_views  # noqa: F401  (sets sys.path)
from config import Config
from intel_evaluation import binom_tail_ge, collect, counterfactual_r, evaluate, markdown, score_opinion

CFG = Config()


def _cfg(tmp_path, **intel):
    return replace(CFG, system=replace(CFG.system, data_dir=str(tmp_path)),
                   brain=replace(CFG.brain, journal_file=str(tmp_path / "v5.jsonl")),
                   intel=replace(CFG.intel, log_file=str(tmp_path / "intel_log.jsonl"),
                                 outcomes_file=str(tmp_path / "outcomes.jsonl"),
                                 evidence_file=str(tmp_path / "evidence.json"), **intel))


def _bars(n=600, start="2026-09-01", drift=0.0002, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n, freq="5min")
    c = 1.08 * np.exp(np.cumsum(drift + rng.normal(0, 0.0002, n)))
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) + 0.00005, "low": np.minimum(o, c) - 0.00005, "close": c},
                        index=idx)


def test_exact_binomial_tail():
    assert abs(binom_tail_ge(10, 10) - 1 / 1024) < 1e-12
    assert binom_tail_ge(0, 10) == 1.0 and binom_tail_ge(5, 0) == 1.0
    assert binom_tail_ge(60, 100) < 0.03 < binom_tail_ge(55, 100)


def test_opinions_are_scored_without_look_ahead_and_only_when_finished():
    df = _bars(drift=0.0003)
    op = {"id": "a", "t": "2026-09-01T10:02:30+00:00", "instrument": "EURUSDs", "bias": 1, "confidence": 0.8,
          "expiry_minutes": 240, "trade_veto": False}
    row = score_opinion(op, df, (60, 240))
    assert row["instrument"] == "EURUSD" and row["moves_atr"]["60"]["close"] > 0 and row["moves_atr"]["240"]["range"] > 0
    late = {**op, "t": str(df.index[-5]) + "+00:00"}
    assert score_opinion(late, df, (60, 240)) is None                        # horizon not over yet
    short = score_opinion({**op, "expiry_minutes": 30}, df, (60, 240))      # horizon capped by the view's expiry
    assert short["moves_atr"]["240"] == short["moves_atr"]["60"]


def test_counterfactual_first_touch_rules():
    idx = pd.date_range("2026-09-01", periods=6, freq="5min")
    df = pd.DataFrame({"open": [1.0] * 6, "high": [1.0, 1.003, 1.0, 1.0, 1.0, 1.0],
                       "low": [1.0, 0.999, 0.996, 1.0, 1.0, 1.0], "close": [1.0] * 6}, index=idx)
    t = "2026-09-01T00:00:00+00:00"
    assert abs(counterfactual_r(t, 1, 1.0, 0.997, 1.002, df) - 2.0 / 3) < 1e-3   # TP first
    assert counterfactual_r(t, 1, 1.0, 0.9992, 1.002, df) == -1.0            # SL and TP in one bar: SL wins
    assert counterfactual_r(t, -1, 1.0, 1.005, 0.995, df) is None            # neither hit, data ends before 48h
    assert counterfactual_r(t, 1, 1.0, 1.0, 1.01, df) is None                # zero risk


def _ops(n, hit_share, h=60):
    rows = []
    for i in range(n):
        hit = i < round(n * hit_share)
        rows.append({"kind": "opinion", "bias": 1, "confidence": 0.8, "trade_veto": False,
                     "moves_atr": {str(h): {"close": 1.0 if hit else -1.0, "range": 2.0}, "240": {"close": 0.5, "range": 3}}})
    return rows


def test_verdicts_require_significance_not_just_a_good_streak():
    assert evaluate(CFG, _ops(80, 0.70))["news_intel"]["verdict"] == "PROVEN"
    assert evaluate(CFG, _ops(80, 0.55))["news_intel"]["verdict"] == "UNPROVEN"
    assert evaluate(CFG, _ops(80, 0.25))["news_intel"]["verdict"] == "HARMFUL"
    few = evaluate(CFG, _ops(20, 0.9))["news_intel"]
    assert few["verdict"] == "UNPROVEN" and "need 50" in few["summary"]
    costly = _ops(80, 0.70) + [{"kind": "news_veto", "r": 1.5 + 0.1 * (i % 3)} for i in range(25)]
    ev = evaluate(CFG, costly)["news_intel"]
    assert ev["verdict"] == "HARMFUL" and "cost money" in ev["summary"]      # good calls, but vetoes block winners


def test_v5_veto_evidence_drives_the_enforce_recommendation():
    good = [{"kind": "v5_veto", "r": -1.0 + 0.05 * (i % 4)} for i in range(30)]
    bad = [{"kind": "v5_veto", "r": 1.2 + 0.1 * (i % 3)} for i in range(30)]
    assert evaluate(CFG, good)["v5_brain_veto"]["verdict"] == "VALUABLE"
    assert "supports BRAIN__ENFORCE=true" in evaluate(CFG, good)["v5_brain_veto"]["enforce_recommendation"]
    assert evaluate(CFG, bad)["v5_brain_veto"]["verdict"] == "COSTLY"
    assert evaluate(CFG, good[:5])["v5_brain_veto"]["verdict"] == "UNPROVEN"
    assert "News intelligence" in markdown(evaluate(CFG, good))


def test_collect_scores_logs_records_and_v5_runs_once(tmp_path):
    cfg = _cfg(tmp_path)
    df = _bars(1200, drift=0.0003)
    (tmp_path / "intel_log.jsonl").write_text(json.dumps(
        {"id": "op1", "t": "2026-09-01T12:00:00+00:00", "instrument": "EURUSDs", "bias": 1, "confidence": 0.8,
         "expiry_minutes": 240, "trade_veto": False}) + "\n")
    e = float(df["close"].iloc[150])
    rec = {"id": "r1", "time": "2026-09-01T12:30:00+00:00", "symbol": "EURUSDs", "news": {"intel_id": "op1"},
           "candidates": [{"direction": "SELL", "entry": e, "sl": e + 0.0010, "tp": e - 0.0020, "setup": "X",
                           "blockers": [{"code": "INTEL", "message": "opposed"}]},
                          {"direction": "BUY", "entry": e, "sl": e - 0.001, "tp": e + 0.002, "blockers": []}]}
    (tmp_path / "decision_records.jsonl").write_text(json.dumps(rec) + "\n")
    run = {"timestamp": "2026-09-01T13:00:00+00:00", "symbol": "EURUSDs", "report": {"deterministic_action": "BUY"},
           "decision": {"permission": "BLOCK", "facts": {"entry": e, "sl": e - 0.001, "tp": e + 0.002}, "blockers": ["x"]}}
    (tmp_path / "v5.jsonl").write_text(json.dumps(run) + "\n")
    new = collect(cfg, lambda sym: df)
    kinds = sorted(r["kind"] for r in new)
    assert kinds == ["news_veto", "opinion", "v5_veto"]
    assert next(r for r in new if r["kind"] == "news_veto")["r"] == -1.0   # the vetoed SELL would have lost
    assert collect(cfg, lambda sym: df) == []                               # idempotent: scored once
    assert evaluate(cfg)["news_intel"]["opinions_scored"] == 1


def test_unproven_llm_may_reduce_but_not_add(tmp_path):
    from intel_evaluation import publish
    from intel_evidence import llm_influence, news_verdict
    cfg = _cfg(tmp_path)
    assert news_verdict(cfg)[0] == "MISSING" and llm_influence(cfg) == "reduce_only"
    publish(cfg, evaluate(cfg, _ops(80, 0.70)))
    assert llm_influence(cfg) == "full"
    old = json.loads((tmp_path / "evidence.json").read_text())
    old["generated_at"] = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    (tmp_path / "evidence.json").write_text(json.dumps(old))
    assert news_verdict(cfg)[0] == "STALE" and llm_influence(cfg) == "reduce_only"
    assert llm_influence(replace(cfg, intel=replace(cfg.intel, evidence_gate=False))) == "full"


def test_decision_bonus_uses_only_permitted_evidence_while_opposition_still_blocks():
    from analysis import MarketState
    from decision import decide
    from news import NewsContext
    ms = MarketState(long_setup_views(), "LONDON_OPEN", None, 0.25)
    full = NewsContext(status="OK", bias=1.0, confidence=0.9, support_bias=1.0, support_confidence=0.9)
    gated = NewsContext(status="OK", bias=1.0, confidence=0.9, support_bias=0.0, support_confidence=0.0,
                        llm_influence="reduce_only")
    d_full, d_gated = decide(ms, 2000.0, full, CFG), decide(ms, 2000.0, gated, CFG)
    bonus = lambda d: max((c.components.get("news", 0) for c in d.candidates), default=0)
    assert bonus(d_full) > 0 and bonus(d_gated) == 0
    opposed = NewsContext(status="OK", bias=-1.0, confidence=0.9, support_bias=0.0, support_confidence=0.0,
                          llm_influence="reduce_only")
    d_opp = decide(ms, 2000.0, opposed, CFG)
    assert not d_opp.is_entry and any(b["code"] == "INTEL" for c in d_opp.candidates for b in c.blockers)


def test_news_engine_logs_opinions_and_attributes_them(tmp_path):
    from news import NewsEngine
    cfg = _cfg(tmp_path)
    ne = NewsEngine(cfg, http_get=lambda *a, **k: b"", llm=lambda s, u: "")
    data = {"id": "abc", "fetched_at": 1_790_000_000.0, "gold_bias": "bullish", "instrument_bias": "bearish",
            "confidence": 0.7, "risk_level": "low", "trade_veto": False, "expiry_minutes": 60, "summary": "s"}
    ne._log_intel(data, None)
    row = json.loads((tmp_path / "intel_log.jsonl").read_text())
    assert row["id"] == "abc" and row["bias"] == -1.0 and row["expiry_minutes"] == 60


def test_ai_evidence_report_only_on_verdict_change(tmp_path):
    from test_v55 import _reporter
    rep = _reporter(tmp_path, lambda: "EURUSD")
    ev = evaluate(CFG, _ops(20, 0.5))
    rep.ai_evidence_report(ev)
    rep.ai_evidence_report(ev)
    rep.ai_evidence_report(evaluate(CFG, _ops(80, 0.7)))
    assert len(rep.tg.texts) == 2 and "may only REDUCE risk" in rep.tg.texts[0] and "may ADD score" in rep.tg.texts[1]
