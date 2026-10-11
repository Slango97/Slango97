from helpers import Config, FakeClient, pos, tmpdir, NS  # noqa: F401

import os

from config import env_override
from journal import Journal
from state import State
from trade_manager import TradeManager


def test_env_overrides_cast_by_field_type():
    os.environ["RISK__RISK_PER_TRADE_PCT"] = "0.25"
    os.environ["RISK__MAX_OPEN_TRADES"] = "2"
    os.environ["SESSIONS__AVOID_HOURS_UTC"] = "21,22"
    os.environ["NEWS__ENABLED"] = "false"
    try:
        c = Config()
        r, s, n = env_override(c.risk, "RISK"), env_override(c.sessions, "SESSIONS"), env_override(c.news, "NEWS")
        assert r.risk_per_trade_pct == 0.25 and r.max_open_trades == 2
        assert s.avoid_hours_utc == (21, 22) and n.enabled is False
        assert c.risk.risk_per_trade_pct == 0.5            # frozen originals are untouched
    finally:
        for k in ("RISK__RISK_PER_TRADE_PCT", "RISK__MAX_OPEN_TRADES", "SESSIONS__AVOID_HOURS_UTC", "NEWS__ENABLED"):
            os.environ.pop(k, None)


def test_config_is_frozen():
    try:
        Config().risk.risk_per_trade_pct = 5
    except Exception:
        return
    raise AssertionError("config should be immutable")


def test_dry_run_manager_never_touches_real_positions():
    d = tmpdir()
    c = FakeClient(10000.0, [pos(1, entry=2000.0, sl=1994.0, price=2020.0)])
    c.read_only = True
    st = State(os.path.join(d, "s.json"))
    st.trades["1"] = {"style": "day", "direction": 1, "entry": 2000.0, "risk": 6.0, "be": False, "partial": False}
    TradeManager(Config(), c, st, Journal(d)).manage({}, NS(blackout=False))
    assert not c.mods and not c.closes


def test_env_file_inline_comments_and_quotes():
    import tempfile
    from config import load_env
    keys = ("T_A", "T_B", "T_C", "T_D", "T_E")
    for k in keys:
        os.environ.pop(k, None)
    p = os.path.join(tempfile.mkdtemp(), ".env")
    with open(p, "w") as f:
        f.write('T_A=XAUUSD          # exact symbol name\nT_B="pass # word"\nT_C=a#b\n# T_D=ignored\nT_E=\n')
    try:
        load_env(p)
        assert os.environ["T_A"] == "XAUUSD" and os.environ["T_B"] == "pass # word" and os.environ["T_C"] == "a#b"
        assert "T_D" not in os.environ and os.environ["T_E"] == ""
    finally:
        for k in keys:
            os.environ.pop(k, None)


def test_shipped_env_example_produces_a_valid_config():
    from pathlib import Path
    from config import Config, load_env
    ex = Path(__file__).resolve().parent.parent / ".env.example"
    before = dict(os.environ)
    try:
        load_env(str(ex))
        c = Config.from_env()
        assert c.validate() == [] and c.system.symbol == "XAUUSD" and c.intel.provider == "anthropic"
        assert c.system.mode == "DRY_RUN"
    finally:
        os.environ.clear()
        os.environ.update(before)
