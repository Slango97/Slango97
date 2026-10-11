from helpers import Config, tmpdir  # noqa: F401

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from events import EventEngine, classify_event, event_bias, learn_impacts, parse_number
from intel import parse, validate_intel
from news import NewsEngine
from sources import assess, enforce, reliability

CFG = Config()
IC = CFG.intel


def intel_json(**kw):
    base = {"gold_bias": "bullish", "confidence": 0.8, "risk_level": "medium",
            "key_drivers": [{"driver": "Fed dovish", "effect": "bullish", "source": "reuters.com"}],
            "contradictions": [], "trade_veto": False, "expiry_minutes": 60, "summary": "Dovish tilt supports gold"}
    base.update(kw)
    return json.dumps(base)


def test_intel_schema_validation_is_strict_and_clamps_expiry():
    ok = validate_intel(json.loads(intel_json(expiry_minutes=10000)), IC)
    assert ok["expiry_minutes"] == IC.max_expiry_min and ok["gold_bias"] == "bullish"
    assert validate_intel(json.loads(intel_json(expiry_minutes=1)), IC)["expiry_minutes"] == IC.min_expiry_min
    for bad in [dict(gold_bias="up"), dict(confidence=1.5), dict(confidence=True), dict(risk_level="severe"),
                dict(trade_veto="no"), dict(expiry_minutes="soon"), dict(key_drivers=[{"driver": 1, "effect": "bullish"}]),
                dict(key_drivers=[{"driver": "x", "effect": "up"}]), dict(contradictions="none")]:
        try:
            validate_intel(json.loads(intel_json(**bad)), IC)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")
    assert parse("Here is my view:\n" + intel_json() + "\nthanks", IC)["confidence"] == 0.8


def test_event_classes_and_surprise_direction():
    C = CFG.events.classes
    assert classify_event("Core CPI m/m", C)[0] == "CPI" and classify_event("PPI m/m", C)[0] == "PPI"
    assert classify_event("10-y Bond Auction", C)[0] == "AUCTION" and classify_event("Unemployment Claims", C)[0] == "JOBLESS"
    assert parse_number("225K") == 225000 and parse_number("") is None
    assert event_bias({"title": "CPI y/y", "forecast": "3.0%", "previous": "3.2%", "actual": "3.5%"}, C) < 0
    assert event_bias({"title": "Unemployment Claims", "forecast": "220K", "previous": "215K", "actual": "260K"}, C) > 0


def test_event_lifecycle_pre_release_stabilize_reevaluate_clear():
    t0 = datetime(2025, 3, 12, 12, 30, tzinfo=timezone.utc)
    eng = EventEngine(CFG)
    eng.set_events([{"title": "CPI m/m", "time": t0, "forecast": "0.3%", "previous": "0.2%", "actual": "0.5%"}])
    calm, wild = (lambda: (1.0, 1.0)), (lambda: (3.0, 2.0))
    assert eng.state(t0 - timedelta(minutes=40)).phase == "CLEAR"
    pre = eng.state(t0 - timedelta(minutes=10))
    assert pre.phase == "PRE" and pre.blackout and pre.expected_move > 0
    assert eng.state(t0 + timedelta(minutes=3), calm).phase == "RELEASE"
    st = eng.state(t0 + timedelta(minutes=20), wild)
    assert st.phase == "STABILIZING" and st.blackout and st.surprise_bias < 0            # hot CPI: bearish gold
    assert eng.state(t0 + timedelta(minutes=25), calm).phase == "RE_EVALUATE"
    assert eng.state(t0 + timedelta(minutes=26), calm).phase == "CLEAR"
    eng2 = EventEngine(CFG)
    eng2.set_events([{"title": "CPI m/m", "time": t0}])
    assert eng2.state(t0 + timedelta(minutes=60)).phase == "STABILIZING"                 # no probe: wait to max
    assert eng2.state(t0 + timedelta(minutes=91)).phase == "CLEAR"


def test_learn_impacts_from_price_data():
    import pandas as pd
    idx = pd.date_range("2025-03-01", periods=3000, freq="5min")
    df = pd.DataFrame({"open": 2000.0, "high": 2001.0, "low": 1999.0, "close": 2000.0}, index=idx)
    events = []
    for k in range(4):
        t = idx[500 + k * 500]
        df.loc[t:t + pd.Timedelta(minutes=25), "high"] = 2020.0
        events.append({"title": "CPI m/m", "time": t.tz_localize("UTC").to_pydatetime()})
    out = learn_impacts(df, events, CFG.events.classes)
    assert abs(out["CPI"] - 20.0) < 1e-6


