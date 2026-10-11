from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from config import Config
from brain.universe import MarketUniverseBrain


def _df():
    idx = pd.date_range("2026-01-01", periods=100, freq="h")
    c = pd.Series([2000 + i * 0.2 for i in range(100)], index=idx, dtype=float)
    return pd.DataFrame({"open": c - 0.1, "high": c + 0.3, "low": c - 0.3, "close": c, "tick_volume": 1.0}, index=idx)


class FakeUniverseClient:
    symbol = "XAUUSDs"

    def resolve_market_symbol(self, requested, aliases=None):
        return {"XAUUSD": "XAUUSDs", "EURUSD": "EURUSD"}.get(requested)

    def tick_age_for(self, symbol):
        return 0.5

    def rates_for(self, symbol, tf, n):
        return _df(), []


def test_universe_resolves_and_scores_available_markets(tmp_path):
    cfg = replace(Config(), brain=replace(Config().brain, universe_refresh_min=1, universe_bars=80,
                                         universe_max_symbols=3, journal_file=str(Path(tmp_path) / "v5.jsonl"),
                                         watchlist=("XAUUSD", "EURUSD", "BTCUSD")))
    rows = MarketUniverseBrain(cfg).scan(FakeUniverseClient(), primary_symbol="XAUUSDs", primary_views=None)
    assert len(rows) == 3
    assert rows[0].symbol in ("XAUUSDs", "EURUSD")
    assert rows[0].status == "OK"
    assert 0 <= rows[0].health <= 100
    assert 0 <= rows[0].confidence <= 1


def test_universe_marks_unavailable_symbols():
    from dataclasses import replace
    cfg = Config()
    cfg = replace(cfg, brain=replace(cfg.brain, universe_max_symbols=9))   # V5.1 default is 5 (Blueprint: 2-5 assets)
    rows = MarketUniverseBrain(cfg).scan(FakeUniverseClient(), primary_symbol="XAUUSDs")
    btc = next(r for r in rows if r.requested == "BTCUSD")
    assert btc.status == "UNAVAILABLE"
    assert btc.health == 0


def test_v5_safety_honours_reconciliation_and_state_recovery():
    from brain.safety import evaluate
    s = evaluate(lifecycle="RUNNING", tick_age_s=0.1, max_tick_age_s=45, calendar_ok=True,
                 reconciliation_ok=False, state_recovery_pending=True)
    assert not s.allowed
    assert "reconciliation not clean" in s.reasons
    assert "state recovery pending" in s.reasons
