"""V5.6: per-market walk-forward research, market specs and the market-evidence gate."""
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pandas as pd
import pytest

import helpers as H  # noqa: F401  (sets sys.path)
from config import Config
from market_evidence import gate, status_for
from market_research import acceptance_metrics, discover, verdict
from market_specs import canonical, commission_in_profit_currency, spec_for

CFG = Config()


def test_broker_symbols_map_to_watchlist_markets():
    for raw, mk in (("XAUUSDs", "XAUUSD"), ("GOLD", "XAUUSD"), ("EURUSD.a", "EURUSD"), ("US30Cash", "US30"),
                    ("USTEC", "NAS100"), ("Bitcoin", "BTCUSD"), ("USDJPYm", "USDJPY")):
        assert canonical(raw) == mk, raw
    assert canonical("ZZZ") is None


def test_specs_are_per_market_and_broker_export_overrides_them():
    eur, gold, jpy = spec_for("EURUSD"), spec_for("XAUUSDs"), spec_for("USDJPY")
    assert eur.point == 0.00001 and eur.slippage < 0.001 and gold.slippage == 0.05     # no $0.02 slippage on FX
    assert commission_in_profit_currency(jpy, 150.0) == 3.5 * 150.0                   # USD commission in JPY P&L
    assert commission_in_profit_currency(eur, 1.08) == 3.5
    b = spec_for("EURUSD", {"point": 0.0001, "contract_size": 100000, "currency_profit": "USD", "median_spread_points": 9})
    assert b.source == "broker_export" and b.point == 0.0001 and abs(b.spread - 0.0009) < 1e-12


def _trades(rs, start="2025-01-06"):
    t0 = pd.Timestamp(start)
    rows = [{"open_time": t0 + pd.Timedelta(days=i), "close_time": t0 + pd.Timedelta(days=i, hours=4), "r": r}
            for i, r in enumerate(rs)]
    return pd.DataFrame(rows)


def test_acceptance_metrics_from_r_multiples():
    m = acceptance_metrics(_trades([2.0, -1.0] * 60), risk_pct=1.0)
    assert m["n"] == 120 and m["pf"] == 2.0 and m["win_rate"] == 0.5 and m["exp_r"] == 0.5
    assert 0 < m["max_dd_pct"] < 1.1 and m["sharpe"] > 0.8 and m["return_pct"] > 0
    bad = acceptance_metrics(_trades([-1.0] * 20 + [1.0] * 5), risk_pct=1.0)
    assert bad["max_dd_pct"] > 15 and bad["pf"] == 0.25
    assert acceptance_metrics(pd.DataFrame(), 1.0) == {"n": 0}


def test_verdict_lists_every_failed_criterion():
    good = {"n": 150, "pf": 1.6, "sharpe": 1.2, "max_dd_pct": 8.0}
    assert verdict(good, {"pf": 1.2}, {"p_negative": 0.05}, CFG) == ("PASS", [])
    st, why = verdict({**good, "pf": 1.1, "max_dd_pct": 20.0}, {"pf": 0.9}, {"p_negative": 0.4}, CFG)
    assert st == "FAIL" and len(why) == 4 and any("PF 1.1" in w for w in why)
    assert verdict({"n": 12}, {}, {}, CFG)[0] == "INSUFFICIENT"


def _evidence(tmp_path, markets, synthetic=False, age_days=1):
    p = tmp_path / "market_evidence.json"
    gen = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    p.write_text(json.dumps({"generated_at": gen, "synthetic": synthetic, "markets": markets}))
    return p


def test_market_evidence_fails_closed(tmp_path):
    ok_row = {"status": "PASS", "reasons": []}
    assert status_for("EURUSD", tmp_path / "missing.json")[:2] == (False, "MISSING")
    p = _evidence(tmp_path, {"EURUSD": ok_row, "USDJPY": {"status": "FAIL", "reasons": ["PF 1.0 < 1.3"]}})
    assert status_for("EURUSD.a", p)[:2] == (True, "PASS")
    ok, st, why = status_for("USDJPY", p)
    assert not ok and st == "FAIL" and "PF 1.0" in why
    assert status_for("GBPUSD", p)[1] == "MISSING"
    assert status_for("EURUSD", _evidence(tmp_path, {"EURUSD": ok_row}, synthetic=True))[1] == "SYNTHETIC"
    assert status_for("EURUSD", _evidence(tmp_path, {"EURUSD": ok_row}, age_days=400))[1] == "STALE"


def test_gate_is_off_by_default_and_blocks_engine_entries_when_required(tmp_path):
    assert gate(CFG, "GBPUSD")[0]
    import test_mt5_engine as T
    eng, cfg, client = T.started_engine("DRY_RUN")
    assert eng.can_enter()[0]
    p = _evidence(tmp_path, {"EURUSD": {"status": "PASS"}})          # the engine trades XAUUSD: no evidence
    eng.cfg = replace(cfg, brain=replace(cfg.brain, require_market_evidence=True, market_evidence_file=str(p)))
    ok, why = eng.can_enter()
    assert not ok and "market evidence" in why and "XAUUSD" in why
    p2 = _evidence(tmp_path, {"XAUUSD": {"status": "PASS"}})
    eng.cfg = replace(eng.cfg, brain=replace(eng.cfg.brain, market_evidence_file=str(p2)))
    assert eng.can_enter()[0]


