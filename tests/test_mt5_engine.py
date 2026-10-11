from helpers import Config, TUE_10, long_setup_views, tmpdir  # noqa: F401

import os
from dataclasses import replace

import fake_mt5

world = fake_mt5.install()

from analysis import MarketState, build_state  # noqa: E402
from backtest import build_frames, synthetic_m5  # noqa: E402
from decision import decide  # noqa: E402
from engine import AgentEngine, Lifecycle  # noqa: E402
from execution import ExecutionEngine  # noqa: E402
from journal import Journal  # noqa: E402
from mt5_client import MT5Client  # noqa: E402
from news import NewsContext  # noqa: E402
from risk import RiskEngine  # noqa: E402
from state import State  # noqa: E402
from trade_manager import TradeManager  # noqa: E402


class RecAlerts:
    def __init__(self):
        self.msgs = []

    def send(self, level, text, key=None):
        self.msgs.append((level, text))

    def flush(self, timeout=0):
        pass

    def levels(self):
        return [m[0] for m in self.msgs]


class StubNews:
    last_error = ""

    def __init__(self, ctx=None):
        self.ctx = ctx or NewsContext(status="NO_DATA")

    def get(self, vol_probe=None):
        return self.ctx

    def refresh(self):
        pass


def make_cfg(mode="DEMO", **flags):
    c = Config()
    d = tmpdir()
    return replace(c, system=replace(c.system, mode=mode, data_dir=d, state_file=os.path.join(d, "state.json")),
                   emergency=replace(c.emergency, flatten_file=os.path.join(d, "FLATTEN"), stop_file=os.path.join(d, "STOP"),
                                     **flags))


def make_client(cfg):
    world.reset()
    c = MT5Client(cfg)
    c.connect()
    c.utc_now = lambda: TUE_10
    return c


def stack(mode="DEMO"):
    cfg = make_cfg(mode)
    client = make_client(cfg)
    j, st, al = Journal(cfg.system.data_dir), State(cfg.system.state_file), RecAlerts()
    risk = RiskEngine(cfg, client, st, j)
    risk.refresh()
    mgr = TradeManager(cfg, client, st, j, al)
    return cfg, client, j, st, al, risk, ExecutionEngine(cfg, client, risk, mgr, j, al)


def setup_decision(client, cfg):
    views = long_setup_views(2000.0, 3.0)
    t = client.tick()
    ms = build_state(views, TUE_10, cfg.sessions.bounds, t.ask - t.bid)
    dec = decide(ms, (t.ask + t.bid) / 2, NewsContext(status="NO_DATA"), cfg)
    assert dec.is_entry, dec.reasons
    return ms, dec


# ---------------- client ----------------
def test_symbol_autodetects_broker_suffix_and_pins_selected_symbol():
    cfg = make_cfg()
    cfg = replace(cfg, system=replace(cfg.system, symbol="AUTO", symbol_autodetect=True))
    world.reset()
    world.known = {"XAUUSDs"}
    c = MT5Client(cfg)
    c.connect()
    assert c.symbol == "XAUUSDs" and c.symbol_auto_detected and c.symbol_resolution["method"] == "semantic_inventory"


def test_validate_broker_flags_disabled_algo_trading():
    cfg = make_cfg()
    c = make_client(cfg)
    assert c.validate_broker()[0] == []
    world.algo = False
    assert any("Algo Trading" in e for e in c.validate_broker()[0])


def test_broker_side_loss_calculation_is_used():
    c = make_client(make_cfg())
    assert abs(c.calc_loss_per_lot(1, 2000.0, 1990.0) - 1000.0) < 1e-9
    assert abs(c.calc_loss_per_lot(-1, 2000.0, 2010.0) - 1000.0) < 1e-9


def test_cross_symbol_loss_calculation_fails_closed_when_broker_returns_none():
    from mt5_client import BrokerDataError
    c = make_client(make_cfg())
    original = fake_mt5.order_calc_profit
    fake_mt5.world.calc_profit_ok = False
    try:
        try:
            c.calc_loss_per_lot_for("EURUSD", 1, 1.1000, 1.0900)
        except BrokerDataError:
            pass
        else:
            raise AssertionError("missing broker loss calculation must fail closed")
    finally:
        fake_mt5.world.calc_profit_ok = True


