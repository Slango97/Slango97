"""V5.5: multi-instrument price precision, lifecycle latch, active-market identity, instrument-aware
intelligence and the Telegram notification engine (Enhancement Specification)."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pandas as pd

import helpers as H  # noqa: F401  (sets sys.path)
from config import Config
from decision import tp1_level
from levels import limit_price
from management import PosView, plan
from pricefmt import fmt_price, price_decimals, round_price

CFG = Config()


# ---------------- price precision (the EURUSD TP1 bug) ----------------
def test_price_precision_follows_the_instrument_scale():
    assert price_decimals(3.0) == 2            # XAUUSD stop: unchanged two decimals
    assert price_decimals(0.0015) == 5         # EURUSD stop: five decimals
    assert price_decimals(0.15) == 3           # USDJPY stop
    assert price_decimals(300.0) == 2          # BTCUSD stop
    assert round_price(1.084567, 0.0015) == 1.08457
    assert fmt_price(1.084567, 0.0015) == "1.08457" and fmt_price(None) == "n/a"


def test_eurusd_tp1_is_not_rounded_behind_the_entry():
    for entry in (1.08431, 1.08456, 1.08499):
        c = NS(entry=entry, sl=entry - 0.0015, tp=entry + 0.0045, direction=1, rr=3.0, barriers=[])
        tp1 = tp1_level(c, CFG)
        assert tp1 > entry and abs(tp1 - (entry + 1.5 * 0.0015)) < 1e-5
    gold = NS(entry=2400.0, sl=2397.0, tp=2409.0, direction=1, rr=3.0, barriers=[])
    assert tp1_level(gold, CFG) == 2404.5


def test_eurusd_position_does_not_take_tp1_at_entry():
    entry, risk = 1.08431, 0.0015
    st = {"direction": 1, "entry": entry, "risk": risk, "tp1": tp1_level(
        NS(entry=entry, sl=entry - risk, tp=entry + 3 * risk, direction=1, rr=3.0, barriers=[]), CFG), "tp1_frac": 0.5}
    S = CFG.strategy.styles["day"]
    view = {"atr": 0.001, "close": entry, "swing_lows": [], "swing_highs": [], "ema20": entry - 0.001}
    acts = plan(st, PosView(1, entry + 0.0001, entry - risk, 0.10, 0.5), view, False, 0.00001, 0.0001, S)
    assert not any(a.kind == "partial" for a in acts) and not any(a.flag == "be" for a in acts)


def test_limit_price_keeps_fx_precision():
    dec = NS(direction=1, sl=1.08200, invalidation={"zone": [1.08310, 1.08350]})
    lp = limit_price(dec, 1.08420, 0.0010, CFG)
    assert lp is not None and abs(lp - 1.0833) < 1e-6 and lp > dec.sl


# ---------------- Lebanon time ----------------
def test_beirut_time_is_dst_aware_and_never_a_fixed_offset():
    from timefmt import fmt_local, local_date
    summer = datetime(2026, 10, 8, 12, 30, tzinfo=timezone.utc)
    winter = datetime(2026, 1, 8, 12, 30, tzinfo=timezone.utc)
    assert fmt_local(summer) == "Thu 08 Oct 15:30 Beirut"
    assert fmt_local(winter) == "Thu 08 Jan 14:30 Beirut"
    assert local_date(datetime(2026, 10, 8, 22, 30, tzinfo=timezone.utc)) == "2026-10-09"   # local midnight rollover
    assert fmt_local(summer, tz="Not/AZone").endswith("12:30 UTC")                         # explicit UTC fallback


# ---------------- notification state engine ----------------
def test_notification_manager_suppresses_repeats_and_persists(tmp_path):
    from brain.notify import NotificationManager
    p = tmp_path / "n.json"
    nm = NotificationManager(p)
    assert nm.daily_due("market_health", "2026-10-11") and not nm.daily_due("market_health", "2026-10-11")
    assert nm.daily_due("market_health", "2026-10-12")
    assert nm.state_changed("opp", ["WAIT"]) and not nm.state_changed("opp", ["WAIT"])     # WAIT -> WAIT silent
    assert nm.state_changed("opp", ["SIGNAL"])                                              # WAIT -> SIGNAL speaks
    eid = "USD_CPI_2026-10-08_12:30"
    assert nm.event_stage(eid, "ANNOUNCED") and not nm.event_stage(eid, "ANNOUNCED")
    assert not nm.event_stage(eid, "DISCOVERED")                                           # never goes backwards
    assert nm.event_stage(eid, "RELEASED", detail=["0.4%", "0.3%"])
    assert not nm.event_stage(eid, "RELEASED", detail=["0.4%", "0.3%"])
    assert nm.event_stage(eid, "RELEASED", detail=["0.5%", "0.3%"])                        # material revision
    assert nm.trade_stage(42, "TRADE_OPEN") and not nm.trade_stage(42, "TRADE_OPEN")
    again = NotificationManager(p)                                                          # restart: no replay
    assert not again.daily_due("market_health", "2026-10-12") and not again.trade_stage(42, "TRADE_OPEN")
    assert again.event_status(eid) == "RELEASED"


def test_event_ids_are_stable():
    from events import event_id, event_record
    t = datetime(2026, 10, 8, 12, 30, tzinfo=timezone.utc)
    ev = {"title": "CPI m/m", "time": t, "currency": "USD", "impact": "High", "forecast": "0.3%"}
    rec = event_record(ev, t - timedelta(minutes=90), CFG.events.classes)
    assert rec["id"] == event_id(ev, CFG.events.classes) and rec["id"].startswith("USD_") and rec["id"].endswith("2026-10-08_12:30")
    assert rec["status"] == "UPCOMING" and rec["minutes_to"] == 90.0
    released = event_record({**ev, "actual": "0.5%"}, t + timedelta(minutes=2), CFG.events.classes)
    assert released["status"] == "RELEASED" and released["id"] == rec["id"]


class _Tg:
    enabled = True

    def __init__(self):
        self.texts, self.photos = [], []

    def text(self, message, **kw):
        self.texts.append(message)
        return True

    def photo(self, path, caption=""):
        self.photos.append((path, caption))
        return True

    def close(self):
        pass


def _finding(name, score=70.0, facts=None):
    from brain.models import BrainFinding
    return BrainFinding(name, "OK", score, 0.7, 0, "", [], [], facts or {})


def _run(symbol, action="WAIT", permission="NO_TRADE", quality=40.0, news_facts=None, opp=70.0, blockers=("risk gate",)):
    from brain.models import BrainRun, V5Decision
    f = {k: _finding(k) for k in ("connection", "technical", "intermarket", "quantitative", "critic", "risk")}
    f["news"] = _finding("news", facts=news_facts or {})
    f["opportunity"] = _finding("opportunity", score=opp, facts={"best_candidate_score": opp})
    d = V5Decision(action=action, permission=permission, setup_quality=quality, symbol=symbol,
                   direction=1 if action == "BUY" else 0, blockers=list(blockers),
                   facts={"setup": "FVG_RETEST", "style": "day", "entry": 1.0843, "sl": 1.0828, "tp": 1.0888, "rr": 3.0})
    return BrainRun("2026-10-11T10:00:00+00:00", symbol, "RUNNING", findings=f, decision=d, report={"market_health": 70})


def _reporter(tmp_path, market):
    from brain.reporting import V5Reporter
    cfg = replace(Config(), system=replace(Config().system, data_dir=str(tmp_path)),
                  brain=replace(Config().brain, telegram_charts=False))
    rep = V5Reporter(cfg, market=market)
    rep.tg = _Tg()
    return rep


def test_reporter_is_silent_on_repeated_states_and_names_the_active_market(tmp_path):
    active = {"sym": "EURUSDs"}
    rep = _reporter(tmp_path, lambda: active["sym"])
    for _ in range(5):                                         # repeated scans with the same NO_TRADE reason
        rep.publish(_run("EURUSDs"))
    no_trade = [t for t in rep.tg.texts if "NO TRADE" in t]
    assert len(no_trade) == 1 and "Active market: EURUSDs" in no_trade[0] and "Beirut" in no_trade[0]
    rep.publish(_run("EURUSDs", "BUY", "APPROVE", quality=85.0, blockers=()))   # WAIT -> opportunity
    rep.publish(_run("EURUSDs", "BUY", "APPROVE", quality=86.0, blockers=()))   # same 10-pt band: silent
    opp = [t for t in rep.tg.texts if "BEST OPPORTUNITY" in t]
    assert len(opp) == 1 and "1.08430" in opp[0]                               # FX precision in the text
    active["sym"] = "USDJPY"                                                   # selector switched markets
    rep.publish(_run("USDJPY"))
    assert "Active market: USDJPY" in rep.tg.texts[-1]
    assert "XAUUSD" not in "".join(rep.tg.texts)


def test_news_events_are_announced_and_released_once(tmp_path):
    rep = _reporter(tmp_path, lambda: "EURUSD")
    now = datetime.now(timezone.utc)
    t = now + timedelta(minutes=45)
    ev = {"id": f"USD_CPI_{t:%Y-%m-%d_%H:%M}", "title": "CPI m/m", "currency": "USD", "impact": "High", "cls": "CPI",
          "category": "inflation", "time": t.isoformat(), "minutes_to": 45.0, "forecast": "0.3%", "previous": "0.2%",
          "actual": None, "status": "UPCOMING", "relevant": True}
    for _ in range(3):
        rep.publish(_run("EURUSD", news_facts={"events": [ev]}))
    briefing = [x for x in rep.tg.texts if "DAILY NEWS BRIEFING" in x]
    announce = [x for x in rep.tg.texts if "UPCOMING HIGH-IMPACT NEWS" in x]
    assert len(briefing) == 1 and len(announce) == 1
    assert "EURUSD ↓" in announce[0] and "Beirut" in announce[0] and "Fed" in announce[0]
    rel = {**ev, "actual": "0.5%", "status": "RELEASED", "minutes_to": -3.0}
    for _ in range(3):
        rep.publish(_run("EURUSD", news_facts={"events": [rel]}))
    released = [x for x in rep.tg.texts if "NEWS RELEASED" in x]
    assert len(released) == 1 and "better than expected" in released[0] and "bearish" in released[0]


def test_trade_lifecycle_messages_are_sent_once(tmp_path):
    rep = _reporter(tmp_path, lambda: "XAUUSD")
    rec = {"symbol": "XAUUSD", "direction": 1, "setup": "FVG_RETEST", "entry": 2400.0, "sl": 2397.0, "tp": 2409.0,
           "tp1": 2404.5, "lots": 0.1, "risk_pct": 0.5, "rr": 3.0, "mode": "DEMO", "ticket": 77}
    rep.trade_entry_feedback(rec)
    rep.trade_entry_feedback(rec)
    rep.trade_management(77, "PARTIAL", {"volume": 0.05, "reason": "TP1 reached"})
    rep.trade_management(77, "PARTIAL", {"volume": 0.05})
    rep.trade_management(77, "TRAILING", {"sl": 2401.0})
    rep.trade_feedback({"ticket": 77, "symbol": "XAUUSD", "direction": 1, "net_pnl": 30.0, "r_multiple": 1.0})
    rep.trade_feedback({"ticket": 77, "symbol": "XAUUSD", "direction": 1, "net_pnl": 30.0, "r_multiple": 1.0})
    kinds = [x.split("\n")[1].split(" — ")[0] for x in rep.tg.texts]
    # Default: the record-based notifier (telegram_explain) owns the OPEN/CLOSE text -> no duplicate here.
    assert kinds == ["TRADE MANAGEMENT"]
    solo = _reporter(tmp_path / "solo", lambda: "XAUUSD")
    solo.explain_active = False
    solo.trade_entry_feedback(rec)
    solo.trade_entry_feedback(rec)
    solo.trade_management(77, "BE", {"sl": 2400.3, "reason": "after TP1"})
    solo.trade_feedback({"ticket": 77, "symbol": "XAUUSD", "direction": 1, "net_pnl": 30.0, "r_multiple": 1.0})
    solo.trade_feedback({"ticket": 77, "symbol": "XAUUSD", "direction": 1, "net_pnl": 30.0, "r_multiple": 1.0})
    kinds = [x.split("\n")[1].split(" — ")[0] for x in solo.tg.texts]
    assert kinds == ["TRADE OPENED", "TRADE MANAGEMENT", "TRADE FINISHED"]
    assert "TP1 2404.50" in solo.tg.texts[0]


# ---------------- charts: exact trade levels ----------------
def _ohlc(n=200, start=1.08, step=0.00005):
    idx = pd.date_range("2026-10-01", periods=n, freq="15min")
    c = pd.Series(start + np.cumsum(np.sin(np.arange(n) / 7.0) * step), index=idx)
    return pd.DataFrame({"open": c.shift(1).fillna(c.iloc[0]), "high": c + 0.0003, "low": c - 0.0003, "close": c,
                         "tick_volume": 100.0}, index=idx)


def test_execution_chart_levels_are_the_execution_candidate(tmp_path):
    from brain.chart import plan_from_decision, render_execution, render_indicators, render_structure
    dec = NS(direction=1, entry=1.08431, sl=1.08281, tp=1.08881, tp1=1.08656, rr=3.0, setup="FVG_RETEST",
             invalidation={"zone": [1.08300, 1.08340]})
    plan = plan_from_decision(dec, "EURUSD", "M15")
    assert (plan.entry, plan.sl, plan.tp1, plan.tp2) == (dec.entry, dec.sl, dec.tp1, dec.tp)
    assert plan.entry_zone == (1.083, 1.0834) and plan.source == "candidate"
    filled = plan_from_decision(dec, "EURUSD", "M15", execution={"fill": 1.08433, "sl": 1.08281, "tp": 1.08881, "ticket": 9})
    assert filled.entry == 1.08433 and filled.source == "executed" and filled.ticket == 9
    df = _ohlc()
    assert Path(render_execution(df, plan, path=tmp_path / "c.png", title="T")).exists()
    assert Path(render_indicators(df, path=tmp_path / "b.png", title="T")).exists()
    view = {"fvg": [{"type": "bull", "top": 1.0834, "bottom": 1.0830, "quality": 60}], "ob": [],
            "sr": [{"level": 1.0845, "touches": 3}], "swing_highs": [(150, 1.0850)], "swing_lows": [(170, 1.0825)],
            "struct_trend": 1, "struct_regime": "BULLISH", "sweep": None}
    assert Path(render_structure(df, view, path=tmp_path / "a.png", title="T")).exists()


# ---------------- instrument-aware intelligence ----------------
def test_intel_prompt_is_instrument_specific_and_schema_is_backward_compatible():
    from instruments import profile_for
    from intel import system_prompt, validate_intel
    jpy = system_prompt(profile_for("USDJPY"))
    assert "USDJPY" in jpy and "BoJ" in jpy and "Analyse ONLY USDJPY" in jpy and "central-bank gold" not in jpy
    assert "ECB" in system_prompt(profile_for("EURUSD")) and "OPEC" in system_prompt(profile_for("USOIL"))
    base = {"confidence": 0.7, "risk_level": "low", "trade_veto": False, "expiry_minutes": 60, "key_drivers": [],
            "contradictions": [], "summary": "x"}
    assert validate_intel({**base, "instrument_bias": "bearish"}, CFG.intel)["instrument_bias"] == "bearish"
    assert validate_intel({**base, "gold_bias": "bullish"}, CFG.intel)["instrument_bias"] == "bullish"


def test_switching_instrument_discards_previous_market_intelligence():
    import time
    from instruments import profile_for
    from news import NewsEngine
    ne = NewsEngine(CFG, http_get=lambda *a, **k: b"", llm=lambda s, u: "")
    ne.set_instrument(profile_for("XAUUSD"))
    ne.intel = {"gold_bias": "bullish", "instrument_bias": "bullish", "confidence": 0.9, "fetched_at": time.time(),
                "expires_at": time.time() + 3600}
    ne.intel_status = "OK"
    ne.set_instrument(profile_for("USDJPY"))
    assert ne.intel is None and ne.intel_status == "UNKNOWN"


# ---------------- engine lifecycle + identity ----------------
def test_halted_is_not_downgraded_and_broker_glitch_is_recoverable():
    import test_mt5_engine as T
    from engine import BROKER_DEGRADED, Lifecycle
    eng, cfg, client = T.started_engine("DRY_RUN")
    assert eng.lifecycle == Lifecycle.RUNNING
    eng.set_lifecycle(Lifecycle.HALTED, "operator stop")
    n = len(eng.alerts.msgs)
    eng.set_lifecycle(Lifecycle.DEGRADED, "stale ticks (99s)")
    assert eng.lifecycle == Lifecycle.HALTED and len(eng.alerts.msgs) == n          # no flapping, no alert spam
    Path(eng.safety_latch).unlink(missing_ok=True)
    eng.set_lifecycle(Lifecycle.RUNNING, "test reset")
    eng.set_lifecycle(Lifecycle.DEGRADED, BROKER_DEGRADED + "profit scale mismatch")
    assert eng.lifecycle == Lifecycle.DEGRADED and not Path(eng.safety_latch).exists()   # no durable latch
    assert not eng.can_enter()[0]


def test_alert_prefix_follows_the_active_market():
    from alerts import Alerts
    a = Alerts(sender=lambda m: None)
    sym = {"s": "EURUSDs"}
    a.market = lambda: sym["s"]
    assert a.identity() == "SLANGO PRO TRADER | Active market: EURUSDs"
    sym["s"] = "USDJPY"
    assert "USDJPY" in a.identity()
    from explain import fmt_close
    assert "CLOSE BUY GBPUSD" in fmt_close({"symbol": "GBPUSD", "direction": 1, "setup": "X", "style": "day"})