def test_selector_never_picks_a_market_without_pass_evidence(tmp_path):
    from brain.system import V5BrainSystem
    from brain.universe import MarketHealth
    p = _evidence(tmp_path, {"EURUSD": {"status": "PASS"}, "GBPUSD": {"status": "FAIL", "reasons": ["Sharpe 0.2 < 0.8"]}})
    cfg = replace(Config(), system=replace(Config().system, data_dir=str(tmp_path)),
                  brain=replace(Config().brain, require_market_evidence=True, market_evidence_file=str(p),
                                journal_file=str(tmp_path / "v5.jsonl")))
    system = V5BrainSystem(cfg, client=NS(symbol="XAUUSD"))
    rows = [MarketHealth("GBPUSD", "GBPUSD", "OK", 90, 0.8, 1, affordable=True, ranking_score=95),
            MarketHealth("EURUSD", "EURUSD", "OK", 80, 0.7, 1, affordable=True, ranking_score=80)]
    ok = system._apply_market_evidence(rows)
    assert ok == {"GBPUSD": False, "EURUSD": True}
    assert rows[0].evidence == "FAIL" and rows[0].selection_reasons[0].startswith("NO OUT-OF-SAMPLE EVIDENCE")


def test_discover_reads_per_market_folders_and_meta(tmp_path):
    for mk, sym in (("EURUSD", "EURUSDs"), ("XAUUSD", "XAUUSDs")):
        d = tmp_path / mk
        d.mkdir()
        (d / f"{sym}_M1.csv").write_text("time,open,high,low,close,tick_volume,spread\n")
        (d / "export_meta.json").write_text(json.dumps({"symbol": sym, "point": 0.00001 if mk == "EURUSD" else 0.01}))
    (tmp_path / "junk_M5.csv").write_text("x\n")
    found = discover(tmp_path, {"EURUSD", "XAUUSD", "USDJPY"})
    assert set(found) == {"EURUSD", "XAUUSD"} and found["EURUSD"][1]["point"] == 0.00001


def test_export_markets_writes_one_folder_per_market(tmp_path):
    from export_history import export_markets, resolve_market
    t0 = int(datetime(2026, 1, 5, tzinfo=timezone.utc).timestamp())

    def rates(symbol, tf, fr, to):
        n = 50 if fr < datetime.now(timezone.utc) - timedelta(days=20) else 0
        arr = np.zeros(n, dtype=[("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8"),
                                 ("tick_volume", "u8"), ("spread", "i4")])
        arr["time"] = t0 + 60 * np.arange(n) + int(fr.timestamp()) % 1000 * 60
        arr["open"] = arr["high"] = arr["low"] = arr["close"] = 1.08
        arr["spread"] = 8
        return arr
    infos = {"EURUSDs": NS(point=0.00001, digits=5, trade_contract_size=100000.0, currency_profit="USD",
                           volume_min=0.01, volume_step=0.01)}
    mt5 = NS(TIMEFRAME_M1=1, TIMEFRAME_M5=5, symbol_info=lambda n: infos.get(n),
             symbols_get=lambda: [NS(name="EURUSDs")], symbol_select=lambda n, e=True: True,
             symbol_info_tick=lambda n: NS(time=int(datetime.now(timezone.utc).timestamp())), copy_rates_range=rates)
    assert resolve_market(mt5, "EURUSD") == "EURUSDs" and resolve_market(mt5, "USDJPY") is None
    out = export_markets(mt5, ["EURUSD", "USDJPY"], 0.1, tmp_path)
    assert "not offered" in out["USDJPY"]
    meta = json.loads((tmp_path / "EURUSD" / "export_meta.json").read_text())
    assert meta["symbol"] == "EURUSDs" and meta["median_spread_points"] == 8.0 and meta["currency_profit"] == "USD"
    assert spec_for("EURUSD", meta).source == "broker_export"


def test_synthetic_results_can_never_be_published(tmp_path, monkeypatch):
    import sys

    import market_research
    monkeypatch.setattr(sys, "argv", ["market_research.py", "--synthetic", "300", "--markets", "EURUSD",
                                      "--folds", "2", "--out", str(tmp_path), "--publish"])
    monkeypatch.setattr(market_research, "research_market",
                        lambda *a, **k: ({"status": "PASS", "reasons": [], "metrics": {"n": 0}}, pd.DataFrame()))
    with pytest.raises(SystemExit, match="refusing to publish synthetic"):
        market_research.main()
    rep = json.loads((tmp_path / "market_research.json").read_text())
    assert rep["synthetic"] is True
    assert "SYNTHETIC DATA" in (tmp_path / "MARKET_RESEARCH_REPORT.md").read_text()
    assert not Path(Config().brain.market_evidence_file).exists()


def test_research_market_runs_end_to_end_on_fx_scale_data():
    from market_research import research_market, synthetic_market
    sim = synthetic_market("EURUSD", 9000, seed=5)
    row, oos = research_market("EURUSD", sim, 5, spec_for("EURUSD"), CFG, folds=3)
    assert row["status"] in ("PASS", "FAIL", "INSUFFICIENT") and row["bars"] > 0
    assert {"metrics", "stress", "monte_carlo", "walk_forward"} <= set(row)
    assert any("DEFAULT estimates" in r for r in row["reasons"])