def test_cross_symbol_loss_calculation_rejects_stop_on_profit_side():
    from mt5_client import BrokerDataError
    c = make_client(make_cfg())
    try:
        c.calc_loss_per_lot_for("EURUSD", 1, 1.1000, 1.1100)
    except BrokerDataError:
        pass
    else:
        raise AssertionError("buy stop on profit side must be rejected")


def test_send_market_verifies_fill_and_measures_slippage():
    c = make_client(make_cfg())
    world.slip = 0.10
    r = c.send_market(1, 0.10, 1994.0, 2012.0, "da74 r6.25")
    assert r.ok and len(world.positions) == 1 and abs(r.slippage - 0.10) < 1e-9
    assert r.position.sl == 1994.0 and r.fill_price > r.requested_price


def test_requote_is_retried_once_without_duplicating_the_trade():
    c = make_client(make_cfg())
    world.script = [10004]
    r = c.send_market(1, 0.10, 1994.0, 2012.0, "x")
    assert r.ok and r.attempts == 2 and len(world.positions) == 1 and len(world.calls) == 2


def test_retries_are_bounded():
    c = make_client(make_cfg())
    world.script = [10004] * 10
    r = c.send_market(1, 0.10, 1994.0, 2012.0, "x")
    assert not r.ok and r.attempts == c.cfg.execution.max_retries + 1 and not world.positions


def test_timeout_after_execution_finds_the_position_and_never_resends():
    c = make_client(make_cfg())
    world.script = ["executed_no_reply"]
    r = c.send_market(1, 0.10, 1994.0, 2012.0, "da74 r6.25")
    assert r.ok and len(world.positions) == 1 and len(world.calls) == 1


def test_timeout_without_execution_is_ambiguous_and_not_retried():
    c = make_client(make_cfg())
    world.script = ["none"]
    r = c.send_market(1, 0.10, 1994.0, 2012.0, "x")
    assert not r.ok and r.ambiguous and len(world.calls) == 1 and not world.positions


def test_hard_reject_is_not_retried():
    c = make_client(make_cfg())
    world.script = [10016]
    r = c.send_market(1, 0.10, 1994.0, 2012.0, "x")
    assert not r.ok and r.attempts == 1 and r.retcode == 10016


def test_stale_tick_blocks_orders():
    c = make_client(make_cfg())
    world.tick_time_offset = -300
    r = c.send_market(1, 0.10, 1994.0, 2012.0, "x")
    assert not r.ok and "stale" in r.comment and not world.calls


def test_dry_run_client_is_read_only():
    c = make_client(make_cfg("DRY_RUN"))
    for call in (lambda: c.send_market(1, 0.1, 1994.0, 2012.0, "x"), lambda: c.close_all_agent_positions()):
        try:
            call()
        except PermissionError:
            continue
        raise AssertionError("read-only client executed a write")


def test_modify_close_partial_and_flatten_with_deal_history():
    c = make_client(make_cfg())
    r = c.send_market(1, 0.10, 1994.0, 2012.0, "da74 r6.25")
    p = r.position
    assert c.modify_sltp(p, sl=2000.5) and world.positions[0].sl == 2000.5
    world.tick_for_test(2010.0)
    assert c.close(p, 0.04) and abs(world.positions[0].volume - 0.06) < 1e-9
    closed, failed = c.close_all_agent_positions()
    assert closed == [p.ticket] and not failed and not world.positions
    s = c.deal_summary(p.ticket)
    assert s and s["volume"] == 0.10 and s["net"] > 0 and s["commission"] < 0


# ---------------- execution engine ----------------
def test_execute_places_protected_trade_and_registers_actual_fill_data():
    cfg, client, j, st, al, risk, ex = stack()
    ms, dec = setup_decision(client, cfg)
    out = ex.execute(dec, ms, [])
    assert out.executed and len(world.positions) == 1
    p = world.positions[0]
    rec = st.trades[str(p.ticket)]
    assert p.sl and p.tp and rec["lifecycle"] == "POSITION_OPEN" and abs(rec["risk"] - (p.price_open - p.sl)) < 1e-9
    assert p.comment.startswith(cfg.strategy.styles[dec.style].code) and st.risk["trades_today"] == 1
    kinds = [r["state"] for r in j.read_jsonl(j.events_path) if r["kind"] == "lifecycle"]
    assert kinds[:3] == ["SIGNAL", "ORDER_SENT", "ORDER_FILLED"]


