"""V5.8: live cross-market correlation matrix feeding portfolio risk."""
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from helpers import FakeClient, pos, tmpdir  # noqa: F401  (sets sys.path)
from config import Config
from correlation_matrix import CorrelationMatrix, compute, fetch_closes, market_key
from portfolio import correlation, correlation_range, portfolio_risk_pct, static_correlation

CFG = Config()
PC = CFG.portfolio


def _closes(n=800, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-08-01", periods=n, freq="1h")
    eq = rng.normal(0, 0.002, n)
    usd = rng.normal(0, 0.001, n)
    px = lambda r, p0: pd.Series(p0 * np.exp(np.cumsum(r)), index=idx)
    return {"US30": px(eq + rng.normal(0, 0.0005, n), 42000), "NAS100": px(1.2 * eq + rng.normal(0, 0.0007, n), 20000),
            "EURUSD": px(-usd + rng.normal(0, 0.0003, n), 1.08), "USDCHF": px(usd + rng.normal(0, 0.0003, n), 0.88),
            "BTCUSD": px(rng.normal(0, 0.004, n), 60000)}


def test_broker_names_are_mapped_before_any_correlation_lookup():
    assert market_key("XAUUSDs") == "XAUUSD" and market_key("GOLD") == "XAUUSD"
    assert static_correlation("XAUUSDs", "XAGUSD.a", PC) == static_correlation("XAUUSD", "XAGUSD", PC) > 0.8
    assert static_correlation("US30", "NAS100", PC) > 0.8                 # no more blind 0.5 for indices
    assert static_correlation("XAUUSD", "XYZ", PC) == PC.unknown_correlation


def test_matrix_measures_real_co_movement_and_shrinks_toward_the_prior():
    m = CorrelationMatrix(compute(_closes(), PC, lambda a, b: static_correlation(a, b, PC)))
    lo, hi = m.range("US30", "NAS100")
    assert lo > 0.8 and hi <= 1.0
    lo, hi = m.range("EURUSD", "USDCHF")
    assert hi < -0.6
    assert abs(sum(m.range("BTCUSD", "EURUSD")) / 2) < 0.3
    assert m.range("US30Cash", "USTEC") == m.range("US30", "NAS100")      # broker aliases
    assert m.range("EURUSD", "EURUSD") == (1.0, 1.0) and m.range("EURUSD", "GBPUSD") is None
    thin = compute({k: v.iloc[-20:] for k, v in _closes().items()}, PC, lambda a, b: 0.25)
    assert thin["pairs"]["NAS100|US30"]["low"] == 0.25                     # too little overlap -> prior only
    assert ("NAS100/US30", m.range("US30", "NAS100")[1]) in [(p, v) for p, v in m.strong_pairs(0.7)]


def test_stress_window_is_used_when_it_is_more_dangerous():
    c = _closes(900, seed=3)
    rng = np.random.default_rng(9)                 # last week: EURUSD and BTC suddenly move together
    shock = rng.normal(0, 0.003, 120)
    for k in ("EURUSD", "BTCUSD"):
        r = np.log(c[k]).diff().fillna(0).to_numpy(copy=True)
        r[-120:] = shock + rng.normal(0, 0.0005, 120)
        c[k] = pd.Series(c[k].iloc[0] * np.exp(np.cumsum(r)), index=c[k].index)
    m = CorrelationMatrix(compute(c, PC, lambda a, b: 0.0))
    lo, hi = m.range("EURUSD", "BTCUSD")
    assert hi > 0.5 and lo < 0.3                                             # long window calm, short window stressed
    same_dir = portfolio_risk_pct([("EURUSD", 0.5), ("BTCUSD", 0.5)], PC, m)
    assert same_dir > portfolio_risk_pct([("EURUSD", 0.5), ("BTCUSD", 0.5)], PC, m, hedge_credit=1.0) - 1e-12
    assert same_dir > 0.5 * np.sqrt(2 + 2 * 0.5)                          # sized with the stressed correlation


def test_hedges_are_only_partly_trusted():
    m = CorrelationMatrix(compute(_closes(), PC, lambda a, b: 0.0))
    long_both = portfolio_risk_pct([("US30", 0.5), ("NAS100", 0.5)], PC, m)
    hedged = portfolio_risk_pct([("US30", 0.5), ("NAS100", -0.5)], PC, m)
    naive = portfolio_risk_pct([("US30", 0.5), ("NAS100", -0.5)], PC, m, hedge_credit=1.0)
    assert long_both > 0.95 and naive < hedged < 0.71
    assert abs(correlation("US30", "NAS100", PC, m) - sum(m.range("US30", "NAS100")) / 2) < 1e-12
    assert correlation_range("US30", "NAS100", PC, None)[0] == static_correlation("US30", "NAS100", PC)


def _risk_engine(tmp, matrix_age_h=1.0, index_positions=1.0):
    from journal import Journal
    from risk import RiskEngine
    from state import State
    corr = os.path.join(tmp, "corr.json")
    m = compute(_closes(), PC, lambda a, b: static_correlation(a, b, PC))
    m["generated_at"] = (datetime.now(timezone.utc) - timedelta(hours=matrix_age_h)).isoformat()
    with open(corr, "w") as f:
        json.dump(m, f)
    cfg = replace(CFG, portfolio=replace(PC, corr_file=corr))
    c = FakeClient(10000.0, [])
    us30 = pos(5, d=1, entry=42000.0, sl=41900.0, vol=index_positions)
    us30.symbol = "US30Cash"
    c.all_positions = lambda: [us30]
    c.symbol = "NAS100"
    c.calc_loss_per_lot_for = lambda s, d, e, sl: abs(e - sl) * 1.0      # index CFD: $1 per point per lot
    r = RiskEngine(cfg, c, State(os.path.join(tmp, "s.json")), Journal(tmp))
    r.refresh()
    return r


def test_live_matrix_blocks_stacking_correlated_indices_and_stale_matrix_falls_back():
    from decision import Decision
    d = Decision(action="BUY", direction=1, style="day", entry=2000, sl=1994, tp=2012, risk_pct=0.5)
    r = _risk_engine(tmpdir(), index_positions=110.0)                       # 1.1% long US30 already open
    a = r.approve(d, 2000.0, [])
    assert not a.ok and "correlated" in a.reason and a.details["portfolio_risk_after"] > 1.5
    assert r.portfolio.matrix() is not None
    stale = _risk_engine(tmpdir(), matrix_age_h=48, index_positions=110.0)
    assert stale.portfolio.matrix() is None                                 # too old -> factor prior
    b = stale.approve(d, 2000.0, [])
    assert not b.ok                                                         # the prior also knows US30~NAS100 now


def test_refresh_fetches_closed_bars_and_persists(tmp_path):
    closes = _closes()

    class C:
        def rates_for(self, sym, tf, n):
            mk = market_key(sym)
            if mk not in closes:
                raise RuntimeError("not offered")
            return pd.DataFrame({"close": closes[mk].iloc[-n:]}), []
    got = fetch_closes(C(), ["US30", "NAS100", "XAUUSD"], "H1", 720, resolve=lambda mk: mk + "Cash" if mk == "US30" else mk)
    assert set(got) == {"US30", "NAS100"}
    from portfolio import PortfolioRisk
    cfg = replace(CFG, portfolio=replace(PC, corr_file=str(tmp_path / "m.json")))
    pr = PortfolioRisk(cfg, C())
    m = pr.refresh_matrix_if_due(["US30", "NAS100", "EURUSD", "USDCHF"])
    assert m is not None and (tmp_path / "m.json").exists() and pr.matrix() is m
    assert pr.refresh_matrix_if_due(["US30", "NAS100"]) is None            # not due again yet
    off = PortfolioRisk(replace(cfg, portfolio=replace(cfg.portfolio, live_correlation=False)), C())
    assert off.refresh_matrix_if_due(["US30", "NAS100"]) is None and off.matrix() is None


def test_daily_report_lists_correlated_clusters(tmp_path):
    from test_v55 import _reporter
    rep = _reporter(tmp_path, lambda: "NAS100")
    corr = tmp_path / "corr.json"
    CorrelationMatrix(compute(_closes(), PC, lambda a, b: 0.0)).save(corr)
    rep.cfg = replace(rep.cfg, portfolio=replace(PC, corr_file=str(corr)))
    line = rep._correlation_line()
    assert line.startswith("Correlated") and "NAS100/US30" in line
