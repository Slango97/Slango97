from helpers import Config, fake_view, long_setup_views  # noqa: F401

from dataclasses import replace

from analysis import MarketState
from decision import decide, SignalScore
from management import PosView, plan
from news import NewsContext

CFG = Config()


def ms(views, session="LONDON", spread=0.25):
    return MarketState(views=views, session=session, time=None, spread=spread)


def test_clean_long_setup_enters_with_consistent_levels():
    views = long_setup_views()
    d = decide(ms(views), 2000.0, NewsContext(status="NO_DATA"), CFG)
    assert d.action == "BUY" and d.is_entry and d.direction == 1 and d.style == "day"
    assert d.sl < 2000.0 < d.tp and d.rr >= CFG.strategy.styles[d.style].min_rr
    comp = d.components
    assert abs(sum(v for k, v in comp.items() if k != "news") - d.technical) < 0.6
    assert d.setup in ("LIQUIDITY_SWEEP_REVERSAL", "BOS_PULLBACK", "ORDER_BLOCK_RETEST", "TREND_PULLBACK")
    assert set(comp) == set(SignalScore().__dict__) and d.confirmed and d.triggered
    assert d.risk_tier == "fixed" and abs(d.risk_pct - CFG.risk.risk_per_trade_pct) < 1e-9     # no score-based boost


def test_news_is_veto_or_small_bonus_never_dominant():
    views = long_setup_views()
    base = decide(ms(views), 2000.0, NewsContext(status="NO_DATA"), CFG)
    bull = decide(ms(views), 2000.0, NewsContext(bias=1, confidence=1, status="OK", impact="medium"), CFG)
    assert 0 < bull.score - base.score <= CFG.strategy.news_max_bonus + 0.1
    bear = decide(ms(views), 2000.0, NewsContext(bias=-1, confidence=1, status="OK", impact="medium"), CFG)
    assert not bear.is_entry or bear.direction != 1
    unknown = decide(ms(views), 2000.0, NewsContext(bias=-1, confidence=1, status="UNKNOWN"), CFG)
    assert unknown.is_entry


def test_vetoes_extreme_volatility_wrong_session_and_htf_misalignment():
    v = long_setup_views()
    v["H1"]["vol_regime"] = "EXTREME"
    assert not decide(ms(v), 2000.0, NewsContext(), CFG).is_entry
    v = long_setup_views()
    v["H4"]["ema_trend"] = v["H4"]["struct_trend"] = -1                  # structural timeframe opposes
    d = decide(ms(v), 2000.0, NewsContext(), CFG)
    assert not d.is_entry and any("structure opposes" in r for r in d.reasons)
    v = long_setup_views()
    v["M15"]["ob"] = v["H1"]["ob"]
    asia = decide(ms(v, session="ASIA"), 2000.0, NewsContext(), CFG)
    assert asia.style != "scalp"


def test_price_outside_any_zone_waits_and_explains():
    d = decide(ms(long_setup_views()), 2060.0, NewsContext(), CFG)
    assert d.action == "WAIT" and d.wait_reason == "NO_SETUP" and d.reasons


def test_exclude_prevents_duplicate_style_direction():
    v = long_setup_views()
    first = decide(ms(v), 2000.0, NewsContext(), CFG)
    again = decide(ms(v), 2000.0, NewsContext(), CFG, exclude={(first.style, first.direction)})
    assert not again.is_entry or again.style != first.style


def test_wide_spread_relative_to_stop_is_rejected():
    assert not decide(ms(long_setup_views(), spread=3.0), 2000.0, NewsContext(), CFG).is_entry


# ---------------- trade management ----------------
DAY = CFG.strategy.styles["day"]


def st(d=1, entry=2000.0, risk=6.0, **kw):
    return {"direction": d, "entry": entry, "risk": risk, "be": False, "partial": False, **kw}


def pv(price, d=1, sl=1994.0, vol=0.10, age=1.0):
    return PosView(d, price, sl, vol, age, 0.01, 0.01)