def test_dry_run_execution_sends_nothing():
    cfg, client, j, st, al, risk, ex = stack("DRY_RUN")
    ms, dec = setup_decision(client, cfg)
    out = ex.execute(dec, ms, [])
    assert out.executed and out.dry_run and not world.calls and not world.positions


def test_drifted_price_aborts_entry():
    cfg, client, j, st, al, risk, ex = stack()
    ms, dec = setup_decision(client, cfg)
    world.tick_for_test(2006.0)
    out = ex.execute(dec, ms, [])
    assert not out.executed and ("drift" in out.reason or "beyond" in out.reason) and not world.positions


def test_position_opened_without_stop_gets_one_or_is_closed():
    cfg, client, j, st, al, risk, ex = stack()
    ms, dec = setup_decision(client, cfg)
    world.strip_sl = True
    out = ex.execute(dec, ms, [])
    assert out.executed and world.positions[0].sl                      # broker dropped it; agent re-applied it
    cfg, client, j, st, al, risk, ex = stack()
    ms, dec = setup_decision(client, cfg)
    world.strip_sl, world.reject_modify = True, True
    out = ex.execute(dec, ms, [])
    assert not out.executed and not world.positions and "CRITICAL" in al.levels()


def test_ambiguous_order_raises_critical_alert():
    cfg, client, j, st, al, risk, ex = stack()
    ms, dec = setup_decision(client, cfg)
    world.script = ["none"]
    out = ex.execute(dec, ms, [])
    assert not out.executed and "CRITICAL" in al.levels() and "UNKNOWN" in al.msgs[-1][1]


# ---------------- engine lifecycle ----------------
def rates_world():
    frames = build_frames(synthetic_m5(75000, seed=3, start="2023-01-02"))
    world.rates = {tf: df for tf, (df, _) in frames.items()}
    world.rates["M1"] = world.rates["M5"]          # stand-in 1-minute series for the microstructure monitor


def started_engine(mode="DEMO", news=None, **flags):
    cfg = make_cfg(mode, **flags)
    client = make_client(cfg)
    rates_world()
    eng = AgentEngine(cfg, client, news=news or StubNews(), alerts=RecAlerts(), journal=Journal(cfg.system.data_dir),
                      state=State(cfg.system.state_file))
    eng.startup()
    return eng, cfg, client


def test_startup_reaches_running_when_everything_checks_out():
    eng, cfg, client = started_engine("DRY_RUN")
    assert eng.lifecycle == Lifecycle.RUNNING and len(eng.views) == 5 and "W1" not in eng.views   # short W1 history tolerated


def test_startup_halts_on_broker_problem_and_on_account_mode_mismatch():
    cfg = make_cfg("DEMO")
    client = make_client(cfg)
    world.algo = False
    rates_world()
    eng = AgentEngine(cfg, client, news=StubNews(), alerts=RecAlerts(), journal=Journal(cfg.system.data_dir), state=State(cfg.system.state_file))
    eng.startup()
    assert eng.lifecycle == Lifecycle.HALTED and "Algo Trading" in eng.halt_reason
    os.environ["CONFIRM_LIVE"] = "YES_I_UNDERSTAND"
    try:
        live = replace(make_cfg("DEMO"), system=replace(make_cfg("DEMO").system, mode="LIVE"))
        client = make_client(live)
        rates_world()
        eng = AgentEngine(live, client, news=StubNews(), alerts=RecAlerts(), journal=Journal(live.system.data_dir), state=State(live.system.state_file))
        eng.startup()
        assert eng.lifecycle == Lifecycle.HALTED and "LIVE but the connected account is DEMO" in eng.halt_reason
    finally:
        os.environ.pop("CONFIRM_LIVE", None)


