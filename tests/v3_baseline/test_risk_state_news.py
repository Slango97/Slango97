from helpers import Config, FakeClient, NS, pos, tmpdir, utc  # noqa: F401

import json
import os
import time

from decision import Decision
from journal import Journal
from reconciliation import make_comment, reconcile
from risk import RiskCore, RiskEngine
from state import SCHEMA_VERSION, State

CFG = Config()


def engine_for(client, d=None):
    d = d or tmpdir()
    st = State(os.path.join(d, "state.json"))
    r = RiskEngine(CFG, client, st, Journal(d))
    r.refresh()
    return r, st


def dec(sl=1994.0, risk_pct=0.5, d=1):
    return Decision(action="enter", direction=d, style="day", entry=2000.0, sl=sl, tp=2012.0, score=75, risk_pct=risk_pct)


# ---------------- risk ----------------
def test_lot_sizing_rounds_down_and_rejects_oversized_minimum():
    core = RiskCore(CFG)
    assert core.lots_for(50.0, 1000.0, 0.01, 0.01, 50) == 0.05
    assert core.lots_for(50.0, 1999.0, 0.01, 0.01, 50) == 0.02              # rounds DOWN, never up
    assert core.lots_for(5.0, 1000.0, 0.01, 0.01, 50) == 0.0                # min lot would risk 2x budget
    assert core.lots_for(10.0, 1000.0, 0.01, 0.01, 50) == 0.01              # 1.0x budget: allowed


def test_gate_blocks_each_limit():
    core = RiskCore(CFG)
    ok = dict(now=utc(10), spread_points=25, n_open=0, trades_today=0, day_loss_pct=0, week_loss_pct=0, dd_pct=0, open_risk_pct=0)
    assert core.gate(**ok)[0]
    for change, text in [({"now": utc(10, d=8)}, "weekend"), ({"now": utc(22)}, "rollover"), ({"spread_points": 90}, "spread"),
                         ({"n_open": 3}, "max open"), ({"trades_today": 6}, "max trades"), ({"day_loss_pct": 2.1}, "daily"),
                         ({"week_loss_pct": 4.1}, "weekly"), ({"dd_pct": 8.5}, "drawdown"), ({"open_risk_pct": 1.6}, "open risk"),
                         ({"unprotected": 1}, "without stop"), ({"now": utc(19, d=7)}, "late Friday"),
                         ({"paused_until": utc(23)}, "paused")]:
        okx, why = core.gate(**{**ok, **change})
        assert not okx and text in why, (change, why)


def test_projected_open_risk_is_checked_before_approval():
    existing = pos(1, entry=2000.0, sl=1988.0, vol=0.10)                    # 0.10 lots * $12 * 100 = $120 = 1.2% of 10k
    r, _ = engine_for(FakeClient(10000.0, [existing]))
    assert abs(r.open_risk_pct([existing]) - 1.2) < 1e-6
    a = r.approve(dec(sl=1994.0, risk_pct=0.5), 2000.0, [existing])          # +0.5% => 1.7% > 1.5% cap
    assert not a.ok and "projected" in a.reason and a.details["projected_risk_pct"] > 1.5
    r2, _ = engine_for(FakeClient(10000.0, []))
    a2 = r2.approve(dec(sl=1994.0, risk_pct=0.5), 2000.0, [])
    assert a2.ok and a2.lots == 0.08 and abs(a2.risk_pct - 0.48) < 1e-6


def test_positions_without_stop_loss_block_trading_and_count_as_unprotected():
    naked = pos(1, sl=0.0)
    c = FakeClient(10000.0, [naked])
    r, _ = engine_for(c)
    assert r.unprotected([naked]) == [naked]
    ok, why = r.gate([naked])
    assert not ok and "without stop loss" in why


def test_breakeven_position_no_longer_counts_as_open_risk():
    be = pos(1, entry=2000.0, sl=2000.5, vol=0.5)
    r, _ = engine_for(FakeClient(10000.0, [be]))
    assert r.open_risk_pct([be]) == 0.0


def test_margin_check_rejects_thin_free_margin():
    c = FakeClient(10000.0, [])
    c.margin, c.margin_free_override = 3000.0, 3500.0
    r, _ = engine_for(c)
    a = r.approve(dec(), 2000.0, [])
    assert not a.ok and "margin" in a.reason


def test_loss_streak_reduces_risk_then_pauses_and_resets_on_win():
    r, st = engine_for(FakeClient(10000.0, []))
    for _ in range(3):
        r.record_result(-50)
    a = r.approve(dec(risk_pct=0.5), 2000.0, [])
    assert abs(a.details["streak_mult"] - 0.5) < 1e-9 and a.lots == 0.04
    r.record_result(-50)
    r.record_result(-50)
    assert st.risk.get("paused_until")
    ok, why = r.gate([])
    assert not ok and "paused" in why
    r.record_result(+80)
    assert st.risk["loss_streak"] == 0


def test_daily_loss_limit_uses_equity():
    c = FakeClient(10000.0, [])
    r, _ = engine_for(c)
    c.equity = 9790.0
    ok, why = r.gate([])
    assert not ok and "daily loss" in why