def view(close, atr=3.0, lows=(), highs=(), ema=1990.0):
    return {"atr": atr, "close": close, "swing_lows": list(lows), "swing_highs": list(highs), "ema20": ema}


def test_break_even_needs_r_and_held_close():
    acts = plan(st(), pv(2006.5), view(2005.0), False, 0.25, 0.3, DAY)
    assert any(a.kind == "move_sl" and a.flag == "be" and a.value > 2000.0 for a in acts)
    acts = plan(st(), pv(2006.5), view(2001.0), False, 0.25, 0.3, DAY)          # last closed bar did not hold
    assert not any(a.flag == "be" for a in acts)
    acts = plan(st(), pv(2005.0), view(2004.0), False, 0.25, 0.3, DAY)          # only 0.83R
    assert not acts


def test_news_risk_moves_to_break_even_early():
    acts = plan(st(), pv(2002.5), view(2002.0), True, 0.25, 0.3, DAY)
    assert any(a.flag == "be" and "news" in a.reason for a in acts)


def test_partial_fires_once_at_threshold():
    acts = plan(st(), pv(2009.5), view(2009.0, lows=[(1, 2003.0)]), False, 0.25, 0.3, DAY)
    p = [a for a in acts if a.kind == "partial"]
    assert len(p) == 1 and abs(p[0].volume - 0.05) < 1e-9
    assert not [a for a in plan(st(partial=True), pv(2009.5), view(2009.0), False, 0.25, 0.3, DAY) if a.kind == "partial"]


def test_trailing_never_loosens_and_respects_min_distance():
    s = st(be=True, partial=True)
    v = view(2012.0, lows=[(1, 2005.0), (2, 2008.0)])
    acts = plan(s, pv(2012.0, sl=2000.5), v, False, 0.25, 0.3, DAY)
    mv = [a for a in acts if a.kind == "move_sl"]
    assert mv and mv[0].value > 2000.5 and mv[0].value <= 2012.0 - 1.5          # at least 0.5 ATR from price
    assert not plan(s, pv(2012.0, sl=2010.0), view(2012.0, lows=[(1, 2005.0)]), False, 0.25, 0.3, DAY)


def test_trail_modes_choose_different_references():
    base = dict(be=True)
    v = view(2020.0, lows=[(1, 2012.0)], ema=2015.0)
    out = {}
    for mode in ("structure", "atr", "ema", "hybrid"):
        S = replace(DAY, trail_mode=mode, trail_atr_mult=2.0)
        acts = plan(st(**base), pv(2020.0, sl=2000.5), v, False, 0.25, 0.3, S)
        out[mode] = [a.value for a in acts if a.kind == "move_sl"][0]
    assert abs(out["structure"] - (2012.0 - 0.75)) < 1e-9
    assert abs(out["atr"] - (2020.0 - 6.0)) < 1e-9
    assert abs(out["ema"] - (2015.0 - 0.75)) < 1e-9
    assert out["hybrid"] == max(out["structure"], out["atr"])


def test_short_side_is_symmetric():
    s = st(d=-1, entry=2000.0, risk=6.0)
    acts = plan(s, pv(1993.5, d=-1, sl=2006.0), view(1995.0), False, 0.25, 0.3, DAY)
    be = [a for a in acts if a.flag == "be"]
    assert be and be[0].value < 2000.0


def test_time_stop_closes_stale_trade():
    acts = plan(st(), pv(2001.0, age=30.0), view(2001.0), False, 0.25, 0.3, DAY)
    assert acts and acts[0].kind == "close"
    assert not plan(st(), pv(2001.0, age=30.0), view(2001.0), False, 0.25, 0.3, replace(DAY, max_hold_h=100))


def test_config_validation_catches_bad_values():
    assert Config().validate() == []
    bad = replace(CFG, risk=replace(CFG.risk, risk_per_trade_pct=5.0))
    assert any("risk_per_trade_pct" in e for e in bad.validate())
    live = replace(CFG, system=replace(CFG.system, mode="LIVE"))
    assert any("CONFIRM_LIVE" in e for e in live.validate())