def open_position_via_engine(eng, cfg, client):
    ms, dec = setup_decision(client, cfg)
    out = eng.execution.execute(dec, ms, client.positions())
    assert out.executed
    eng.last_bar = client.last_bar_time("M5")                       # suppress the new-candle path for this step
    return world.positions[0], dec


def test_engine_moves_stop_to_breakeven_through_the_full_stack():
    eng, cfg, client = started_engine("DEMO")
    p, dec = open_position_via_engine(eng, cfg, client)
    entry, risk = p.price_open, p.price_open - p.sl
    eng.views["M15"]["close"] = entry + 0.8 * risk
    tp1 = eng.state.trades[str(p.ticket)].get("tp1")                 # V5.1 documents: break-even comes with TP1
    world.tick_for_test(max(entry + 1.2 * risk, (tp1 or 0) + 0.2))
    eng.fast_step()
    assert world.positions[0].sl >= entry                            # at or beyond break-even
    assert eng.state.trades[str(p.ticket)]["be"] is True
    states = [r["state"] for r in eng.journal.read_jsonl(eng.journal.events_path) if r["kind"] == "lifecycle"]
    assert "BE" in states


def test_closed_position_is_reconciled_and_journaled():
    eng, cfg, client = started_engine("DEMO")
    p, dec = open_position_via_engine(eng, cfg, client)
    world.tick_for_test(p.sl - 0.5)
    world.script = []
    client.close(world.positions[0])                                  # simulate the stop being hit
    eng._t_recon = 0
    eng.fast_step()
    assert not eng.state.trades
    closes = [r for r in eng.journal.read_jsonl(eng.journal.trades_path) if r["type"] == "close"]
    assert closes and closes[0]["net_pnl"] < 0 and eng.state.risk["loss_streak"] == 1


def test_flatten_file_closes_everything_and_halts():
    eng, cfg, client = started_engine("DEMO")
    open_position_via_engine(eng, cfg, client)
    open(cfg.emergency.flatten_file, "w").close()
    eng.fast_step()
    assert not world.positions and eng.lifecycle == Lifecycle.HALTED and "flattened" in eng.halt_reason
    assert "CRITICAL" in eng.alerts.levels()


def test_stop_file_blocks_entries_but_positions_stay_managed():
    eng, cfg, client = started_engine("DEMO")
    p, dec = open_position_via_engine(eng, cfg, client)
    open(cfg.emergency.stop_file, "w").close()
    eng.fast_step()
    assert eng.lifecycle == Lifecycle.HALTED and len(world.positions) == 1
    assert eng.can_enter()[0] is False
    os.remove(cfg.emergency.stop_file)
    eng.fast_step()
    # Removing STOP alone must not clear the durable safety latch.
    assert eng.lifecycle == Lifecycle.HALTED
    assert eng.safety_latch.exists()


def test_connection_loss_degrades_then_recovers():
    eng, cfg, client = started_engine("DRY_RUN")
    world.connected, world.init_ok = False, False
    eng.fast_step()
    assert eng.lifecycle == Lifecycle.DEGRADED and not eng.can_enter()[0]
    world.connected, world.init_ok = True, True
    eng.fast_step()
    assert eng.lifecycle == Lifecycle.RUNNING


def test_repeated_loop_errors_halt_the_engine():
    eng, cfg, client = started_engine("DRY_RUN")
    eng.fast_step = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    for _ in range(cfg.emergency.max_consecutive_errors):
        eng.step()
    assert eng.lifecycle == Lifecycle.HALTED and "consecutive loop errors" in eng.halt_reason


def test_blackout_and_risk_gates_prevent_new_candle_decisions():
    eng, cfg, client = started_engine("DRY_RUN", news=StubNews(NewsContext(status="OK", blackout=True, blackout_reason="CPI",
                                                                           event_phase="PRE")))
    eng.news_ctx = eng.news.get()
    dec = eng.candle_step()
    assert dec is not None and dec.action == "HALT" and not world.calls
    eng2, cfg2, client2 = started_engine("DRY_RUN")
    eng2.news_ctx = NewsContext(status="NO_DATA")
    client2.utc_now = lambda: TUE_10.replace(hour=22)
    assert eng2.candle_step() is None
    client2.utc_now = lambda: TUE_10
    assert eng2.candle_step() is not None
