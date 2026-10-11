"""V5.1: the trading documents' news, risk and management rules; key-free official sources; multi-market profiles."""
from helpers import Config, NS, TUE_10, long_setup_views, tmpdir  # noqa: F401

import json
import os
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

import test_mt5_engine as T
from analysis import MarketState
from events import EventEngine
from instruments import profile_for
from news import NewsContext, NewsEngine, parse_bls_schedule

CFG = Config()
UTC = timezone.utc


# ---------------- multi-market profiles ----------------
def test_instrument_profiles_cover_every_asset_class():
    g, e, j, i, o, b, de = (profile_for(s) for s in ("XAUUSDs", "EURUSD.m", "USDJPY", "US30", "USOIL", "BTCUSD", "GER40"))
    assert (g.asset_class, g.calendar_currencies[:1], g.usd_beta) == ("metal", ("USD",), -1.0)
    assert e.asset_class == "fx" and set(e.calendar_currencies) >= {"EUR", "USD"} and e.currency_beta("EUR") == 1.0
    assert j.usd_beta == 1.0 and j.risk_beta > 0                      # risk-on weakens the yen: USDJPY up
    assert i.asset_class == "index" and o.asset_class == "energy" and b.trades_weekends
    assert de.quote == "EUR" and set(de.calendar_currencies) >= {"EUR", "USD"}


# ---------------- impact filter and currency relevance (documents) ----------------
FF = [{"title": "CPI m/m", "country": "USD", "date": "2026-10-08T12:30:00+00:00", "impact": "High", "forecast": "0.3%", "previous": "0.2%"},
      {"title": "ECB President Lagarde Speaks", "country": "EUR", "date": "2026-10-08T09:00:00+00:00", "impact": "Medium"},
      {"title": "German Final CPI m/m", "country": "EUR", "date": "2026-10-08T06:00:00+00:00", "impact": "Low"},
      {"title": "Bank Holiday", "country": "JPY", "date": "2026-10-08T00:00:00+00:00", "impact": "Holiday"}]


def test_calendar_keeps_red_and_orange_drops_yellow_and_filters_by_instrument():
    evs = NewsEngine._parse_calendar(FF, ("High", "Medium"), None)
    assert {e["title"] for e in evs} == {"CPI m/m", "ECB President Lagarde Speaks"}           # yellow + holiday dropped
    n = NewsEngine(CFG, http_get=lambda u: b"[]", llm=None)
    n._set_events(evs)
    assert [e["title"] for e in n.ev.events] == ["CPI m/m"]                                    # default: gold, USD only
    n.set_instrument(profile_for("EURUSD"))
    assert [e["title"] for e in n.ev.events] == ["CPI m/m"] and [e["title"] for e in n.ev.medium] == ["ECB President Lagarde Speaks"]


def test_red_news_windows_follow_the_documents():
    t0 = datetime(2026, 10, 8, 12, 30, tzinfo=UTC)
    eng = EventEngine(CFG)
    eng.set_events([{"title": "CPI m/m", "time": t0, "impact": "High", "currency": "USD", "forecast": "0.3%", "actual": "0.5%", "previous": "0.2%"},
                    {"title": "ISM Services PMI", "time": t0 + timedelta(hours=3), "impact": "Medium", "currency": "USD"}])
    calm = lambda: (1.0, 1.0)
    assert eng.state(t0 - timedelta(minutes=59)).label == "PRE_EVENT"                          # checklist: no red news in the next hour
    assert eng.state(t0 - timedelta(minutes=61)).phase in ("CAUTION", "CLEAR")
    assert eng.state(t0 + timedelta(minutes=14)).label == "INITIAL_REACTION"                   # first 5-15 min: watch only
    assert eng.state(t0 + timedelta(minutes=16), vol_probe=lambda: (3.0, 2.0)).label == "ABSORPTION"
    assert eng.state(t0 + timedelta(minutes=17), vol_probe=calm).label == "POST_EVENT"         # entries allowed again
    s = eng.state(t0 + timedelta(hours=3) - timedelta(minutes=5))
    assert not s.blackout and eng.upcoming_medium(t0 + timedelta(hours=2), 2)                 # orange: monitored only


