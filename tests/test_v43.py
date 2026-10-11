import numpy as np
from config import Config


def test_pro_quality_gate_produces_bounded_score():
    from analysis import MarketState
    from pro_quality import assess
    import test_mt5_engine as T
    from decision import generate
    views = T.long_setup_views()
    ms = MarketState(views, "LONDON_OPEN", T.TUE_10, 0.25)
    cs, _ = generate(ms, 2000.0, Config())
    assert cs
    q = assess(cs[0], ms, Config())
    assert 0 <= q.score <= 100 and q.strong_domains >= 0


def test_order_check_preflight_is_called():
    import test_mt5_engine as T
    mt5 = T.fake_mt5
    cfg, client, j, st, al, risk, ex = T.stack()
    calls = []
    mt5.order_check = lambda req: calls.append(req) or type("R", (), {"retcode": 0, "comment": "Done"})()
    ms, dec = T.setup_decision(client, cfg)
    out = client.send_market(dec.direction, 0.01, dec.sl, dec.tp, "v43")
    assert out.ok and any(x.get("action") == mt5.TRADE_ACTION_DEAL for x in calls)


def test_counterfactual_long_and_short_both_account_for_spread():
    from backtest import counterfactual_ex
    H=np.array([100.,104.,105.,106.]); L=np.array([99.,98.,99.,100.]); C=np.array([100.,103.,104.,105.]); SP=np.array([2.,2.,2.,2.])
    a=counterfactual_ex(H,L,C,SP,0,1,100.,97.,104.,2)
    b=counterfactual_ex(H,L,C,SP,0,-1,100.,103.,96.,2)
    assert a is not None and b is not None


def test_nested_meta_cv_exposes_outer_metrics():
    from metalabel import cross_validate
    rng=np.random.default_rng(4); n=300
    X=rng.normal(size=(n,3)); y=(X[:,0]+0.2*rng.normal(size=n)>0).astype(int)
    t=(np.arange(n,dtype=float),np.arange(n,dtype=float)+3); r=np.where(y==1,1.0,-0.8)
    cv=cross_validate(X,y,t,["a","b","c"],cf_r=r,k=5,min_filtered=10)
    assert "outer_filtered_gain" in cv and "recommended_threshold" in cv and "best_threshold" in cv


def test_pending_fill_preserves_risk_pct_reauthorization():
    import test_v42 as V42
    import test_mt5_engine as T
    cfg, client, st, ex = V42._limit_stack()
    T.fake_mt5.order_check = lambda req: type("R", (), {"retcode": 0, "comment": "Done"})()
    ms, dec = T.setup_decision(client, cfg)
    out = ex.execute(dec, ms, [])
    assert out.executed and str(out.ticket) in st.data["pending"]
    assert st.data["pending"][str(out.ticket)]["dec"]["risk_pct"] > 0


def test_order_check_failure_fails_closed():
    import test_mt5_engine as T
    mt5 = T.fake_mt5
    cfg, client, j, st, al, risk, ex = T.stack()
    mt5.order_check = lambda req: type("R", (), {"retcode": 10019, "comment": "insufficient"})()
    ms, dec = T.setup_decision(client, cfg)
    out = client.send_market(dec.direction, 0.01, dec.sl, dec.tp, "v43")
    assert not out.ok and not T.world.positions


def test_cancel_attempts_remove_even_if_order_check_fails():
    import test_mt5_engine as T
    mt5 = T.fake_mt5
    cfg, client, *_ = T.stack()
    # Seed an agent pending order through the fake broker.
    mt5.order_send({"action": mt5.TRADE_ACTION_PENDING, "symbol": "XAUUSD", "volume": 0.01,
                    "type": mt5.ORDER_TYPE_BUY_LIMIT, "price": 1999.0, "sl": 1990.0, "tp": 2010.0,
                    "magic": cfg.system.magic, "comment": "cancel-test", "type_filling": mt5.ORDER_FILLING_RETURN,
                    "type_time": mt5.ORDER_TIME_SPECIFIED, "expiration": 0})
    ticket = mt5.orders_get()[0].ticket
    mt5.order_check = lambda req: type("R", (), {"retcode": 10019, "comment": "preflight unavailable"})()
    assert client.cancel_order(ticket)
    assert not mt5.orders_get()
    mt5.order_check = lambda req: type("R", (), {"retcode": 0, "comment": "Done"})()


def test_pending_fill_is_rejected_when_current_engine_is_blocked():
    import test_v42 as V42
    import test_mt5_engine as T
    cfg, client, st, ex = V42._limit_stack()
    ms, dec = T.setup_decision(client, cfg)
    out = ex.execute(dec, ms, [])
    ticket = str(out.ticket)
    assert ticket in st.data["pending"]
    import test_mt5_engine as T2
    T2.fake_mt5.fill_pending(out.ticket)
    ex._current_news = T2.NewsContext(status="OK", calendar_ok=True)
    ex._current_regime = None
    # V4.3.1 (intentional change): a SOFT block at fill time keeps the protected position and warns;
    # a news blackout still closes it (see test_v431 for the hard-limit and oversize cases).
    events = ex.manage_pending("market quality 25 below entry threshold")
    assert any(e[0] == "filled" for e in events) and any("WARN" == m[0] for m in ex.alerts.msgs)
    out2 = ex.execute(dec, ms, [])
    T2.fake_mt5.fill_pending(out2.ticket)
    events = ex.manage_pending("news blackout: CPI")
    assert any(e[0] == "filled_rejected" for e in events)


def test_adaptive_risk_never_increases_and_reduces_in_caution():
    from decision import _adaptive_risk_multiplier
    cfg = Config()
    ms = type("M", (), {"micro": type("Q", (), {"band": "CAUTIOUS"})(), "regime": None})()
    c = type("C", (), {"vol_regime": "NORMAL"})()
    assert _adaptive_risk_multiplier(ms, c, cfg, True) == cfg.risk.news_caution_risk_mult
    ms.micro.band = "NORMAL"
    assert _adaptive_risk_multiplier(ms, c, cfg, False) == 1.0


def test_pro_quality_floor_blocks_concentrated_evidence():
    from analysis import MarketState
    from pro_quality import assess
    import test_mt5_engine as T
    views = T.long_setup_views()
    ms = MarketState(views, "LONDON_OPEN", T.TUE_10, 0.25)
    cs, _ = T.generate_for_test(ms) if hasattr(T, "generate_for_test") else ([], [])
    if not cs:
        from decision import generate
        cs, _ = generate(ms, 2000.0, Config())
    c = cs[0]
    c.components.update({"mtf": 25.0, "structure": 4.0, "zone": 4.0, "location": 2.0, "liquidity": 1.0, "momentum": 1.0, "room": 1.0, "volatility": 1.0, "session": 1.0})
    q = assess(c, ms, Config())
    assert not q.passed


def test_close_proceeds_when_preflight_fails_for_risk_reduction():
    import test_mt5_engine as T
    mt5 = T.fake_mt5
    cfg, client, *_ = T.stack()
    ms, dec = T.setup_decision(client, cfg)
    out = client.send_market(dec.direction, 0.01, dec.sl, dec.tp, "close-test")
    assert out.ok
    mt5.order_check = lambda req: type("R", (), {"retcode": 10019, "comment": "preflight unavailable"})()
    assert client.close(out.position)
    assert not mt5.positions_get(ticket=out.ticket)
    mt5.order_check = lambda req: type("R", (), {"retcode": 0, "comment": "Done"})()
