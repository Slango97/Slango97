"""V4.2: regression tests for the six V4.1 defects plus every new capability."""
from helpers import Config, NS, TUE_10, long_setup_views, tmpdir  # noqa: F401

import json
import logging
import os
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

import test_mt5_engine as T
from analysis import MarketState
from config import london_dst, phases_for, session_phase, us_dst
from decision import decide, generate
from events import classify_event
from news import NewsContext, NewsEngine
from registry import PromotionError, Registry

CFG = Config()
world = T.world
EV = lambda title, t: {"title": title, "country": "USD", "impact": "High", "date": t.isoformat()}


# ---------- defect 1: vendor timestamps ----------
def test_trading_economics_times_are_utc_on_any_machine_timezone():
    row = [{"Country": "United States", "Importance": 3, "Date": "2026-10-07T12:30:00", "Event": "Inflation Rate MoM"}]
    parser = NewsEngine(CFG, http_get=lambda _: b"[]", llm=None)._parse_calendar_te
    expected = datetime(2026, 10, 7, 12, 30, tzinfo=timezone.utc)

    # On POSIX, exercise the parser under several process timezones. Windows
    # has no time.tzset(), so validate the parser's timezone-independent
    # semantics directly with naive and explicitly-offset vendor timestamps.
    if hasattr(time, "tzset"):
        original_tz = os.environ.get("TZ")
        try:
            for tz in ("UTC", "Asia/Beirut", "America/New_York"):
                os.environ["TZ"] = tz
                time.tzset()
                assert parser(row)[0]["time"] == expected
        finally:
            if original_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original_tz
            time.tzset()
    else:
        assert parser(row)[0]["time"] == expected
        offset_row = [{**row[0], "Date": "2026-10-07T12:30:00+00:00"}]
        assert parser(offset_row)[0]["time"] == expected


# ---------- defect 2: secrets ----------
def test_secrets_are_redacted_from_logs_health_and_errors():
    from redact import RedactingFilter, redact
    os.environ["TRADING_ECONOMICS_API_KEY"] = "SECRETKEY123"
    try:
        assert "SECRETKEY123" not in redact("url: /calendar?c=SECRETKEY123&f=json")
        assert "123456:ABC-def" not in redact("https://api.telegram.org/bot123456:ABC-def/sendMessage")
        assert "SECRETKEY123" not in redact("plain mention SECRETKEY123 in an exception")
        rec = logging.LogRecord("x", 30, "f", 1, "failed: %s", ("HTTPSConnectionPool url: /cal?c=SECRETKEY123",), None)
        RedactingFilter().filter(rec)
        assert "SECRETKEY123" not in rec.getMessage()
        n = NewsEngine(CFG, http_get=lambda u: (_ for _ in ()).throw(ConnectionError(f"Max retries exceeded with url: {u}")), llm=None)
        n.refresh()
        assert not any("SECRETKEY123" in v["last_error"] for v in n.provider_health().values())
    finally:
        os.environ.pop("TRADING_ECONOMICS_API_KEY", None)


# ---------- defect 3: redundancy semantics ----------
def _two_vendor_news(te_ok, ff_ok, d, strict=False):
    soon = datetime.now(timezone.utc) + timedelta(minutes=10)
    ff = json.dumps([EV("CPI m/m", soon)]).encode()
    te = json.dumps([{"Country": "United States", "Importance": 3, "Date": soon.strftime("%Y-%m-%dT%H:%M:%S"),
                      "Event": "Inflation Rate MoM"}]).encode()
    calls = []

    def http(u):
        calls.append(u)
        if "tradingeconomics" in u:
            if te_ok:
                return te
            raise ConnectionError("TE down")
        if "faireconomy" in u:
            if ff_ok:
                return ff
            raise ConnectionError("FF down")
        return b"<rss><channel></channel></rss>"
    os.environ["TRADING_ECONOMICS_API_KEY"] = "k" * 8
    te_spec = ("tradingeconomics", "te_json", "https://api.tradingeconomics.com/calendar/country/united%20states?c=${TRADING_ECONOMICS_API_KEY}&importance=3&f=json", "tradingeconomics")
    ff_only = tuple(p for p in CFG.news.calendar_providers if p[1] != "bls_html")    # V5.1: TE is opt-in (needs a key)
    nc = replace(CFG.news, calendar_cache_file=os.path.join(d, "cache.json"), calendar_strict_all_families=strict,
                 calendar_providers=ff_only + (te_spec,))
    n = NewsEngine(replace(CFG, news=nc), http_get=http, llm=None)
    n.refresh()
    return n, calls


