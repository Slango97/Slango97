from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import json

import pandas as pd

from config import Config
from brain import V5BrainSystem
from brain.chart import render_chart
from brain.technical import TechnicalBrain


class FakeClient:
    symbol = "XAUUSDs"
    symbol_auto_detected = True
    symbol_resolution = {"method": "test", "selected": "XAUUSDs"}
    diag = SimpleNamespace(status="OK", cause="CONSISTENT", errors=[], warnings=[])

    def account(self):
        return SimpleNamespace(login=1, server="Demo", company="Test", currency="USD", leverage=100,
                               balance=3000.0, equity=3000.0, margin_free=3000.0)

    def tick_age_s(self):
        return 0.2

    def is_connected(self):
        return True


def _views():
    out = {}
    for tf, trend in (("W1", 1), ("D1", 1), ("H4", 1), ("H1", 1), ("M15", 1), ("M5", 1)):
        out[tf] = {
            "tf": tf, "close": 4000.0, "atr": 5.0, "ema_trend": trend, "struct_trend": trend,
            "rsi": 56.0, "macd_hist": 1.0, "adx": 28.0, "adx_band": "STRONG", "vol_regime": "NORMAL",
            "struct_regime": "BULLISH", "sweep": None, "fvg": [], "ob": [], "labels": [], "data_issues": [],
        }
    return out


def _decision(action="WAIT"):
    return SimpleNamespace(action=action, direction=1 if action == "BUY" else 0, score=78.0, technical=76.0,
                           rr=2.2, risk_pct=0.5, p_win=None, exp_r=None, setup="FVG_RETEST", style="day",
                           entry=4000.0, sl=3990.0, tp=4022.0, blockers=[], reasons=["test setup"], is_entry=action in ("BUY", "SELL"))


def test_v5_workflow_runs_without_llm_or_telegram(tmp_path):
    cfg = replace(Config(), brain=replace(Config().brain, journal_file=str(tmp_path / "v5.jsonl"), health_report_interval_min=60))
    system = V5BrainSystem(cfg, client=FakeClient(), risk_engine=None, alerts=None)
    ms = SimpleNamespace(views=_views(), micro=SimpleNamespace(score=90), regime=SimpleNamespace(label="STRONG_TREND"))
    news = SimpleNamespace(status="OK", bias=0.4, confidence=0.8, impact="low", event_phase="CLEAR", blackout=False,
                           trade_veto=False, calendar_ok=True, calendar_state="FRESH", calendar_cache_age_h=0.1,
                           drivers=[], contradictions=[])
    run = system.run(lifecycle="RUNNING", ms=ms, decision=_decision("WAIT"), news_ctx=news, positions=[], frames={}, aux=None)
    assert run.symbol == "XAUUSDs"
    assert run.decision.permission == "NO_TRADE"
    assert Path(cfg.brain.journal_file).exists()
    json.loads(Path(cfg.brain.journal_file).read_text().splitlines()[-1])


def test_v5_cannot_approve_when_stale_calendar():
    cfg = replace(Config(), brain=replace(Config().brain, journal_file="data/test_v5_tmp.jsonl"))
    system = V5BrainSystem(cfg, client=FakeClient(), risk_engine=None, alerts=None)
    ms = SimpleNamespace(views=_views(), micro=SimpleNamespace(score=95), regime=SimpleNamespace(label="STRONG_TREND"))
    news = SimpleNamespace(status="OK", bias=0.4, confidence=0.8, impact="low", event_phase="CLEAR", blackout=False,
                           trade_veto=False, calendar_ok=False, calendar_state="UNAVAILABLE", calendar_cache_age_h=None,
                           drivers=[], contradictions=[])
    run = system.run(lifecycle="RUNNING", ms=ms, decision=_decision("BUY"), news_ctx=news, positions=[], frames={}, aux=None)
    assert run.decision.permission in ("BLOCK", "NO_TRADE")
    Path("data/test_v5_tmp.jsonl").unlink(missing_ok=True)


def test_technical_brain_detects_basic_candles_and_patterns(tmp_path):
    idx = pd.date_range("2026-01-01", periods=40, freq="5min")
    close = pd.Series([100 + i * 0.05 for i in range(40)], index=idx)
    df = pd.DataFrame({"open": close, "high": close + 0.2, "low": close - 0.2, "close": close, "tick_volume": 1.0}, index=idx)
    df.iloc[-2, df.columns.get_loc("open")] = 101.9
    df.iloc[-2, df.columns.get_loc("close")] = 101.4
    df.iloc[-1, df.columns.get_loc("open")] = 101.3
    df.iloc[-1, df.columns.get_loc("close")] = 102.0
    df.iloc[-1, df.columns.get_loc("low")] = 100.9
    v = TechnicalBrain().run(_views(), frames={"M15": df}, symbol="XAUUSDs")
    assert "BULLISH_ENGULFING" in v.facts["candlestick_patterns"]
    assert 0 <= v.score <= 100


def test_chart_renderer_creates_png(tmp_path):
    idx = pd.date_range("2026-01-01", periods=30, freq="5min")
    c = pd.Series(range(100, 130), index=idx, dtype=float)
    df = pd.DataFrame({"open": c-0.2, "high": c+0.4, "low": c-0.4, "close": c, "tick_volume": 1.0}, index=idx)
    p = render_chart(df, path=tmp_path / "chart.png", entry=115, sl=110, tp=125, patterns=["HAMMER"])
    assert p is not None and Path(p).exists()

def test_connection_brain_does_not_report_stale_degraded_state_as_healthy():
    from brain.connection import ConnectionBrain
    cfg = Config()
    class SlowClient(FakeClient):
        def tick_age_s(self):
            return 120.0
    finding = ConnectionBrain().run(SlowClient(), cfg, lifecycle="DEGRADED")
    assert finding.score < 70
    assert "stale/invalid market data" in finding.reasons
