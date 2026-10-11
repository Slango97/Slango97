"""V5.7: drawdown-aware and volatility-targeted exposure scaling (live + backtest share one function)."""
from dataclasses import replace

from helpers import FakeClient  # noqa: F401  (sets sys.path)
from config import Config
from risk import exposure_scale
from test_risk_state_news import dec, engine_for

RC = Config().risk


def test_drawdown_scaling_is_linear_between_start_and_full_and_recovers():
    assert exposure_scale(RC, 0.0) == (1.0, {"drawdown": 1.0})
    assert exposure_scale(RC, 2.0)[0] == 1.0
    m, parts = exposure_scale(RC, 4.0)                       # half way from 2% to 6% -> 1 - 0.65/2
    assert abs(m - 0.675) < 1e-9 and parts["drawdown"] == 0.675
    assert exposure_scale(RC, 6.0)[0] == RC.dd_scale_floor
    assert exposure_scale(RC, 7.9)[0] == RC.dd_scale_floor   # the 8% hard halt stays the risk gate's job
    assert exposure_scale(RC, 1.0)[0] == 1.0                 # back near the peak -> full size again


def test_market_volatility_only_reduces_risk():
    assert exposure_scale(RC, 0.0, atr_ratio=0.6)[0] == 1.0   # calm market never raises risk
    assert exposure_scale(RC, 0.0, atr_ratio=1.2)[0] == 1.0
    m, parts = exposure_scale(RC, 0.0, atr_ratio=2.0)
    assert abs(m - 1.3 / 2.0) < 1e-9 and "market_vol" in parts
    assert exposure_scale(RC, 0.0, atr_ratio=9.0)[0] == RC.vol_scale_floor
    assert exposure_scale(RC, 0.0, atr_ratio=float("nan"))[0] == 1.0


def test_account_volatility_targeting():
    calm = [10000 * (1 + 0.001 * (i % 2)) for i in range(20)]           # ~1.6%/yr: under the 12% target
    m, parts = exposure_scale(RC, 0.0, day_equities=calm)
    assert m == 1.0 and parts["account_vol_pct"] < RC.equity_vol_target_pct
    wild = [10000 * (1.03 if i % 2 else 0.97) for i in range(20)]       # ~95%/yr
    m, parts = exposure_scale(RC, 0.0, day_equities=wild)
    assert m == RC.equity_vol_floor and parts["account_vol_pct"] > 50
    assert "account_vol_pct" not in exposure_scale(RC, 0.0, day_equities=calm[:5])[1]   # too little history


def test_combined_factors_multiply_with_a_floor_and_can_be_disabled():
    m, parts = exposure_scale(RC, 4.0, atr_ratio=2.0)
    assert abs(m - round(0.675 * 0.65, 4)) < 1e-4
    assert exposure_scale(RC, 10.0, atr_ratio=9.0)[0] == RC.exposure_scale_floor   # 0.35*0.5 -> floored 0.25
    off = replace(RC, dd_scaling=False, vol_scaling=False)
    assert exposure_scale(off, 10.0, atr_ratio=9.0) == (1.0, {})
    for dd in (0, 3, 5, 9):
        for ratio in (None, 0.5, 1.5, 4):
            assert 0 < exposure_scale(RC, dd, ratio)[0] <= 1.0


def test_live_approval_sizes_down_in_drawdown_and_high_volatility():
    c = FakeClient(10000.0, [])
    r, st = engine_for(c)
    full = r.approve(dec(risk_pct=0.5), 2000.0, [])
    assert full.ok and full.details["exposure_scale"] == 1.0 and full.lots == 0.08
    st.risk["peak"] = 10000.0 / 0.96                               # equity is 4% below its peak
    dd = r.approve(dec(risk_pct=0.5), 2000.0, [])
    assert dd.ok and abs(dd.details["exposure_scale"] - 0.675) < 1e-3 and dd.lots == 0.05
    both = r.approve(dec(risk_pct=0.5), 2000.0, [], atr_ratio=2.0)
    assert both.lots < dd.lots and "scale_market_vol" in both.details


def test_daily_equity_history_is_recorded_once_per_day():
    c = FakeClient(10000.0, [])
    r, st = engine_for(c)
    assert st.risk["day_equities"] == [10000.0]
    r.refresh()
    assert st.risk["day_equities"] == [10000.0]                      # same day: no duplicate point
    from datetime import timedelta
    c.now, c.equity = c.now + timedelta(days=1), 10100.0
    r.refresh()
    assert st.risk["day_equities"] == [10000.0, 10100.0]


def test_exposure_report_only_on_band_changes(tmp_path):
    from test_v55 import _reporter
    rep = _reporter(tmp_path, lambda: "EURUSD")
    rep.exposure_report(1.0, {"drawdown": 1.0})                      # normal at start: silent
    rep.exposure_report(0.68, {"drawdown": 0.68})
    rep.exposure_report(0.67, {"drawdown": 0.67})                    # same 10% band: silent
    rep.exposure_report(1.0, {"drawdown": 1.0})
    titles = [t.split("\n")[1].split(" — ")[0] for t in rep.tg.texts]
    assert titles == ["RISK SCALED DOWN", "RISK BACK TO FULL SIZE"]
    assert "drawdown x0.68" in rep.tg.texts[0] and "Active market: EURUSD" in rep.tg.texts[0]


def test_research_metrics_use_the_risk_actually_taken():
    from market_research import acceptance_metrics
    from test_v56_markets import _trades
    tr = _trades([-1.0] * 10 + [2.0] * 10)
    fixed = acceptance_metrics(tr, risk_pct=1.0)
    scaled = acceptance_metrics(tr.assign(risk_frac=[0.01] * 3 + [0.005] * 17), risk_pct=1.0)
    assert scaled["max_dd_pct"] < fixed["max_dd_pct"] and scaled["pf"] == fixed["pf"] == 2.0
    assert acceptance_metrics(tr.assign(risk_frac=[None] * 20), 1.0)["max_dd_pct"] == fixed["max_dd_pct"]
