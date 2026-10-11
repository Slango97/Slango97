from dataclasses import replace
from types import SimpleNamespace

import pandas as pd

from config import Config
from brain.universe import MarketUniverseBrain


def _df(price=1.1, periods=180):
    idx = pd.date_range("2026-10-01", periods=periods, freq="15min")
    c = pd.Series([price + i * 0.00005 for i in range(periods)], index=idx, dtype=float)
    return pd.DataFrame({"open": c - 0.00002, "high": c + 0.00010, "low": c - 0.00010,
                         "close": c, "tick_volume": 100.0}, index=idx)


class SelectClient:
    symbol = "XAUUSD"

    def account(self):
        return SimpleNamespace(equity=5.0)

    def utc_now(self):
        return pd.Timestamp("2026-10-07 14:00:00")

    def resolve_market_symbol(self, requested, aliases=None):
        return {"XAUUSD": "XAUUSD", "EURUSD": "EURUSD", "GBPUSD": "GBPUSD"}.get(requested)

    def tick_age_for(self, symbol):
        return 0.2

    def rates_for(self, symbol, tf, n):
        return _df(2000 if "XAU" in symbol else 1.1), []

    def symbol_info_for(self, symbol):
        return SimpleNamespace(point=0.00001 if "USD" in symbol and "XAU" not in symbol else 0.01,
                               trade_stops_level=0, volume_min=0.01)

    def tick_for(self, symbol):
        p = 2000.0 if "XAU" in symbol else 1.1
        return SimpleNamespace(ask=p + 0.0001, bid=p - 0.0001)

    def calc_loss_per_lot_for(self, symbol, direction, entry, sl):
        # EURUSD is affordable on $5; XAUUSD is intentionally too expensive at 0.01 lot.
        return 4.0 if "EUR" in symbol else 800.0


def test_adaptive_rank_prefers_affordable_market_over_unaffordable_market():
    cfg = replace(Config(), brain=replace(Config().brain, universe_max_symbols=3,
                                           watchlist=("XAUUSD", "EURUSD", "GBPUSD")))
    rows = MarketUniverseBrain(cfg).scan(SelectClient(), primary_symbol="XAUUSD")
    ranked = MarketUniverseBrain(cfg).rank(SelectClient(), rows, cfg=cfg)
    eur = next(r for r in ranked if r.requested == "EURUSD")
    xau = next(r for r in ranked if r.requested == "XAUUSD")
    assert eur.affordable
    assert not xau.affordable
    assert eur.ranking_score > xau.ranking_score


def test_adaptive_rank_never_marks_missing_broker_data_affordable():
    cfg = Config()
    rows = [__import__('brain.universe', fromlist=['MarketHealth']).MarketHealth("EURUSD", "EURUSD", "OK", 90, .9, 1)]
    class Bad(SelectClient):
        def symbol_info_for(self, symbol):
            return None
    ranked = MarketUniverseBrain(cfg).rank(Bad(), rows, cfg=cfg)
    assert not ranked[0].affordable
    assert ranked[0].ranking_score < 35


def test_account_level_selector_switches_to_best_market_only_when_flat():
    from brain.system import V5BrainSystem
    from brain.universe import MarketHealth
    from dataclasses import replace

    cfg = replace(Config(), brain=replace(Config().brain, adaptive_market_selection=True,
                                           adaptive_selection_refresh_min=0.001,
                                           adaptive_selection_min_score=65.0),
                  system=replace(Config().system, symbol="AUTO"))

    class C(SelectClient):
        symbol = "XAUUSD"
        def switch_symbol(self, symbol):
            self.symbol = symbol
            return True
        def all_positions(self):
            return []

    c = C()
    system = V5BrainSystem(cfg, client=c)
    row = MarketHealth("EURUSD", "EURUSD", "OK", 90, .9, 1, affordable=True, ranking_score=92, regime="STRONG_TREND")
    system.universe.scan = lambda *a, **k: [row]
    system.universe.rank = lambda *a, **k: [row]
    system.reporter.selection_report = lambda rows: None
    result = system.select_market_if_due(force=True)
    assert result["changed"] and c.symbol == "EURUSD"
