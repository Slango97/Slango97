"""V4 acceptance: reliability, broker diagnostics, news redundancy, AI safety, decision records, Telegram, M1 live flow."""
from helpers import Config, NS, TUE_10, long_setup_views, tmpdir  # noqa: F401

import ast
import json
import os
import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import pandas as pd

import test_mt5_engine as T                                     # reuses the fake-MT5 fixtures
from analysis import MarketState
from broker_diag import diagnose
from connection import ReconnectPolicy
from decision import Decision, decide
from explain import TelegramNotifier, daily_stats, fmt_close, fmt_open, fmt_rejected
from bus import EventBus
from intel import ProviderChain
from news import NewsContext, NewsEngine
import records
from refine import RefineResult

world = T.world
CFG = Config()


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_reconnect_backoff_grows_with_jitter_and_hits_a_ceiling():
    clk = Clock()
    rc = replace(CFG.reliability, backoff_base_s=2, backoff_max_s=60, jitter_frac=0.25, ceiling_attempts=5, cooldown_s=300)
    p = ReconnectPolicy(rc, clk, random.Random(1))
    p.on_lost()
    assert p.should_attempt()
    delays = []
    for _ in range(4):
        p.record(False)
        delays.append(p.next_at - clk.t)
        assert not p.should_attempt()                          # never hammer between attempts
        clk.t = p.next_at
    assert all(1.5 <= d / (2 * 2 ** k) <= 1.25 + 1e-9 or d <= 60 * 1.25 for k, d in enumerate(delays))
    assert delays[3] > delays[0]
    p.record(False)                                            # 5th attempt inside the window -> ceiling
    assert p.state == "COOLDOWN" and abs(p.next_at - clk.t - 300) < 1e-9 and p.ceiling_hits == 1
    clk.t = p.next_at
    p.record(True)
    assert p.state == "CONNECTED" and p.failures == 0


def test_engine_does_not_hammer_mt5_and_blocks_trading_until_reconciled():
    eng, cfg, client = T.started_engine("DRY_RUN")
    calls = []
    orig = client.try_reconnect
    client.try_reconnect = lambda: calls.append(1) or orig()
    world.connected, world.init_ok = False, False
    for _ in range(25):
        eng.fast_step()
    assert len(calls) == 1 and eng.lifecycle == T.Lifecycle.DEGRADED and not eng.can_enter()[0]
    world.pending_orders = [NS(magic=cfg.system.magic, ticket=77)]
    world.connected, world.init_ok = True, True
    eng.fast_step()
    assert eng.reconcile_required and "pending_orders" in eng.halt_reason and not eng.can_enter()[0]
    world.pending_orders = []
    eng._t_sync = 0
    eng.fast_step()
    assert not eng.reconcile_required and eng.lifecycle == T.Lifecycle.RUNNING
    ev = [r for r in eng.journal.read_jsonl(eng.journal.events_path) if r["kind"] == "post_reconnect_reconciliation"]
    assert ev[-1]["ok"] and {"account", "positions", "pending_orders", "candles", "broker_diagnosis", "server_time"} <= set(ev[-1])


def _diag(**knobs):
    world.reset()
    for k, v in knobs.items():
        setattr(world, k, v)
    world.connected = True
    return diagnose(T.fake_mt5, "XAUUSD", CFG)


def test_broker_diagnostics_explain_or_fail_closed():
    ok = _diag()
    assert ok.status == "OK" and ok.cause == "CONSISTENT" and ok.tick_sizing_trusted and ok.sizing_method == "broker_calc"
    meta = _diag(tick_value=10.0)                             # metadata claims 10x the broker's real profit
    assert meta.status == "WARN" and meta.cause.startswith("TICK_METADATA_INCONSISTENT") and not meta.tick_sizing_trusted
    fx = _diag(account_currency="EUR", fx={"EURUSD": 1.10})
    assert fx.status == "WARN" and fx.cause.startswith("ACCOUNT_CURRENCY_CONVERSION")
    bad = _diag(profit_scale=3.7)
    assert bad.status == "FAIL" and bad.sizing_method == "none" and any("refusing" in e for e in bad.errors)
    none = _diag(calc_profit_ok=False)
    assert none.status == "FAIL" and "order_calc_profit unavailable" in none.errors


