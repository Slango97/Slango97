import os
import json
from dataclasses import replace
from datetime import datetime, timezone, timedelta

from config import Config
from risk import RiskCore
from optimize import monte_carlo
from news import NewsEngine
from learning import feature_version, code_fingerprint, dependency_fingerprint, config_fingerprint


def test_minimum_lot_never_exceeds_risk_budget():
    assert RiskCore.lots_for(0.5, 100.0, 0.01, 0.01, 100.0) == 0.0
    assert RiskCore.lots_for(10.0, 100.0, 0.01, 0.01, 100.0) == 0.1


def test_block_monte_carlo_is_deterministic_and_reports_block_len():
    r = [1.0, -0.5, 0.8, -0.2, 0.6, -0.4, 1.2, -0.3, 0.5, -0.1, 0.7, -0.2]
    a = monte_carlo(r, n_sims=500, seed=7, block_len=3)
    b = monte_carlo(r, n_sims=500, seed=7, block_len=3)
    assert a == b and a["block_len"] == 3 and "p_negative" in a


def test_provenance_fingerprint_changes_with_config():
    c = Config()
    base = feature_version(c)
    changed = feature_version(replace(c, strategy=replace(c.strategy, min_score=c.strategy.min_score + 1)))
    assert base != changed and len(code_fingerprint()) == 64 and len(dependency_fingerprint()) == 64 and len(config_fingerprint(c)) == 64


def test_calendar_te_provider_parsing():
    cfg = Config()
    n = NewsEngine(cfg, http_get=lambda _: b"[]", llm=None)
    evs = n._parse_calendar_te([{"Country": "United States", "Importance": 3,
                                 "Date": "2026-10-05T15:30:00", "Event": "CPI",
                                 "Forecast": "0.2%", "Previous": "0.1%", "Actual": None}])
    assert len(evs) == 1 and evs[0]["title"] == "CPI" and evs[0]["forecast"] == "0.2%"


def test_calendar_cache_is_used_when_remote_fails(tmp_path):
    now = datetime.now(timezone.utc)
    cache = tmp_path / "calendar_cache.json"
    event_time = now + timedelta(minutes=20)
    cache.write_text(json.dumps({"saved_at": now.isoformat(), "events": [{"title": "CPI", "time": event_time.isoformat(), "forecast": "0.2", "previous": "0.1", "actual": None}]}))
    cfg = replace(Config(), news=replace(Config().news, calendar_cache_file=str(cache), block_if_calendar_unavailable=True))
    def http(_):
        raise ConnectionError("offline")
    n = NewsEngine(cfg, http_get=http, llm=None, clock=lambda: now)
    n.refresh()
    c = n.get()
    assert c.calendar_ok and c.calendar_cache_age_h is not None


def test_config_defaults_fail_closed_and_require_independent_provider_when_configured():
    c = Config()
    assert c.news.block_if_calendar_unavailable is True
    assert c.news.calendar_require_independent_remote_when_available is True
    assert c.news.calendar_cache_max_age_h > 0


def test_calendar_cache_stale_fails_closed(tmp_path):
    now = datetime.now(timezone.utc)
    cache = tmp_path / "calendar_cache.json"
    old = now - timedelta(hours=30)
    cache.write_text(json.dumps({"saved_at": old.isoformat(), "events": []}))
    cfg = replace(Config(), news=replace(Config().news, calendar_cache_file=str(cache), calendar_cache_max_age_h=24, block_if_calendar_unavailable=True))
    def http(_):
        raise ConnectionError("offline")
    n = NewsEngine(cfg, http_get=http, llm=None, clock=lambda: now)
    n.refresh()
    c = n.get()
    assert c.blackout and "unavailable" in c.blackout_reason