# ---------------- state ----------------
def test_state_roundtrip_and_backup_recovery_flags_reconciliation():
    d = tmpdir()
    p = os.path.join(d, "state.json")
    s = State(p)
    s.trades["7"] = {"style": "day", "direction": 1, "entry": 2000.0, "risk": 6.0}
    for _ in range(12):
        s.save()
    assert State(p).trades["7"]["entry"] == 2000.0 and not State(p).needs_recovery
    with open(p, "w") as f:
        f.write("{ this is not json")
    s2 = State(p)
    assert s2.needs_recovery and "7" in s2.trades and any("recovered from backup" in n for n in s2.notes)
    assert [f for f in os.listdir(d) if "corrupt" in f]                      # evidence is preserved, not overwritten


def test_state_without_backup_starts_empty_but_demands_recovery():
    d = tmpdir()
    p = os.path.join(d, "state.json")
    with open(p, "w") as f:
        f.write("garbage")
    s = State(p)
    assert s.needs_recovery and s.trades == {}
    s.acknowledge()
    assert not s.needs_recovery


def test_state_migrates_old_schema_and_rejects_newer():
    d = tmpdir()
    p = os.path.join(d, "state.json")
    with open(p, "w") as f:
        json.dump({"risk": {}, "trades": {"1": {"style": "day"}}}, f)
    s = State(p)
    assert s.data["schema_version"] == SCHEMA_VERSION and s.trades["1"]["lifecycle"] == "OPEN" and not s.needs_recovery
    with open(p, "w") as f:
        json.dump({"schema_version": 99, "risk": {}, "trades": {}}, f)
    assert State(p).needs_recovery


# ---------------- reconciliation ----------------
def recon(client, state, d):
    risk = RiskEngine(CFG, client, state, Journal(d))
    risk.refresh()
    return reconcile(CFG, client, state, Journal(d), risk, {}), risk


def test_restart_recovery_rebuilds_state_from_order_comment():
    d = tmpdir()
    p = pos(55, d=1, entry=2000.0, sl=2000.5, price=2010.0, comment=make_comment("da", 74, 6.0))
    st = State(os.path.join(d, "state.json"))
    rep, _ = recon(FakeClient(10000.0, [p]), st, d)
    rec = st.trades["55"]
    assert rep.created == [55] and rec["style"] == "day" and abs(rec["risk"] - 6.0) < 1e-9
    assert rec["reconstructed"] and rec["be"] is True and abs(rec["sl0"] - 1994.0) < 1e-9


def test_closed_trade_is_accounted_from_deal_history():
    d = tmpdir()
    st = State(os.path.join(d, "state.json"))
    st.trades["9"] = {"style": "day", "direction": 1, "entry": 2000.0, "risk": 6.0, "risk_money": 60.0,
                      "setup": "FVG_RETEST", "score": 72, "opened_ts": time.time() - 3600}
    c = FakeClient(10000.0, [])
    c.summaries[9] = {"net": -61.0, "exit_price": 1994.0, "volume": 0.1, "entry_price": 2000.0,
                      "exit_ts": time.time(), "reason": "sl", "commission": -1.0, "swap": 0.0}
    rep, risk = recon(c, st, d)
    assert rep.closed == [(9, -61.0)] and "9" not in st.trades and st.risk["loss_streak"] == 1
    rows = Journal(d).read_jsonl(Journal(d).trades_path)
    assert rows and rows[-1]["type"] == "close" and rows[-1]["r_multiple"] < -1.0


def test_missing_stop_is_restored_or_escalated_to_critical():
    d = tmpdir()
    st = State(os.path.join(d, "state.json"))
    st.trades["3"] = {"style": "day", "direction": 1, "entry": 2000.0, "risk": 6.0, "sl0": 1994.0, "last_sl": 1994.0, "tp": 2012.0}
    naked = pos(3, d=1, entry=2000.0, sl=0.0, price=2003.0)
    c = FakeClient(10000.0, [naked])
    rep, _ = recon(c, st, d)
    assert c.mods == [1994.0] and any("restored" in f for f in rep.fixed) and not rep.critical
    d2 = tmpdir()
    st2 = State(os.path.join(d2, "state.json"))
    st2.trades["4"] = {"style": "day", "direction": 1, "entry": 2000.0, "risk": 6.0, "sl0": 2010.0, "last_sl": None, "tp": 2020.0}
    c2 = FakeClient(10000.0, [pos(4, d=1, entry=2000.0, sl=0.0, price=2003.0)])
    rep2, _ = recon(c2, st2, d2)
    assert rep2.critical and "NO STOP LOSS" in rep2.critical[0]


def test_manual_sl_change_is_adopted_and_reported():
    d = tmpdir()
    st = State(os.path.join(d, "state.json"))
    st.trades["5"] = {"style": "day", "direction": 1, "entry": 2000.0, "risk": 6.0, "sl0": 1994.0, "last_sl": 1994.0, "tp": 2012.0}
    c = FakeClient(10000.0, [pos(5, d=1, entry=2000.0, sl=2001.0, price=2004.0)])
    rep, _ = recon(c, st, d)
    assert any("manual change" in i for i in rep.issues) and st.trades["5"]["be"] is True and st.trades["5"]["last_sl"] == 2001.0