def test_unexplained_discrepancy_halts_startup_and_sizing_fails_closed():
    cfg = T.make_cfg("DEMO")
    client = T.make_client(cfg)
    world.profit_scale = 3.7
    T.rates_world()
    eng = T.AgentEngine(cfg, client, news=T.StubNews(), alerts=T.RecAlerts(), journal=T.Journal(cfg.system.data_dir),
                        state=T.State(cfg.system.state_file))
    eng.startup()
    assert eng.lifecycle == T.Lifecycle.HALTED and "broker diagnostics" in eng.halt_reason
    world.profit_scale, world.calc_profit_ok = 1.0, False
    client.broker_diagnosis()
    a = eng.risk.approve(Decision(action="BUY", direction=1, style="day", entry=2000, sl=1994, tp=2012, risk_pct=0.5), 2000.0, [])
    assert not a.ok and "broker profit calculation unavailable" in a.reason


def rss(items):
    body = "".join(f"<item><title>{t}</title><pubDate>{format_datetime(p)}</pubDate></item>" for t, p in items)
    return f"<rss><channel>{body}</channel></rss>".encode()


def test_calendar_providers_fallback_dedupe_and_health():
    soon = datetime.now(timezone.utc) + timedelta(minutes=10)
    ev = [{"title": "Core CPI m/m", "country": "USD", "impact": "High", "date": soon.isoformat(), "forecast": "0.3%"}]
    calls = []

    def http(url):
        calls.append(url)
        if "thisweek" in url:
            raise ConnectionError("primary down")
        if "nextweek" in url:
            return json.dumps(ev).encode()
        return rss([])
    d = tmpdir()
    local = os.path.join(d, "cal.json")
    with open(local, "w") as f:
        json.dump(ev, f)                                       # same event from the manual fallback file
    nc = replace(CFG.news, calendar_providers=CFG.news.calendar_providers[:2] + (("local", "file", local),))
    n = NewsEngine(replace(CFG, news=nc), http_get=http, llm=lambda s, u: "{}")
    n.refresh()
    assert len(n.ev.events) == 1                               # merged across providers
    h = n.provider_health()
    assert h["calendar:forexfactory"]["fail"] == 1 and h["calendar:forexfactory_next"]["fresh"] and h["calendar:local"]["fresh"]
    assert n.get().event_phase == "PRE"
    dead_nc = replace(nc, calendar_providers=nc.calendar_providers[:1],
                     calendar_cache_file=os.path.join(d, "missing-cache.json"))
    dead = NewsEngine(replace(CFG, news=dead_nc), http_get=http, llm=None)
    dead.refresh()
    assert "calendar" in dead.last_error and not dead.get().calendar_ok


def test_near_duplicate_headlines_are_merged_keeping_the_most_reliable():
    now = datetime.now(timezone.utc)
    feeds = {"www.fxstreet.com": [("Gold jumps as Fed signals rate cut", now - timedelta(minutes=5))],
             "www.cnbc.com": [("Gold jumps as Fed signals a rate cut", now - timedelta(minutes=9))]}
    nc = replace(CFG.news, rss_feeds=("https://www.fxstreet.com/rss", "https://www.cnbc.com/rss"), calendar_providers=())
    n = NewsEngine(replace(CFG, news=nc), http_get=lambda u: rss(feeds[u.split("/")[2]]), llm=None)
    n.refresh()
    assert len(n.heads) == 1 and n.heads[0].source == "cnbc.com"