def test_actual_vs_forecast_direction_per_instrument():
    from events import event_bias
    C = CFG.events.classes
    hot = {"title": "CPI m/m", "actual": "0.5%", "forecast": "0.3%", "previous": "0.2%", "currency": "USD"}
    assert event_bias(hot, C, profile_for("XAUUSD")) < 0 and event_bias(hot, C, profile_for("EURUSD")) < 0
    assert event_bias(hot, C, profile_for("USDJPY")) > 0 and event_bias(hot, C, profile_for("US30")) < 0
    eur = {**hot, "currency": "EUR"}
    assert event_bias(eur, C, profile_for("EURUSD")) > 0 and event_bias(eur, C, profile_for("XAUUSD")) is None
    unemp = {"title": "Unemployment Rate", "actual": "4.4%", "forecast": "4.2%", "previous": "4.2%", "currency": "USD"}
    assert event_bias(unemp, C, profile_for("XAUUSD")) > 0            # "Actual < Forecast = good for currency" data
    assert event_bias(hot, C) < 0                                     # legacy gold path unchanged


# ---------------- official key-free sources ----------------
BLS_HTML = """<h3>Upcoming News Releases</h3><ul>
<li><a href="#">Job Openings and Labor Turnover Survey</a><br>Tuesday, March 31, 2026 (10:00 AM)</li>
<li><a href="#">Employment Situation</a><br>Friday, April 03, 2026 (08:30 AM)</li>
<li><a href="#">Consumer Price Index</a><br>Friday, January 16, 2026 (08:30 AM)</li>
<li><a href="#">State Employment and Unemployment (Monthly)</a><br>Wednesday, April 08, 2026 (10:00 AM)</li></ul>"""


def test_official_bls_schedule_parses_eastern_time_to_utc():
    evs = {e["title"]: e for e in parse_bls_schedule(BLS_HTML)}
    assert set(evs) == {"JOLTS Job Openings (BLS)", "Non-Farm Employment Change (BLS Employment Situation)", "CPI m/m (BLS)"}
    assert evs["Non-Farm Employment Change (BLS Employment Situation)"]["time"] == datetime(2026, 4, 3, 12, 30, tzinfo=UTC)   # EDT
    assert evs["CPI m/m (BLS)"]["time"] == datetime(2026, 1, 16, 13, 30, tzinfo=UTC)                                        # EST
    assert evs["JOLTS Job Openings (BLS)"]["impact"] == "Medium"


def test_unpublished_and_unconfigured_providers_are_not_failures():
    ff = json.dumps([{**FF[0], "date": (datetime.now(UTC) + timedelta(days=1)).isoformat()}]).encode()

    def http(u):
        if "nextweek" in u:
            raise RuntimeError("404 Client Error: Not Found for url: " + u)
        if "bls.gov/opub" in u:
            return BLS_HTML.encode()
        return ff if "faireconomy" in u else b"<rss><channel></channel></rss>"
    te = ("tradingeconomics", "te_json", "https://api.example/cal?c=${TE_TEST_KEY}", "tradingeconomics")
    nc = replace(CFG.news, calendar_cache_file=os.path.join(tmpdir(), "c.json"), calendar_providers=CFG.news.calendar_providers + (te,))
    n = NewsEngine(replace(CFG, news=nc), http_get=http, llm=None)
    n.refresh()
    h = n.provider_health()
    assert h["calendar:forexfactory_next"]["state"] == "NOT_PUBLISHED" and h["calendar:forexfactory_next"]["fail"] == 0
    assert h["calendar:tradingeconomics"]["state"] == "NOT_CONFIGURED" and h["calendar:tradingeconomics"]["fail"] == 0
    c = n.get()
    assert c.calendar_ok and c.calendar_coverage == "CROSS_CHECKED"           # ForexFactory + official BLS, no API key


def test_default_sources_are_official_and_key_free():
    feeds = " ".join(CFG.news.rss_feeds)
    assert "federalreserve.gov" in feeds and "bls.gov" in feeds and "ecb.europa.eu" in feeds and "fxstreet" not in feeds
    assert not any("${" in str(p[2]) for p in CFG.news.calendar_providers)


