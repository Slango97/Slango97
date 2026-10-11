"""V5.10: enforced deployment ladder DRY_RUN -> DEMO -> LIVE_PILOT -> LIVE_FULL."""
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from helpers import FakeClient, tmpdir  # noqa: F401  (sets sys.path)
import rollout
from config import Config


def _cfg(tmp_path, mode="DRY_RUN"):
    c = Config()
    return replace(c, system=replace(c.system, data_dir=str(tmp_path), mode=mode),
                   rollout=replace(c.rollout, state_file=str(tmp_path / "rollout.json")),
                   brain=replace(c.brain, market_evidence_file=str(tmp_path / "none.json")))


def _iso(days_ago, minutes=0):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago, minutes=minutes)).isoformat(timespec="seconds")


def _write(path, rows):
    with open(path, "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _stage(cfg, stage, days_ago, account=None):
    rollout.save(cfg, {"stage": stage, "since": _iso(days_ago), "account": account, "history": []})


def _trades(tmp_path, n, pattern=(2.0, -1.0), start_days=25, slip=0.02):
    rows = [{"t": _iso(start_days - 0.5 - i * (start_days - 1) / max(n, 1)), "type": "close", "r_multiple": pattern[i % len(pattern)],
             "net_pnl": 10 * pattern[i % len(pattern)], "slippage_r": slip} for i in range(n)]
    _write(tmp_path / "trades.jsonl", rows)


def test_first_use_starts_at_dry_run_since_the_first_journal_entry(tmp_path):
    cfg = _cfg(tmp_path)
    _write(tmp_path / "events.jsonl", [{"t": _iso(12), "kind": "heartbeat"}])
    st = rollout.ensure(cfg)
    assert st["stage"] == "DRY_RUN" and rollout._parse(st["since"]) < datetime.now(timezone.utc) - timedelta(days=11)
    assert rollout.stage(cfg) == "DRY_RUN"


def test_dry_run_to_demo_needs_time_signals_and_no_incidents(tmp_path):
    cfg = _cfg(tmp_path)
    _stage(cfg, "DRY_RUN", 3)
    ok, msg = rollout.promote(cfg, "DEMO", "Salim")
    assert not ok and "days in DRY_RUN" in msg and "simulated entries" in msg
    _stage(cfg, "DRY_RUN", 12)
    _write(tmp_path / "events.jsonl", [{"t": _iso(5), "kind": "dry_run_trade"}] * 25)
    _write(tmp_path / "events.jsonl", [{"t": _iso(2), "kind": "lifecycle_engine", "new": "HALTED", "reason": "3 consecutive loop errors"}])
    ok, msg = rollout.promote(cfg, "DEMO", "Salim")
    assert not ok and "incidents" in msg
    _write(tmp_path / "events.jsonl", [])
    (tmp_path / "events.jsonl").write_text("\n".join(json.dumps({"t": _iso(5), "kind": "dry_run_trade"}) for _ in range(25))
                                            + "\n" + json.dumps({"t": _iso(1), "kind": "lifecycle_engine", "new": "HALTED",
                                                                 "reason": "STOP file present"}) + "\n")
    ok, msg = rollout.promote(cfg, "DEMO", "Salim")                      # an operator STOP is not an incident
    assert ok and rollout.stage(cfg) == "DEMO"
    h = rollout.load(cfg)["history"][-1]
    assert h["by"] == "Salim" and h["from"] == "DRY_RUN" and h["hash"] and h["evidence"]["signals"] == 25


def test_rungs_cannot_be_skipped(tmp_path):
    cfg = _cfg(tmp_path)
    _stage(cfg, "DRY_RUN", 30)
    ok, msg = rollout.promote(cfg, "LIVE_PILOT", "Salim", login="1")
    assert not ok and "next rung only" in msg


def test_demo_to_live_pilot_judges_demo_trading_and_needs_the_real_account(tmp_path):
    cfg = _cfg(tmp_path)
    _stage(cfg, "DEMO", 25)
    _trades(tmp_path, 20)
    ok, msg = rollout.promote(cfg, "LIVE_PILOT", "Salim", login="777")
    assert not ok and "closed trades = 20" in msg
    _trades(tmp_path, 30, pattern=(-1.0, -1.0, 1.5))                    # 50 trades but now losing overall
    ok, msg = rollout.promote(cfg, "LIVE_PILOT", "Salim", login="777")
    assert not ok and "profit factor" in msg
    (tmp_path / "trades.jsonl").unlink()
    _trades(tmp_path, 50, slip=0.3)
    ok, msg = rollout.promote(cfg, "LIVE_PILOT", "Salim", login="777")
    assert not ok and "slippage" in msg
    (tmp_path / "trades.jsonl").unlink()
    _trades(tmp_path, 50)
    assert not rollout.promote(cfg, "LIVE_PILOT", "Salim")[0]          # no --login
    ok, msg = rollout.promote(cfg, "LIVE_PILOT", "Salim", login="777", server="Broker-Live")
    assert ok, msg
    st = rollout.load(cfg)
    assert st["stage"] == "LIVE_PILOT" and st["account"] == {"login": "777", "server": "Broker-Live"}
    assert st["history"][-1]["evidence"]["metrics"]["pf"] == 2.0


def test_live_mode_only_on_the_promoted_account(tmp_path):
    cfg = _cfg(tmp_path, "LIVE")
    _stage(cfg, "DEMO", 5)
    ok, why = rollout.live_permission(cfg, 777, "Broker-Live")
    assert not ok and "LIVE_PILOT promotion" in why
    _stage(cfg, "LIVE_PILOT", 1, {"login": "777", "server": "Broker-Live"})
    assert rollout.live_permission(cfg, 777, "Broker-Live")[0]
    ok, why = rollout.live_permission(cfg, 888, "Broker-Live")
    assert not ok and "not the promoted live account" in why
    assert rollout.live_permission(replace(cfg, rollout=replace(cfg.rollout, enforce_live=False)), 888)[0]
    assert rollout.live_permission(_cfg(tmp_path, "DEMO"), 1)[0]


def test_pilot_caps_risk_and_open_trades(tmp_path):
    from journal import Journal
    from risk import RiskEngine
    from state import State
    from test_risk_state_news import dec
    cfg = _cfg(tmp_path, "LIVE")
    _stage(cfg, "LIVE_PILOT", 1, {"login": "1", "server": ""})
    assert rollout.limits(cfg) == (cfg.rollout.pilot_risk_pct, cfg.rollout.pilot_max_open_trades)
    c = FakeClient(10000.0, [])
    r = RiskEngine(cfg, c, State(os.path.join(tmp_path, "s.json")), Journal(str(tmp_path)))
    r.refresh()
    a = r.approve(dec(risk_pct=0.5), 2000.0, [])
    assert a.ok and a.details["rollout_risk_cap_pct"] == 0.25 and a.lots == 0.04      # 0.25% instead of 0.5%
    ok, why = r.gate([object()])
    assert not ok and "LIVE_PILOT" in why
    assert rollout.limits(_cfg(tmp_path, "DEMO")) == (None, None)


def test_live_review_demotes_full_and_halts_a_failing_pilot(tmp_path):
    cfg = _cfg(tmp_path, "LIVE")
    _stage(cfg, "LIVE_FULL", 30, {"login": "1", "server": ""})
    _trades(tmp_path, 25, pattern=(2.0, -1.0))
    assert rollout.review(cfg) is None
    _trades(tmp_path, 25, pattern=(-1.0, -1.0, -1.0, 0.5), start_days=10)
    kind, msg = rollout.review(cfg)
    assert kind == "demote" and rollout.stage(cfg) == "LIVE_PILOT" and "PF" in msg
    _stage(cfg, "LIVE_PILOT", 30, {"login": "1", "server": ""})
    kind, msg = rollout.review(cfg)
    assert kind == "halt" and "LIVE_PILOT failed" in msg
    assert rollout.review(_cfg(tmp_path, "DEMO")) is None


def test_demotion_goes_down_only_and_is_recorded(tmp_path):
    cfg = _cfg(tmp_path)
    _stage(cfg, "DEMO", 3)
    assert not rollout.demote(cfg, "LIVE_FULL", "x")[0]
    ok, _ = rollout.demote(cfg, "DRY_RUN", "operator review", by="operator")
    assert ok and rollout.load(cfg)["history"][-1]["reason"] == "operator review"


def test_engine_refuses_live_without_promotion(tmp_path):
    import test_mt5_engine as T
    from engine import Lifecycle
    os.environ["CONFIRM_LIVE"] = "YES_I_UNDERSTAND"
    try:
        base = T.make_cfg("DEMO")
        live = replace(base, system=replace(base.system, mode="LIVE"),
                       rollout=replace(base.rollout, state_file=str(tmp_path / "r.json")))
        T.world.account_mode = 2                                               # a REAL account
        client = T.make_client(live)
        T.rates_world()
        eng = T.AgentEngine(live, client, news=T.StubNews(), alerts=T.RecAlerts(), journal=T.Journal(live.system.data_dir),
                            state=T.State(live.system.state_file))
        eng.startup()
        assert eng.lifecycle == Lifecycle.HALTED and "rollout: LIVE needs a recorded LIVE_PILOT" in eng.halt_reason
    finally:
        os.environ.pop("CONFIRM_LIVE", None)
        T.world.account_mode = 0


@pytest.mark.parametrize("cmd", [["status"]])
def test_cli_status_prints_the_checklist(tmp_path, monkeypatch, capsys, cmd):
    import sys
    monkeypatch.setenv("ROLLOUT__STATE_FILE", str(tmp_path / "r.json"))
    monkeypatch.setenv("SYSTEM__DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["rollout.py"] + cmd)
    rollout.main()
    out = capsys.readouterr().out
    assert "Stage: DRY_RUN" in out and "Checklist for DEMO" in out and "[ ] days in DRY_RUN" in out