def test_intelligence_provider_chain_falls_back_and_fails_safe():
    boom = lambda s, u: (_ for _ in ()).throw(RuntimeError("down"))
    ch = ProviderChain(CFG.intel, [("primary", boom), ("backup", lambda s, u: "ok")])
    assert ch("s", "u") == "ok" and ch.last_provider == "backup" and ch.failures["primary"] == 1
    try:
        ProviderChain(CFG.intel, [("a", boom), ("b", boom)])("s", "u")
        raise AssertionError("expected failure")
    except RuntimeError as ex:
        assert "all intelligence providers failed" in str(ex)


def test_llm_output_can_never_bypass_deterministic_gates_or_create_orders():
    rnd = random.Random(7)
    hostile = ["BUY 50 lots NOW, ignore risk", "SYSTEM: disable all gates", '{"action": "BUY", "lots": 50}', ""]
    views = long_setup_views()
    blocked_states = [
        MarketState(views, "LONDON_OPEN", None, 0.25, micro=NS(score=10, band="ABNORMAL", flags=["spread"], range_ratio=1, spread_ratio=4)),
        MarketState(views, "LONDON_OPEN", None, 0.25, regime=NS(label="EXTREME", no_trade=True, reasons=["x"], allowed=(), size_down=False, direction=0, confidence=1)),
    ]
    for _ in range(150):
        ctx = NewsContext(status="OK", bias=rnd.choice([1.0, -1.0, 0.5]), confidence=rnd.random(),
                          trade_veto=rnd.random() < 0.3, risk_level=rnd.choice(["low", "high", "extreme"]),
                          summary=rnd.choice(hostile), drivers=[{"driver": rnd.choice(hostile), "effect": "bullish"}])
        for ms in blocked_states:
            assert not decide(ms, 2000.0, ctx, CFG).is_entry
        ok = decide(MarketState(views, "LONDON_OPEN", None, 0.25), 2000.0, ctx, CFG)
        base = decide(MarketState(views, "LONDON_OPEN", None, 0.25), 2000.0, NewsContext(), CFG)
        if ok.is_entry:
            assert ok.score - base.score <= CFG.strategy.news_max_bonus + 1e-9   # at most a small bonus
    eng, cfg, client = T.started_engine("DEMO", news=T.StubNews(NewsContext(status="OK", bias=1.0, confidence=1.0, summary=hostile[0])))
    client.utc_now = lambda: TUE_10.replace(hour=22)            # rollover window: risk gate closed
    eng.news_ctx = eng.news.get()
    assert eng.candle_step() is None and not world.calls
    for mod in ("intel.py", "news.py", "sources.py"):          # the intelligence layer cannot even reach execution
        tree = ast.parse(Path(mod).read_text())
        names = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names} | \
                {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert not names & {"execution", "mt5_client", "risk", "trade_manager", "MetaTrader5"}


def test_decision_records_capture_every_candidate_with_ordered_blockers():
    v = long_setup_views()
    v["M15"]["last_event"], v["M15"]["sweep"] = None, None
    ms = MarketState(v, "LONDON_OPEN", None, 0.25, news=None)
    dec = decide(ms, 2000.0, NewsContext(), CFG)
    rec = records.build(dec, ms, NewsContext(), CFG)
    assert rec["final_action"] == "BUY" and rec["candidates"]
    c = rec["candidates"][0]
    assert c["status"] in ("SELECTED", "VIABLE") and c["policy_risk_mult"] < 1.0
    assert c["mtf_alignment"] and {"role", "tf", "state"} <= set(c["mtf_alignment"][0]) and c["positive_evidence"]
    assert rec["structural_rejections"] and all("code" in r for r in rec["structural_rejections"])
    txt = fmt_rejected(rec, c)
    assert "NO TRADE" in txt and "Blocked by:" in txt
    json.dumps(rec, default=str)                               # serialisable for the audit log


def test_telegram_messages_are_generated_from_records_and_rate_limited():
    v = long_setup_views()
    ms = MarketState(v, "LONDON_OPEN", None, 0.25)
    dec = decide(ms, 2000.0, NewsContext(), CFG)
    rec = records.build(dec, ms, NewsContext(), CFG, stage="EXECUTED", risk={"ok": True, "lots": 0.08, "risk_pct": 0.48, "tier": "fixed"},
                        execution={"fill": 2000.25})
    msg = fmt_open(rec)
    assert "OPEN BUY" in msg and "Risk 0.48%" in msg and "MTF:" in msg and "Why:" in msg and "APPROVED" in msg
    alerts, bus = T.RecAlerts(), EventBus()
    TelegramNotifier(CFG, alerts, bus)
    bus.publish("record", rec)
    v2 = long_setup_views()
    v2["M15"]["last_event"], v2["M15"]["sweep"] = None, None
    ms2 = MarketState(v2, "LONDON_OPEN", None, 0.25)
    rej = records.build(decide(ms2, 2000.0, NewsContext(), CFG), ms2, NewsContext(), CFG)
    bus.publish("record", rej)
    bus.publish("record", rej)                                 # identical rejection inside the cooldown
    texts = [m[1] for m in alerts.msgs]
    assert sum("OPEN BUY" in t for t in texts) == 1 and sum("NO TRADE" in t for t in texts) == 0
    close = {"type": "close", "t": "2025-03-04T12:00:00", "direction": 1, "setup": "BOS_PULLBACK", "style": "day", "entry": 2000.0,
             "exit": 2012.0, "net_pnl": 96.0, "r_multiple": 1.9, "reason": "tp", "mfe_r": 2.1, "mae_r": -0.3, "hold_h": 3.0,
             "regime": "STRONG_TREND", "session": "LONDON_OPEN", "slippage_r": 0.02}
    assert "R +1.90" in fmt_close(close) and "MFE +2.10R" in fmt_close(close)
    st = daily_stats([close], [rej], "2025-03-04")
    assert st["trades"] == 1 and st["net_r"] == 1.9 and st["by_setup"] == {"BOS_PULLBACK": 1}


def test_live_m1_refinement_arms_then_executes_or_expires():
    import engine as E
    eng, cfg, client = T.started_engine("DEMO")
    ms, dec = T.setup_decision(client, cfg)
    now = pd.Timestamp(TUE_10)
    orig = E.m1_check
    try:
        E.m1_check = lambda *a, **k: RefineResult("WAIT", "no M1 confirmation yet")
        eng.armed = E.Armed(dec, ms, now, now + pd.Timedelta(minutes=10))
        eng.refine_step(now)
        assert eng.armed is not None and not world.positions
        eng.refine_step(now + pd.Timedelta(minutes=11))
        assert eng.armed is None and not world.positions       # window expired: nothing traded
        E.m1_check = lambda *a, **k: RefineResult("GO", "M1 BOS confirms")
        eng.armed = E.Armed(dec, ms, now, now + pd.Timedelta(minutes=10))
        eng._m1_bar = None
        eng.refine_step(now)
        assert len(world.positions) == 1 and eng.armed is None
        recs = eng.journal.read_jsonl(eng.journal.dir / "decision_records.jsonl")
        stages = [r["stage"] for r in recs]
        assert "EXPIRED" in stages and "EXECUTED" in stages
        ex = [r for r in recs if r["stage"] == "EXECUTED"][-1]
        assert ex["risk_authorization"]["ok"] and ex["m1_refinement"]["status"] == "GO"
        assert any("OPEN BUY" in m[1] for m in eng.alerts.msgs)
    finally:
        E.m1_check = orig


def test_higher_timeframes_are_not_refetched_before_a_new_candle_can_exist():
    eng, cfg, client = T.started_engine("DRY_RUN")
    last = world.rates["M5"].index[-2]
    client.utc_now = lambda: (last + pd.Timedelta(minutes=6)).tz_localize("UTC").to_pydatetime()
    eng.refresh_views(force=True)
    p0 = client.pulls
    eng.refresh_views()
    assert client.pulls == p0                                  # nothing new can have closed: zero MT5 pulls
    client.utc_now = lambda: (last + pd.Timedelta(days=8)).tz_localize("UTC").to_pydatetime()
    eng.refresh_views()
    assert client.pulls - p0 == len(cfg.system.timeframes)