def test_feeds_are_fetched_in_parallel():
    def http(u):
        time.sleep(0.4)
        return b"<rss><channel></channel></rss>"
    n = NewsEngine(CFG, http_get=http, llm=None)
    t0 = time.time()
    try:
        n._load_headlines()
    except RuntimeError:
        pass
    assert time.time() - t0 < 0.4 * len(CFG.news.rss_feeds) / 2


def test_headline_votes_are_signed_for_the_traded_market():
    from news_rules import assess_headlines
    h = [NS(title="Fed signals rate cuts ahead", weight=1.0)]
    assert assess_headlines(h, profile_for("XAUUSD")).bias > 0 and assess_headlines(h, profile_for("EURUSD")).bias > 0
    assert assess_headlines(h, profile_for("USDJPY")).bias < 0 and assess_headlines(h, profile_for("NAS100")).bias > 0
    ecb = [NS(title="ECB raises rates to fight inflation", weight=1.0)]
    assert assess_headlines(ecb, profile_for("EURUSD")).bias > 0 and assess_headlines(ecb, profile_for("GER40")).bias < 0
    assert "fed_policy" not in assess_headlines(ecb, profile_for("EURUSD")).families        # not a Fed vote


# ---------------- volatility regime ----------------
def test_extreme_needs_a_real_shock_not_a_busy_day():
    from indicators import atr, atr_regime
    rng = np.random.default_rng(3)

    def run(mult):
        vol = np.r_[np.full(400, 1.0), np.full(120, mult)]
        c = 2600 + np.cumsum(rng.normal(0, vol))
        o = np.r_[c[0], c[:-1]]
        a = atr(pd.DataFrame({"open": o, "high": np.maximum(o, c) + vol * 0.4, "low": np.minimum(o, c) - vol * 0.4, "close": c}))
        return [atr_regime(a.iloc[:400 + k])["regime"] for k in range(1, 121)]
    busy, shock = run(1.6), run(4.0)
    assert busy.count("EXTREME") <= 4 and "HIGH" in busy                   # V5: ~46 of 120 bars EXTREME
    assert "EXTREME" in shock