def test_fresh_vendor_data_is_never_discarded_for_the_cache():
    try:
        d = tmpdir()
        old = datetime.now(timezone.utc) + timedelta(hours=30)
        json.dump({"saved_at": (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat(),
                   "events": [{"title": "OLD", "time": old.isoformat()}]}, open(os.path.join(d, "cache.json"), "w"))
        n, _ = _two_vendor_news(te_ok=False, ff_ok=True, d=d)
        c = n.get()
        assert [e["title"] for e in n.ev.events] == ["CPI m/m"] and c.calendar_coverage == "SINGLE_VENDOR"
        assert c.event_phase == "PRE" and c.blackout and "unavailable" not in c.blackout_reason
        both, _ = _two_vendor_news(te_ok=True, ff_ok=True, d=tmpdir())
        assert both.get().calendar_coverage == "CROSS_CHECKED" and len(both.ev.events) == 1     # FF + TE merged as one CPI
        d2 = tmpdir()
        json.dump({"saved_at": (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat(),
                   "events": [{"title": "OLD", "time": old.isoformat()}]}, open(os.path.join(d2, "cache.json"), "w"))
        strict, _ = _two_vendor_news(te_ok=False, ff_ok=True, d=d2, strict=True)
        assert {"CPI m/m", "OLD"} <= {e["title"] for e in strict.ev.events}                        # strict: cache + fresh union
    finally:
        os.environ.pop("TRADING_ECONOMICS_API_KEY", None)


def test_failing_providers_back_off_instead_of_being_hammered():
    try:
        n, calls = _two_vendor_news(te_ok=False, ff_ok=True, d=tmpdir())
        te1 = sum("tradingeconomics" in c for c in calls)
        n.events_at = 0
        n._load_calendar()                                      # immediate retry attempt
        assert sum("tradingeconomics" in c for c in calls) == te1 and sum("faireconomy" in c for c in calls) == 2
    finally:
        os.environ.pop("TRADING_ECONOMICS_API_KEY", None)


# ---------- defect 4: redundancy switched off ----------
def test_single_provider_mode_does_not_block_forever():
    cfg = replace(CFG, features=replace(CFG.features, news_redundancy=False))
    ev = json.dumps([EV("CPI m/m", datetime.now(timezone.utc) + timedelta(days=1))]).encode()
    n = NewsEngine(cfg, http_get=lambda u: ev if "faireconomy" in u else b"<rss><channel></channel></rss>", llm=None)
    n.refresh()
    c = n.get()
    assert c.calendar_ok and not c.blackout


# ---------- defect 5: vendor titles ----------
def test_vendor_titles_map_to_the_right_event_class():
    C = CFG.events.classes
    for title, cls in (("Inflation Rate MoM", "CPI"), ("Core Inflation Rate YoY", "CPI"), ("Non Farm Payrolls", "NFP"),
                       ("Fed Interest Rate Decision", "FOMC"), ("FOMC Member Waller Speaks", "FED_SPEECH"),
                       ("FOMC Statement", "FOMC"), ("Initial Jobless Claims", "JOBLESS"), ("CPI m/m", "CPI")):
        assert classify_event(title, C)[0] == cls, title


# ---------- defect 6: registry integrity ----------
def test_registry_metadata_tampering_and_signatures():
    reg = Registry(tmpdir())
    vid = reg.register("params", {"strategy.min_score": 50.0}, {"gates_passed": False})
    m = json.loads(reg.path.read_text())
    m["versions"][vid]["status"], m["versions"][vid]["gates_passed"] = "candidate_passed", True
    reg.path.write_text(json.dumps(m))
    try:
        Registry(reg.root).promote(vid, "attacker")
        raise AssertionError("tampered candidate promoted")
    except PromotionError as ex:
        assert "modified" in str(ex)
    os.environ["REGISTRY_HMAC_KEY"] = "a-long-signing-key"
    try:
        r2 = Registry(tmpdir())
        v2 = r2.register("params", {"strategy.min_score": 70.0}, {"gates_passed": True})
        r2.promote(v2, "alice")
        m = json.loads(r2.path.read_text())
        m["versions"][v2]["signature"] = "0" * 64
        r2.path.write_text(json.dumps(m))
        try:
            Registry(r2.root).production("params")
            raise AssertionError("bad signature accepted")
        except PromotionError:
            pass
    finally:
        os.environ.pop("REGISTRY_HMAC_KEY", None)


# ---------- minor: alert delivery ----------
def test_alerts_do_not_retry_4xx_and_honour_retry_after():
    import alerts as A
    calls, sleeps = [], []
    seq = []

    class R:
        def __init__(self, code, ra=None):
            self.status_code, self._ra, self.headers = code, ra, {}

        def json(self):
            return {"parameters": {"retry_after": self._ra}} if self._ra else {}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(self.status_code)
    orig_post, orig_sleep = A.requests.post, A.time.sleep
    A.requests.post = lambda *a, **k: calls.append(1) or seq.pop(0)
    A.time.sleep = lambda s: sleeps.append(s)
    os.environ.update(TELEGRAM_BOT_TOKEN="1:abc", TELEGRAM_CHAT_ID="42")
    try:
        al = A.Alerts(sender=lambda m: None, http_retries=3, http_retry_delay_s=0.5)
        seq[:] = [R(401)]
        try:
            al._deliver("x")
        except RuntimeError:
            pass
        assert len(calls) == 1                                  # unauthorised: not retried
        calls.clear()
        seq[:] = [R(429, ra=7), R(200)]
        al._deliver("x")
        assert len(calls) == 2 and sleeps[-1] == 7.0
    finally:
        A.requests.post, A.time.sleep = orig_post, orig_sleep
        for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            os.environ.pop(k, None)


def test_release_manifest_detects_changes():
    import release_manifest as RM
    root = T.Path(tmpdir()) if hasattr(T, "Path") else __import__("pathlib").Path(tmpdir())
    (root / "a.py").write_text("x = 1\n")
    saved = RM.ROOT
    RM.ROOT = root
    try:
        RM.generate()
        assert RM.verify() == ([], [], [])
        (root / "a.py").write_text("x = 2\n")
        (root / "b.py").write_text("y = 1\n")
        c, m, a = RM.verify()
        assert c == ["a.py"] and a == ["b.py"]
    finally:
        RM.ROOT = saved


# ---------- meta-labelling ----------
def test_purged_folds_never_overlap_the_test_window():
    from metalabel import purged_folds
    t0 = np.arange(500, dtype=float)
    t1 = t0 + 20
    for tr, te in purged_folds(t0, t1, k=5, embargo_frac=0.02):
        lo, hi = t0[te].min(), t1[te].max()
        assert ((t1[tr] < lo) | (t0[tr] > hi + 0.02 * 519)).all() and not set(tr) & set(te)


def test_meta_model_learns_signal_not_noise_and_gates_policy():
    from metalabel import MetaModel, auc, cross_validate, features
    rng = np.random.default_rng(0)
    n = 1200
    X = rng.normal(size=(n, 4))
    y = (X[:, 0] + 0.3 * rng.normal(size=n) > 0).astype(int)
    t = (np.arange(n, dtype=float), np.arange(n, dtype=float) + 5)
    cv = cross_validate(X, y, t, ["a", "b", "c", "d"], cf_r=np.where(y == 1, 1.5, -1.0))
    assert cv["oos_auc"] > 0.85 and cv["oos_brier"] < cv["base_rate_brier"] and cv["importance"][0][0] == "a"
    noise = cross_validate(rng.normal(size=(n, 4)), rng.integers(0, 2, n), t, list("abcd"))
    assert abs(noise["oos_auc"] - 0.5) < 0.08
    v = long_setup_views()
    ms = MarketState(v, "LONDON_OPEN", None, 0.25)
    cands, _ = generate(ms, 2000.0, CFG)
    f = features(cands[0])
    assert {"c_mtf", "rr", "setup_BOS_PULLBACK", "regime_RANGE", "session_LONDON_OPEN"} <= set(f)
    names = sorted(f)
    m = MetaModel().fit(np.array([[0.0] * len(names), [1.0] * len(names)] * 20), np.array([0, 1] * 20), names)
    m.w = np.r_[-5.0, np.zeros(len(names))]                     # always predicts ~0.7%
    on = replace(CFG, policy=replace(CFG.policy, use_meta_model=True))
    d = decide(ms, 2000.0, NewsContext(), on, meta_model=m)
    assert not d.is_entry and d.wait_reason == "META_FILTER"
    assert decide(ms, 2000.0, NewsContext(), CFG, meta_model=m).is_entry      # recorded but not gating when off
    assert MetaModel.from_json(json.loads(json.dumps(m.to_json()))).predict(f) == m.predict(f)
    assert auc([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9]) == 1.0


# ---------- gold drivers ----------
def test_macro_driver_feeds_signed_for_gold_and_used_as_evidence():
    from macro_feeds import MacroFeeds, bias_from_series, load_aux_specs
    up = np.r_[np.full(300, 100.0), np.linspace(100, 110, 30)]
    assert bias_from_series(up + np.random.default_rng(1).normal(0, 0.05, 330)) > 0.5
    d = tmpdir()
    p = os.path.join(d, "usdx.csv")
    pd.DataFrame({"time": pd.date_range("2025-01-01", periods=330, freq="1h").astype(str), "close": up}).to_csv(p, index=False)
    spec = load_aux_specs(f"usd_index={p}:-1")
    assert spec["usd_index"][1] == -1.0 and len(spec["usd_index"][0]) == 330
    c = NS(symbol_available=lambda s: s == "USDX", aux_rates=lambda s, tf, n: pd.DataFrame(
        {"close": up}, index=pd.date_range(pd.Timestamp.now(tz="UTC").tz_localize(None) - pd.Timedelta(hours=329), periods=330, freq="1h")))
    mf = MacroFeeds(CFG, c)
    vals = mf.refresh(force=True)
    assert vals["usd_index"] < -0.5 and "us10y" not in vals and mf.status["us10y"] == "not offered by broker"
    v = long_setup_views()
    ms = MarketState(v, "LONDON_OPEN", None, 0.25)
    plain = generate(ms, 2000.0, CFG)[0][0].conflict
    hostile = generate(ms, 2000.0, CFG, {"usd_index": -1.0, "us10y": -1.0})[0][0].conflict
    assert hostile > plain                                       # rising dollar + yields contradict a gold long


# ---------- sessions ----------
def test_daylight_saving_aware_sessions():
    assert london_dst(datetime(2025, 7, 1, 12)) and not london_dst(datetime(2025, 1, 15, 12))
    assert us_dst(datetime(2025, 3, 20, 12)) and not london_dst(datetime(2025, 3, 20, 12))   # the March gap
    dst = phases_for(replace(CFG.sessions, dst_aware=True))
    plain = phases_for(CFG.sessions)
    summer, winter = datetime(2025, 7, 1, 6, 30), datetime(2025, 1, 15, 6, 30)
    assert session_phase(summer, dst) == "LONDON_OPEN" and session_phase(winter, dst) == "ASIA"
    assert session_phase(summer, plain) == "ASIA"
    assert session_phase(datetime(2025, 7, 1, 12, 45), dst) == "NY_OPEN" and session_phase(datetime(2025, 1, 15, 12, 45), dst) == "OVERLAP"


# ---------- research speed ----------
def test_vectorised_swings_match_the_reference_loop():
    from smc import swings
    rng = np.random.default_rng(4)
    for n in (2, 3, 5):
        c = 100 + np.cumsum(rng.normal(size=800))
        df = pd.DataFrame({"high": c + rng.random(800), "low": c - rng.random(800)})
        sh, sl = swings(df, n)
        hi, lo = df["high"].values, df["low"].values
        ref_h = [i for i in range(n, 800 - n) if hi[i] >= hi[i - n:i].max() and hi[i] > hi[i + 1:i + n + 1].max()]
        ref_l = [i for i in range(n, 800 - n) if lo[i] <= lo[i - n:i].min() and lo[i] < lo[i + 1:i + n + 1].min()]
        assert list(np.where(sh)[0]) == ref_h and list(np.where(sl)[0]) == ref_l


def test_parallel_optimizer_trials_match_sequential():
    from backtest import BTConfig, Backtester, synthetic_m5
    from optimize import Optimizer
    btr = Backtester(CFG, BTConfig(decision_step=9, bars=300))
    prep = btr.prepare(synthetic_m5(4000, seed=6), 5)
    sig = btr.signals(prep)
    lo, hi = sig[0].i, sig[-1].i
    a = Optimizer(btr, prep, sig, 1).best("sizing", CFG, lo, hi)
    b = Optimizer(btr, prep, sig, 1, workers=2).best("sizing", CFG, lo, hi)
    assert [(x[1], x[2]) for x in a] == [(x[1], x[2]) for x in b]


# ---------- execution ----------
def _limit_stack():
    cfg, client, j, st, al, risk, ex = T.stack()
    lim = replace(cfg, features=replace(cfg.features, limit_entries=True), execution=replace(cfg.execution, limit_min_improve_atr=0.05))
    for o in (ex, risk, ex.mgr):
        o.cfg = lim
    return lim, client, st, ex


def test_limit_entry_is_placed_filled_and_registered():
    from levels import limit_price
    cfg, client, st, ex = _limit_stack()
    ms, dec = T.setup_decision(client, cfg)
    assert limit_price(dec, 2000.125, 9.0, cfg) is not None
    out = ex.execute(dec, ms, [])
    assert out.executed and "limit" in out.reason and len(world.pending_orders) == 1 and not world.positions
    tk = world.pending_orders[0].ticket
    assert str(tk) in st.data["pending"] and world.pending_orders[0].price_open < 2000.125
    T.fake_mt5.fill_pending(tk)
    ev = ex.manage_pending()
    assert ev == [("filled", str(tk))] and len(world.positions) == 1 and str(world.positions[0].ticket) in st.trades
    assert not st.data["pending"]


def test_limit_orders_cancel_on_expiry_invalidation_and_blocking():
    cfg, client, st, ex = _limit_stack()
    ms, dec = T.setup_decision(client, cfg)
    ex.execute(dec, ms, [])
    tk = str(world.pending_orders[0].ticket)
    st.data["pending"][tk]["expiry"] = time.time() - 1
    assert ex.manage_pending() == [("cancelled", tk)] and not world.pending_orders
    ex.execute(dec, ms, [])
    world.tick_for_test(dec.sl - 1.0)
    tk = str(world.pending_orders[0].ticket)
    assert ex.manage_pending() == [("cancelled", tk)]
    world.tick_for_test(2000.0)
    ex.execute(dec, ms, [])
    tk = str(world.pending_orders[0].ticket)
    assert ex.manage_pending("news blackout") == [("cancelled", tk)]


def test_reconnect_accepts_our_pending_orders_but_not_unknown_ones():
    eng, cfg, client = T.started_engine("DEMO")
    eng.state.data["pending"] = {"555": {"dec": {}, "price": 1, "expiry": time.time() + 600, "comment": "x"}}
    world.pending_orders = [NS(ticket=555, magic=cfg.system.magic)]
    assert eng.post_reconnect_sync()
    world.pending_orders.append(NS(ticket=777, magic=cfg.system.magic))
    assert not eng.post_reconnect_sync() and "pending_orders" in eng.halt_reason


def test_slippage_model_calibrates_from_your_fills():
    from costs import CostModel, calibrate_from_trades
    rng = np.random.default_rng(2)
    closes = [{"slippage": 0.05 + 0.4 * s + rng.normal(0, 0.005), "spread": s, "session": "NY_OPEN" if k % 2 else "LONDON_CONT"}
              for k, s in enumerate(rng.uniform(0.2, 0.6, 200))]
    cm, info = calibrate_from_trades(closes)
    assert abs(cm.base_slippage - 0.05) < 0.02 and abs(cm.spread_frac - 0.4) < 0.05 and info["n"] == 200
    p = os.path.join(tmpdir(), "cm.json")
    cm.save(p)
    assert CostModel.load(p).spread_frac == cm.spread_frac
    assert calibrate_from_trades(closes[:5])[0] is None


# ---------- shadow / telegram ----------
def test_shadow_mode_records_but_never_trades():
    from shadow import ShadowRunner, report
    d = tmpdir()
    reg = Registry(os.path.join(d, "reg"))
    vid = reg.register("params", {"strategy.min_score": 99.0}, {"gates_passed": False})   # even a failed candidate can be shadowed
    cfg = replace(CFG, shadow=replace(CFG.shadow, params_version=vid))
    sr = ShadowRunner(cfg, reg, d)
    ms = MarketState(long_setup_views(), "LONDON_OPEN", None, 0.25)
    prod = decide(ms, 2000.0, NewsContext(), CFG)
    rec = sr.evaluate(ms, 2000.0, NewsContext(), (), prod)
    assert prod.is_entry and rec["shadow"]["action"] == "WAIT" and rec["shadow"]["wait_reason"] == "SCORE" and not rec["agree"]
    recs = [json.loads(x) for x in open(sr.path).read().splitlines()]
    bars = pd.DataFrame({"high": np.linspace(2000, 2050, 50), "low": np.linspace(1999, 2049, 50), "close": np.linspace(2000, 2050, 50)},
                        index=pd.date_range(pd.Timestamp(recs[0]["t"]), periods=50, freq="5min"))
    rp = report(recs, bars)
    assert rp["agreement"] == 0.0 and rp["production_entries"] == 1 and rp["production_hypothetical"]["n"] == 1


def test_telegram_commands_are_authenticated_and_two_step():
    from telegram_cmd import CommandHandler, TelegramCommander
    d = tmpdir()
    cfg = replace(CFG, emergency=replace(CFG.emergency, stop_file=os.path.join(d, "STOP"), flatten_file=os.path.join(d, "FLATTEN")))
    os.environ.update(TELEGRAM_CHAT_ID="42", TELEGRAM_COMMAND_PIN="2468", TELEGRAM_ALLOWED_USER_IDS="7")
    clk = [1000.0]
    try:
        h = CommandHandler(cfg, lambda: "status ok", lambda: "report", clock=lambda: clk[0])
        assert h.handle("/stop", 99, 7) is None and not os.path.exists(cfg.emergency.stop_file)       # wrong chat
        assert h.handle("/stop", 42, 8) is None                                                       # wrong user
        assert "STOP set" in h.handle("/stop", 42, 7) and os.path.exists(cfg.emergency.stop_file)
        assert h.handle("/resume 0000", 42, 7) == "Wrong PIN." and os.path.exists(cfg.emergency.stop_file)
        reply = h.handle("/resume 2468", 42, 7)
        assert "Resume requested" in reply and not os.path.exists(cfg.emergency.stop_file)
        assert os.path.exists(os.path.join(cfg.system.data_dir, "resume.request"))
        msg = h.handle("/flatten 2468", 42, 7)
        code = msg.split("/confirm ")[1].split()[0]
        assert not os.path.exists(cfg.emergency.flatten_file)
        clk[0] += 61
        assert "expired" in h.handle(f"/confirm {code}", 42, 7)
        code = h.handle("/flatten 2468", 42, 7).split("/confirm ")[1].split()[0]
        assert "FLATTEN requested" in h.handle(f"/confirm {code}", 42, 7) and os.path.exists(cfg.emergency.flatten_file)
        for _ in range(5):
            h.handle("/resume 1111", 42, 7)
        assert "Locked" in h.handle("/resume 2468", 42, 7)
        sent = []
        tc = TelegramCommander(h, http_get=lambda url, params: {"result": [{"update_id": 5, "message": {"text": "/status", "chat": {"id": 42}, "from": {"id": 7}}}]},
                               http_post=lambda url, data: sent.append(data))
        tc.poll_once()
        assert sent == [{"chat_id": "42", "text": "status ok"}] and tc.offset == 6
    finally:
        for k in ("TELEGRAM_CHAT_ID", "TELEGRAM_COMMAND_PIN", "TELEGRAM_ALLOWED_USER_IDS"):
            os.environ.pop(k, None)


# ---------- evidence pipeline ----------
def test_evidence_pipeline_produces_a_report_with_a_verdict():
    import argparse
    import evidence
    out = tmpdir()
    a = argparse.Namespace(csv=None, events=None, start=None, end=None, aux=None, calibrate_costs=None, synthetic=4500,
                           timeframe="M5", offset=0.0, decision_step=9, folds=3, groups="sizing", min_trades=1, workers=0,
                           broker_diag=False, register=False, out=out)
    rep = evidence.run(a)
    assert rep["verdict"].startswith(("EVIDENCE", "NOT READY")) and len(rep["readiness"]) >= 6
    txt = open(os.path.join(out, "EVIDENCE_REPORT.md")).read()
    assert "Verdict" in txt and "Walk-forward" in txt and "Meta-label" in txt