def test_source_reliability_and_confirmation():
    assert reliability("https://www.reuters.com/markets") > 0.9 and reliability("x.com") < 0.3
    assert reliability("https://feeds.bbci.co.uk/news/business/rss.xml") >= 0.85 and reliability("unknown.biz") == 0.3
    assert reliability("fox.com") == 0.3 and reliability("markets.reuters.com") > 0.9          # no substring false matches
    hs = assess([(10, "randomblog.net", 2, "Missile attack on Iran oil field reported"),
                 (12, "fxstreet.com", 2, "Gold rallies on reports of Iran attack"),
                 (15, "kitco.com", 1, "Iran attack lifts safe-haven demand")])
    assert all(h.high_impact for h in hs) and hs[0].confirmed                          # two reliable outlets agree
    lone = assess([(10, "randomblog.net", 2, "Missile attack on Iran oil field reported")])
    assert not lone[0].confirmed


def test_veto_requires_reliable_evidence():
    weak = {"trade_veto": True, "risk_level": "extreme", "key_drivers": [{"driver": "rumor", "effect": "bearish", "source": "x.com"}]}
    out, notes = enforce(dict(weak), assess([(5, "x.com", 1, "Rumor: emergency Fed meeting")]), IC)
    assert out["trade_veto"] is False and out["risk_level"] == "high" and notes
    strong = {**weak, "key_drivers": [{"driver": "Fed", "effect": "bearish", "source": "federalreserve.gov"}]}
    out2, _ = enforce(dict(strong), [], IC)
    assert out2["trade_veto"] is True


def rss(items):
    body = "".join(f"<item><title>{t}</title><pubDate>{format_datetime(p)}</pubDate></item>" for t, p in items)
    return f"<rss><channel>{body}</channel></rss>".encode()


def make_news(llm, events=None, items=None, fail_feeds=False, ic=None):
    now = datetime.now(timezone.utc)
    items = items if items is not None else [("Fed signals rate cut as inflation cools, gold jumps", now - timedelta(minutes=20)),
                                             ("Dollar slides as treasury yields drop", now - timedelta(minutes=50)),
                                             ("Local football results", now - timedelta(minutes=5))]

    def http(url):
        if "faireconomy" in url:
            return json.dumps(events or []).encode()
        if fail_feeds:
            raise ConnectionError("down")
        return rss(items)
    cfg = replace(CFG, intel=ic) if ic else CFG
    return NewsEngine(cfg, http_get=http, llm=llm)


def test_news_engine_intel_ok_then_expires():
    n = make_news(lambda s, u: intel_json(expiry_minutes=30))
    n.refresh()
    c = n.get()
    assert c.status == "OK" and c.bias > 0.9 and 0.7 < c.confidence <= 0.8 and c.drivers and c.expires_in_min > 29
    assert len(n.heads) == 2                                                        # irrelevant headline filtered
    n.intel["fetched_at"] -= 15 * 60
    n.intel["expires_at"] -= 15 * 60
    assert abs(n.get().confidence - 0.8 * 0.75) < 0.02                              # halfway through: 75% weight
    n.intel["fetched_at"] -= 20 * 60
    n.intel["expires_at"] -= 20 * 60
    assert n.get().status == "UNKNOWN" and n.get().confidence == 0                  # expired: no influence


def test_malformed_or_failing_intel_means_unknown():
    for llm in (lambda s, u: "gold up!!", lambda s, u: intel_json(confidence=7), lambda s, u: (_ for _ in ()).throw(RuntimeError("down"))):
        n = make_news(llm)
        n.refresh()
        c = n.get()
        assert c.status == "UNKNOWN" and c.bias == 0 and not c.trade_veto


def test_intel_veto_flows_to_policy_halt_only_when_reliable():
    from analysis import MarketState
    from decision import decide
    from helpers import long_setup_views
    n = make_news(lambda s, u: intel_json(trade_veto=True, risk_level="high",
                                          key_drivers=[{"driver": "war", "effect": "bearish", "source": "apnews.com"}]))
    n.refresh()
    ctx = n.get()
    assert ctx.trade_veto
    d = decide(MarketState(long_setup_views(), "LONDON_OPEN", None, 0.25), 2000.0, ctx, CFG)
    assert d.action == "HALT" and d.wait_reason == "NEWS"
    n2 = make_news(lambda s, u: intel_json(trade_veto=True, key_drivers=[{"driver": "tweet", "effect": "bearish", "source": "x.com"}]))
    n2.refresh()
    assert not n2.get().trade_veto


def test_calendar_blackout_survives_intel_failure_and_feeds_down():
    soon = datetime.now(timezone.utc) + timedelta(minutes=10)
    ev = [{"title": "Core CPI m/m", "country": "USD", "impact": "High", "date": soon.isoformat(), "forecast": "0.3%", "previous": "0.2%"}]
    n = make_news(lambda s, u: "garbage", events=ev, fail_feeds=True)
    n.refresh()
    c = n.get()
    assert c.blackout and c.event_phase == "PRE" and "CPI" in c.blackout_reason and c.impact == "high" and n.last_error


def test_no_provider_means_no_data_not_crash():
    n = make_news(None, ic=replace(IC, provider="none"))
    n._llm = None
    n.refresh()
    assert n.get().status in ("NO_DATA", "UNKNOWN")