# ---------------- loss rules (documents) ----------------
def test_two_loss_lock_and_cooldown_are_account_wide():
    from test_risk_state_news import FakeClient, engine_for
    r1, _ = engine_for(FakeClient(10000.0, []))
    r2, _ = engine_for(FakeClient(10000.0, []))                         # a second market instance, same account
    r1.record_result(-100, -1.0)
    ok, why = r2.gate([])
    assert not ok and "cooldown" in why
    sh = r1._shared()
    sh["cooldown_until"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    r1._shared_save(sh)
    r1.record_result(-100, -1.0)
    ok, why = r2.gate([])
    assert not ok and ("two-loss" in why or "cooldown" in why)
    sh = r1._shared()
    sh["cooldown_until"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    r1._shared_save(sh)
    ok, why = r2.gate([])
    assert not ok and "two-loss" in why                                  # locked for the rest of the day
    sh["locked_day"] = "2000-01-01"
    r1._shared_save(sh)
    r1.record_result(-30, -0.3)                                          # small loss: cooldown, but not a full stop-out
    assert r1._shared().get("full_loss_streak") == 0


def test_backtest_applies_the_same_loss_rules():
    """Tight-stop longs at the real price every decision point: losses come quickly, and the two-loss rule must
    then stop entries for the rest of the day, exactly as live."""
    import copy
    import backtest as B
    import test_v431 as V431
    from decision import Decision
    sim = B.synthetic_m5(3000, seed=4)
    cfg = replace(CFG, features=replace(CFG.features, m1_refinement=False))
    btr = B.Backtester(cfg, B.BTConfig(decision_step=2, bars=300))
    prep = btr.prepare(sim, 5)
    orig_g, orig_p = B.generate, B.apply_policy

    def forced(ms, mid, c, aux=None):                               # 1 ATR stop (within the style's stop bounds), 4R target
        a = ms.views["H1"]["atr"]
        return [V431._entry(mid, mid - a, mid + 4 * a, [mid - 0.8 * a, mid - 0.2 * a])], []
    try:
        B.generate = forced
        sig = btr.signals(prep)
        B.apply_policy = lambda cands, ms, *a, **k: copy.copy(cands[0]) if cands else Decision(action="WAIT", wait_reason="NO_SETUP")
        sub = sig[len(sig) // 4:len(sig) // 4 + 200]
        tr, _, summ = btr.simulate(prep, sub, cfg, sub[0].i, sub[-1].i)
    finally:
        B.generate, B.apply_policy = orig_g, orig_p
    assert (tr["r"] <= -0.8).sum() >= 2 and "DOC_LOSS_RULES" in summ.get("no_trade_reasons", {})
    t = tr.assign(o=pd.to_datetime(tr["open_time"]), c=pd.to_datetime(tr["close_time"])).sort_values("c")
    streak, locks = 0, []
    for _, row in t.iterrows():                                        # when did two consecutive full losses happen?
        streak = streak + 1 if row["r"] <= -0.8 else 0
        if streak >= 2:
            locks.append(row["c"])
            streak = 0
    assert locks
    for lk in locks:                                                   # no NEW position after the lock, that same day
        same_day_after = t[(t["o"] > lk) & (t["o"].dt.date == lk.date())]
        assert same_day_after.empty, same_day_after[["o", "r"]]


# ---------------- TP1 / TP2 management (Blueprint section 6) ----------------
def test_tp1_level_uses_nearest_structure_at_least_one_r():
    from decision import tp1_level
    c = NS(entry=2000.0, sl=1990.0, tp=2040.0, direction=1, rr=4.0, barriers=[{"r": 0.6, "price": 2006.0}])
    assert tp1_level(c, CFG) == round(2000 + 15 - 0.5, 2) or tp1_level(c, CFG) == 2015.0
    c.barriers = [{"r": 1.8, "price": 2018.0}]
    assert abs(tp1_level(c, CFG) - (2018.0 - 0.5)) < 1e-9
    c.rr, c.barriers = 1.6, []
    assert tp1_level(c, CFG) is None                                     # single target


def test_tp1_closes_half_and_moves_stop_to_entry_plus_spread():
    from management import plan
    S = CFG.strategy.styles["day"] if hasattr(CFG.strategy, "styles") else CFG.styles["day"]
    st = {"direction": 1, "entry": 2000.0, "risk": 10.0, "tp1": 2015.0, "tp1_frac": 0.5}
    pos = NS(price=2012.0, age_h=1.0, volume=0.10, volume_step=0.01, volume_min=0.01, sl=1990.0)
    view = {"atr": 5.0, "close": 2012.0, "swing_lows": [], "swing_highs": [], "ema20": 2005.0, "ema50": 2000.0}
    assert not [a for a in plan(st, pos, view, False, 0.3, 0.5, S) if a.kind in ("partial", "move_sl")]   # 1.2R: wait for TP1
    pos.price = 2015.2
    acts = plan(st, pos, view, False, 0.3, 0.5, S)
    part = [a for a in acts if a.kind == "partial"]
    be = [a for a in acts if a.kind == "move_sl" and a.flag == "be"]
    assert part and abs(part[0].volume - 0.05) < 1e-9 and be and abs(be[0].value - 2000.3) < 1e-9


# ---------------- momentum triggers ----------------
def test_document_momentum_triggers_are_detected():
    from analysis import doc_momentum
    n = 80
    low = np.r_[np.linspace(2010, 2000, 40), np.linspace(2000, 2006, 20), np.linspace(2006, 1998, 15), [1999, 2000, 2001, 2002, 2003]]
    rsi = np.r_[np.linspace(50, 25, 40), np.linspace(25, 45, 20), np.linspace(45, 32, 15), [33, 34, 29, 28, 31]]
    k = np.r_[np.full(n - 2, 15.0), [12.0, 18.0]]
    dd = np.r_[np.full(n - 2, 16.0), [16.0, 15.0]]
    d = pd.DataFrame({"low": low, "high": low + 2, "rsi": rsi, "stoch_k": k, "stoch_d": dd})
    out = doc_momentum(d)
    assert out["rsi_reclaim"] == 1 and out["stoch_x"] == 1 and out["rsi_div"] == 1


# ---------------- checklist, critic, plans ----------------
def _entry_candidate():
    from decision import generate
    ms = MarketState(long_setup_views(), "LONDON_OPEN", None, 0.25)
    cands, _ = generate(ms, 2000.0, CFG)
    c = cands[0]
    c.action = "BUY"
    return c, ms


def test_preflight_checklist_is_recorded_for_entries():
    import records
    from preflight import checklist
    c, ms = _entry_candidate()
    items = checklist(c, ms, NewsContext(), CFG)
    assert len(items) == 9 and {i["group"] for i in items} == {"Macro", "Structural", "Trigger", "Risk", "Execution"}
    assert all(i["status"] in ("PASS", "FAIL", "INFO") for i in items)
    rec = records.build(c, ms, NewsContext(), CFG, "XAUUSD", "DECISION")
    assert "preflight" in rec and "pass" in rec["preflight"]["summary"]


def test_trade_critic_attacks_before_the_chief_decides():
    from brain.chief import ChiefTraderBrain
    from brain.critic import TradeCriticBrain
    from brain.models import BrainFinding
    c, _ = _entry_candidate()
    c.htf_bias, c.confirmed, c.triggered, c.score = -0.6, False, False, 90
    f = lambda name, bias=0, conf=0.0, facts=None: BrainFinding(name, "OK", 90.0, conf, bias, "", [], [], facts or {})
    critic = TradeCriticBrain().run(c, f("technical", 1), f("news", facts={"event_label": "CLEAR"}), f("intermarket"))
    assert critic.status == "BLOCK" and critic.facts["major"] >= 2
    chief = ChiefTraderBrain()
    chief.min_score = 85.0
    out = chief.run(symbol="XAUUSD", decision=c, connection=f("connection"), technical=f("technical", 1), quant=f("quant"),
                    news=f("news"), intermarket=f("intermarket"), opportunity=f("opportunity"),
                    risk=BrainFinding("risk", "OK", 100.0, 1.0, 0, "", [], [], {"risk_multiplier": 1.0}), critic=critic)
    assert out.permission == "BLOCK" and any("critic" in b for b in out.blockers)


def test_premarket_plan_and_weekly_review():
    from premarket import build_plan, weekly_review
    now = datetime(2026, 10, 8, 6, 0, tzinfo=UTC)
    n = NewsEngine(CFG, http_get=lambda u: b"[]", llm=None)
    n._set_events([{"title": "CPI m/m", "time": now.replace(hour=12, minute=30), "impact": "High", "currency": "USD"}])
    views = long_setup_views()
    views["D1"]["sr"] = [{"level": 2030.0, "touches": 4}, {"level": 1975.0, "touches": 3}]
    txt = build_plan(views, n, profile_for("XAUUSD"), now)
    assert "CPI m/m" in txt and "11:30 to 12:45" in txt and "IF price sweeps below 1975.0" in txt
    closes = [{"type": "close", "close_time": "2026-10-06T10:00:00", "r": x, "setup": s, "session": "LONDON_OPEN"}
              for x, s in ((2.0, "FVG_RETEST"), (-1.0, "BOS_PULLBACK"), (1.5, "FVG_RETEST"), (-1.0, "BOS_PULLBACK"))]
    rv = weekly_review(closes, now)
    assert "4 trades" in rv and "Win rate 50%" in rv and "best FVG_RETEST" in rv


# ---------------- engine wiring ----------------
def test_engine_applies_the_instrument_profile_and_cfd_spread_cap():
    eng, cfg, client = T.started_engine("DEMO")
    assert eng.profile.asset_class == "metal"
    eng.profile = profile_for("US30")
    eng._dynamic_spread_cap()
    cap = eng.risk.core.spread_cap_points
    assert cap and abs(cap - cfg.execution.max_spread_atr_frac * eng.views["M5"]["atr"] / client.symbol_info().point) < 1e-6


def test_watchlist_and_multi_market_launcher_follow_the_blueprint():
    from pathlib import Path
    # V5.4 scans a 12-market universe (ranking still narrows it to an actionable shortlist)
    assert 2 <= CFG.brain.universe_max_symbols <= 12 and len(CFG.brain.watchlist) >= CFG.brain.universe_max_symbols
    bat = (Path(__file__).resolve().parent.parent / "START_MULTI_V5_WINDOWS.bat").read_text()
    assert "SYSTEM__SYMBOL" in bat and "SYSTEM__DATA_DIR" in bat
