from dataclasses import replace

from config import Config
from decision import Decision
from risk import RiskEngine
from state import State
from journal import Journal
from helpers import FakeClient, tmpdir, NS
from brain.chief import ChiefTraderBrain
from brain.models import BrainFinding


def test_micro_account_risk_cap_is_account_aware_and_never_exceeds_budget():
    cfg = Config()
    # A synthetic low-cost instrument where the broker minimum is actually affordable.
    c = FakeClient(5.0, [])
    c.symbol = "EURUSD"
    c.calc_loss_per_lot = lambda d, entry, sl: 5.0
    c.calc_margin = lambda d, lots, price: 0.0
    st = State(tmpdir() + "/state.json")
    r = RiskEngine(cfg, c, st, Journal(tmpdir()))
    r.refresh()
    dec = Decision(action="BUY", direction=1, style="day", entry=2000.0, sl=1999.0, tp=2002.0,
                   score=75, risk_pct=2.0)
    a = r.approve(dec, 2000.0, [])
    assert a.ok
    assert a.details["account_risk_cap_pct"] == cfg.risk.micro_account_risk_pct
    assert a.risk_pct <= cfg.risk.micro_account_risk_pct + 1e-9
    assert a.lots >= 0.01


def test_micro_account_does_not_force_minimum_lot_when_it_would_break_risk():
    cfg = Config()
    c = FakeClient(5.0, [])
    c.calc_loss_per_lot = lambda d, entry, sl: 600.0
    st = State(tmpdir() + "/state.json")
    r = RiskEngine(cfg, c, st, Journal(tmpdir()))
    r.refresh()
    dec = Decision(action="BUY", direction=1, style="day", entry=2000.0, sl=1994.0, tp=2012.0,
                   score=75, risk_pct=1.0)
    a = r.approve(dec, 2000.0, [])
    assert not a.ok and "minimum lot" in a.reason


def test_chief_treats_advisory_news_veto_as_soft_contradiction():
    chief = ChiefTraderBrain()
    chief.min_score = 62
    ok = lambda name, score=80, bias=1, **facts: BrainFinding(name, "OK", score, score/100, bias, facts=facts)
    decision = Decision(action="BUY", direction=1, score=75, setup="FVG_RETEST", style="day", rr=2.0)
    risk = ok("risk", 90, facts={"risk_multiplier": 1.0})
    # Explicitly construct the finding to keep the test independent of account plumbing.
    risk = BrainFinding("risk", "OK", 90, .9, 0, facts={"risk_multiplier": 1.0})
    news = BrainFinding("news", "OK", 70, .55, -1, facts={"trade_veto": True, "blackout": False,
                                                              "calendar_required": False, "calendar_ok": True,
                                                              "confidence": .55})
    tech = BrainFinding("technical", "OK", 80, .8, 1)
    quant = BrainFinding("quant", "OK", 75, .75, 1)
    inter = BrainFinding("intermarket", "OK", 75, .75, 1)
    opp = BrainFinding("opportunity", "OK", 80, .8, 1)
    conn = BrainFinding("connection", "OK", 100, 1.0, 0)
    out = chief.run(symbol="EURUSD", decision=decision, connection=conn, technical=tech,
                    quant=quant, news=news, intermarket=inter, opportunity=opp, risk=risk, critic=None)
    assert out.permission in ("APPROVE", "REDUCE")
    assert "advisory news veto downgraded to contradiction" in out.contradictions


def test_config_defaults_use_moderate_veto_thresholds():
    c = Config()
    assert c.strategy.min_score == 65.0
    assert c.strategy.min_score_gap == 8.0
    assert c.policy.conflict_reject == 0.65
    assert c.policy.min_hard_quality_score == 48.0
