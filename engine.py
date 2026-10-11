import logging
import os
from pathlib import Path
import signal
import threading
import time
from enum import Enum

from collections import Counter

import pandas as pd

import records
from alerts import Alerts
from bus import EventBus
from connection import ReconnectPolicy
from explain import TelegramNotifier, daily_stats
from refine import Armed, m1_check
from analysis import analyze_tf, build_state
from calibration import Calibration
from decision import decide
from liquidity import build_map
from microstructure import TickMonitor
from regime import classify as classify_regime
from setups import EdgeBook
from execution import ExecutionEngine
from health import Health
from indicators import DataError
from journal import Journal
from news import NewsContext, NewsEngine
from reconciliation import reconcile
from risk import RiskEngine
from state import State
from trade_manager import TradeManager
from brain import V5BrainSystem

log = logging.getLogger("engine")


class Lifecycle(str, Enum):
    STARTING = "STARTING"
    CONNECTING = "CONNECTING"
    VALIDATING = "VALIDATING"
    SYNCING = "SYNCING"
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"      # recoverable problem: no new trades, keeps managing positions
    HALTED = "HALTED"          # safety stop: no new trades, keeps protecting open positions
    SHUTDOWN = "SHUTDOWN"


def market_open_guess(now):
    wd = now.weekday()
    return wd < 4 or (wd == 4 and now.hour < 22) or (wd == 6 and now.hour >= 22)


class AgentEngine:
    def __init__(self, cfg, client, news=None, alerts=None, journal=None, state=None):
        self.cfg = cfg
        self.client = client
        self.journal = journal or Journal(cfg.system.data_dir)
        self.state = state or State(cfg.system.state_file)
        self.alerts = alerts or Alerts(http_retries=cfg.notify.http_retries, http_retry_delay_s=cfg.notify.http_retry_delay_s)
        self.news = news or NewsEngine(cfg)
        self.risk = RiskEngine(cfg, client, self.state, self.journal, on_pause=self._on_pause)
        self.mgr = TradeManager(cfg, client, self.state, self.journal, self.alerts)
        self.monitor = TickMonitor(cfg, client)
        self.execution = ExecutionEngine(cfg, client, self.risk, self.mgr, self.journal, self.alerts, self.monitor)
        self.calibration = self.edge_book = self.meta_model = None
        from macro_feeds import MacroFeeds
        self.macro = MacroFeeds(cfg, client) if cfg.features.macro_feeds else None
        self.shadow = None
        self.commander = None
        self.last_decision = None
        self.regime = None
        self.micro = None
        self.wait_counts = Counter()
        self.bus = EventBus()
        self.bus.subscribe("record", lambda _, r: self.journal.record(r) if cfg.features.decision_records else None)
        self.notifier = TelegramNotifier(cfg, self.alerts, self.bus) if cfg.features.telegram_explain else None
        self.v5_brain = V5BrainSystem(cfg, client=client, risk_engine=self.risk, alerts=self.alerts) if getattr(cfg.brain, "enabled", True) else None
        if self.v5_brain is not None:
            self.bus.subscribe("trade_close", lambda _, t: self.v5_brain.reporter.trade_feedback(t))
        self.reconnect = ReconnectPolicy(cfg.reliability)
        self.reconcile_required = False
        self._t_sync = self._t_diag = 0.0
        self.armed = None
        self._m1_bar = None
        self._tf_last = {}
        self._tf_retry = {}
        self.versions = {}
        self._feed_alert_at = 0.0
        self.health = Health(cfg)
        self.lifecycle = Lifecycle.STARTING
        self.halt_reason = ""
        self.views, self._cache = {}, {}
        self.raw_frames = {}
        self.last_bar = None
        self.news_ctx = NewsContext()
        self.recon_critical = []
        self._t_recon = self._t_hb = 0.0
        self._report_day = None
        self.stop_flag = threading.Event()
        # Durable safety latch: HALTED survives process restarts until an explicit,
        # validated resume request is processed by this engine.
        self.safety_latch = Path(cfg.system.data_dir) / "safety_halt.json"
        self.resume_request = Path(cfg.system.data_dir) / "resume.request"

    # ---- lifecycle --------------------------------------------------
    def set_lifecycle(self, new, reason=""):
        if new == self.lifecycle and (reason == self.halt_reason or
                                     (new == Lifecycle.DEGRADED and str(self.halt_reason).startswith("stale ticks")
                                      and str(reason).startswith("stale ticks"))):
            return
        old, self.lifecycle = self.lifecycle, new
        self.halt_reason = reason if new in (Lifecycle.HALTED, Lifecycle.DEGRADED) else ""
        log.warning("lifecycle %s -> %s %s", old.value, new.value, reason)
        self.journal.event("lifecycle_engine", old=old.value, new=new.value, reason=reason)
        if new == Lifecycle.HALTED:
            try:
                self.safety_latch.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.safety_latch.with_suffix(".tmp")
                import json
                tmp.write_text(json.dumps({"halted": True, "reason": str(reason), "at": time.time()}), encoding="utf-8")
                os.replace(tmp, self.safety_latch)
            except Exception as ex:
                log.critical("could not persist safety halt latch: %s", ex)
            self.alerts.send("CRITICAL", f"HALTED: {reason}", key="halt")
        elif new == Lifecycle.DEGRADED and old == Lifecycle.RUNNING:
            self.alerts.send("WARN", f"DEGRADED: {reason}", key="degraded")
        elif new == Lifecycle.RUNNING and old in (Lifecycle.DEGRADED, Lifecycle.HALTED):
            self.alerts.send("INFO", "Recovered: RUNNING again", key="recovered")

    def _on_pause(self, streak, diag):
        self.alerts.send("WARN", f"{streak} losses in a row: entries paused. Diagnosis: " + "; ".join(diag["findings"]), key="pause")

    def _load_models(self):
        pc = self.cfg.policy
        if self.cfg.features.registry_models:
            from registry import Registry
            reg = Registry(self.cfg.learning.registry_dir)
            if pc.use_calibration and reg.production("calibration"):
                self.calibration = Calibration(reg.production("calibration")["payload"])
                self.versions["calibration"] = reg.production("calibration")["id"]
            if pc.use_meta_model and reg.production("meta_model"):
                from metalabel import MetaModel
                self.meta_model = MetaModel.from_json(reg.production("meta_model")["payload"])
                self.versions["meta_model"] = reg.production("meta_model")["id"]
            if pc.use_edge_book and reg.production("edge_book"):
                from setups import EdgeBook as _EB
                self.edge_book = _EB(reg.production("edge_book")["payload"].get("setups", {}), pc.edge_min_trades)
                self.versions["edge_book"] = reg.production("edge_book")["id"]
            if self.versions:
                self.journal.event("production_models_loaded", **self.versions)
                return
        if pc.use_calibration:
            self.calibration = Calibration.load(pc.calibration_file)
            if self.calibration is None:
                self.alerts.send("WARN", f"use_calibration is on but {pc.calibration_file} is missing; expectancy gate inactive")
        if pc.use_edge_book:
            self.edge_book = EdgeBook.load(pc.edge_book_file, pc.edge_min_trades)

    def _start_extras(self):
        cfg = self.cfg
        if cfg.features.shadow_mode and (cfg.shadow.params_version or cfg.shadow.meta_version):
            try:
                from registry import Registry
                from shadow import ShadowRunner
                self.shadow = ShadowRunner(cfg, Registry(cfg.learning.registry_dir), cfg.system.data_dir)
                self.journal.event("shadow_started", **self.shadow.versions)
            except Exception as ex:
                self.alerts.send("WARN", f"shadow mode not started: {ex}")
        if cfg.features.telegram_commands and os.getenv("TELEGRAM_COMMAND_PIN") and os.getenv("TELEGRAM_BOT_TOKEN"):
            from telegram_cmd import CommandHandler, TelegramCommander
            h = CommandHandler(cfg, self.status_text, self.report_text)
            self.commander = TelegramCommander(h)
            self.commander.start()

    def status_text(self):
        s = self.health.snapshot(self.lifecycle.value, self.client, self.state, self.risk)
        d = self.last_decision
        return (f"{self.lifecycle.value} {self.halt_reason}\nequity {s.get('equity')} | positions {s.get('open_positions')} | "
                f"day P/L {s.get('day_pnl')}\nregime {self.regime.label if self.regime else '?'} | "
                f"last decision {d.action + ' ' + (d.wait_reason or d.setup) if d else 'none'}")

    def report_text(self):
        from explain import daily_stats, fmt_daily
        day = str(self.client.utc_now().date())
        return fmt_daily(daily_stats(self.journal.read_jsonl(self.journal.trades_path),
                                     self.journal.read_jsonl(self.journal.dir / "decision_records.jsonl"), day))

    def startup(self):
        cfg = self.cfg
        self._load_models()
        self._start_extras()
        self.set_lifecycle(Lifecycle.CONNECTING)
        self.client.connect()
        self.set_lifecycle(Lifecycle.VALIDATING)
        problems = cfg.validate()
        acct = self.client.account_mode()
        if cfg.system.mode == "DEMO" and acct != "DEMO":
            problems.append(f"mode DEMO but the connected account is {acct}")
        if cfg.system.mode == "LIVE" and acct != "REAL":
            problems.append(f"mode LIVE but the connected account is {acct}")
        errs, warns = self.client.validate_broker()
        for w in warns:
            log.info("broker: %s", w)
        problems += errs
        self.set_lifecycle(Lifecycle.SYNCING)
        if cfg.dry_run and self.client.positions():
            self.alerts.send("WARN", "Agent positions exist but mode is DRY_RUN: they are NOT being managed. "
                                     "Manage them manually or restart in DEMO/LIVE.", key="dryrun_positions")
        for n in self.state.notes:
            self.journal.event("state_note", note=n)
        self.risk.refresh()
        self.refresh_views(force=True)
        self.run_reconcile()
        if self.state.needs_recovery:
            self.alerts.send("WARN", "State file was damaged; rebuilt from MT5. " + "; ".join(self.state.notes))
            self.state.acknowledge()
        if problems:
            self.set_lifecycle(Lifecycle.HALTED, "; ".join(problems))
        elif self.recon_critical:
            self.set_lifecycle(Lifecycle.HALTED, "; ".join(self.recon_critical))
        elif self.safety_latch.exists():
            try:
                import json
                prior = json.loads(self.safety_latch.read_text(encoding="utf-8"))
                reason = str(prior.get("reason", "persisted safety halt"))
            except Exception:
                reason = "persisted safety halt (marker unreadable)"
            self.set_lifecycle(Lifecycle.HALTED, reason)
        else:
            self.set_lifecycle(Lifecycle.RUNNING)
            self._apply_profile()
            self.alerts.send("INFO", f"Agent started in {cfg.system.mode} mode on {self.client.symbol}", key="start")

    # ---- data -------------------------------------------------------
    def refresh_views(self, force=False):
        """Re-analyze only timeframes whose last closed candle changed."""
        cfg = self.cfg
        period = {"M1": 60, "M5": 300, "M15": 900, "H1": 3600, "H4": 14400, "D1": 86400, "W1": 604800}
        now = pd.Timestamp(self.client.utc_now()).tz_convert(None)
        for tf in cfg.system.timeframes:
            last = self._tf_last.get(tf)
            if cfg.features.htf_fetch_schedule and not force and last is not None and tf in self.views \
                    and now < last + pd.Timedelta(seconds=2 * period[tf] - 2):
                continue                    # no new candle can have closed yet: keep the cached analysis
            if cfg.features.htf_fetch_schedule and not force and now < self._tf_retry.get(tf, now):
                continue
            try:
                df, issues = self.client.rates(tf, cfg.system.bars)
                self.raw_frames[tf] = df.copy()
            except DataError as ex:
                log.warning("no usable data for %s: %s", tf, ex)
                self.views.pop(tf, None)
                self._tf_retry[tf] = now + pd.Timedelta(seconds=period[tf])     # do not re-pull every loop
                continue
            key = (tf, df.index[-1])
            self._tf_last[tf] = df.index[-1]
            if force or key not in self._cache:
                for k in [k for k in self._cache if k[0] == tf]:
                    self._cache.pop(k)
                self._cache[key] = analyze_tf(df, tf, cfg.sessions.bounds, cfg.strategy.adx_bands, phases=cfg.sessions.phases)
            self.views[tf] = self._cache[key]

    def run_reconcile(self):
        on_close = (lambda r: self.bus.publish("trade_close", r)) if (self.notifier or self.v5_brain is not None) else None
        rep = reconcile(self.cfg, self.client, self.state, self.journal, self.risk, self.views, self.alerts, on_close)
        self.recon_critical = rep.critical
        for c in rep.critical:
            self.alerts.send("CRITICAL", c, key=c)
        self._t_recon = time.time()
        return rep

    # ---- emergency --------------------------------------------------
    def flatten_all(self, reason):
        closed, failed = self.client.close_all_agent_positions()
        self.journal.event("flatten", reason=reason, closed=closed, failed=failed)
        self.alerts.send("CRITICAL", f"FLATTEN ({reason}): closed {closed}, failed {failed}", key="flatten")
        self.set_lifecycle(Lifecycle.HALTED, f"flattened: {reason}")

    def _resume_checks(self):
        """Fail-closed checks required before clearing a durable HALTED latch."""
        if os.path.exists(self.cfg.emergency.stop_file) or os.path.exists(self.cfg.emergency.flatten_file):
            return False, "STOP/FLATTEN marker still present"
        if not self.client.ensure_connected():
            return False, "MT5 connection unavailable"
        expected = self.cfg.system.mode
        actual = self.client.account_mode()
        if expected in ("DEMO", "LIVE") and actual != expected:
            return False, f"account mode mismatch: expected {expected}, got {actual}"
        errs, _ = self.client.validate_broker()
        if errs:
            return False, "broker validation failed: " + "; ".join(errs)
        if self.cfg.features.broker_fail_closed and self.client.broker_diagnosis().status != "OK":
            return False, "broker diagnosis is not OK"
        if self.reconcile_required and not self.post_reconnect_sync():
            return False, "post-reconnect reconciliation failed"
        rep = self.run_reconcile()
        if rep.critical:
            return False, "reconciliation unresolved: " + "; ".join(rep.critical)
        ok, why = self.risk.hard_limits(self.client.all_positions())
        if not ok:
            return False, "risk hard limit failed: " + why
        return True, "all resume checks passed"

    def check_kill_switch(self):
        em = self.cfg.emergency
        if os.path.exists(em.flatten_file):
            if self.lifecycle != Lifecycle.HALTED or "flattened" not in self.halt_reason:
                self.flatten_all(f"{em.flatten_file} file present")
            return True
        if os.path.exists(em.stop_file):
            self.set_lifecycle(Lifecycle.HALTED, f"{em.stop_file} file present")
            return True
        # A HALTED state is latched across restarts. Removing STOP is not a resume.
        if self.safety_latch.exists():
            if not self.resume_request.exists():
                if self.lifecycle != Lifecycle.HALTED:
                    try:
                        import json
                        reason = str(json.loads(self.safety_latch.read_text(encoding="utf-8")).get("reason", "persisted safety halt"))
                    except Exception:
                        reason = "persisted safety halt (marker unreadable)"
                    self.set_lifecycle(Lifecycle.HALTED, reason)
                return True
            # Consume the request before validation so it cannot cause repeated resume attempts.
            try:
                self.resume_request.unlink(missing_ok=True)
            except Exception:
                self.set_lifecycle(Lifecycle.HALTED, "unable to consume resume request")
                return True
            ok, reason = self._resume_checks()
            if not ok:
                self.set_lifecycle(Lifecycle.HALTED, "resume rejected: " + reason)
                self.alerts.send("CRITICAL", "Resume rejected: " + reason, key="resume_rejected")
                return True
            try:
                self.safety_latch.unlink(missing_ok=True)
            except Exception as ex:
                self.set_lifecycle(Lifecycle.HALTED, f"unable to clear safety latch: {ex}")
                return True
            self.set_lifecycle(Lifecycle.RUNNING, "explicit resume validated")
            self.alerts.send("INFO", "Resume accepted after safety checks.", key="resume_accepted")
            return False
        return False

    # ---- one fast iteration ------------------------------------------
    def fast_step(self):
        cfg, now = self.cfg, self.client.utc_now()
        if cfg.features.reconnect_backoff:
            if not self.connection_step():
                return
        elif not self.client.ensure_connected():
            self.health.mt5_ok = False
            self.set_lifecycle(Lifecycle.DEGRADED, "MT5 connection lost")
            return
        try:
            current_age = max(0.0, float(self.client.tick_age_s()))
            if current_age <= cfg.execution.max_tick_age_s:
                self.health.tick_ok()
            else:
                self.health.mt5_ok = False
        except Exception:
            self.health.mt5_ok = False
        if cfg.features.broker_fail_closed and time.time() - self._t_diag >= 60 * cfg.reliability.broker_diag_interval_min:
            self._t_diag = time.time()
            dg = self.client.broker_diagnosis()
            self.journal.event("broker_diagnosis", status=dg.status, cause=dg.cause, errors=dg.errors)
            if dg.status == "FAIL":
                self.set_lifecycle(Lifecycle.HALTED, "broker data inconsistent: " + "; ".join(dg.errors))
            elif self.lifecycle == Lifecycle.HALTED and self.halt_reason.startswith("broker data inconsistent"):
                self.set_lifecycle(Lifecycle.RUNNING)
        self.monitor.sample()
        self.micro = self.monitor.evaluate()
        age = self.client.tick_age_s()
        if age > cfg.execution.max_tick_age_s and market_open_guess(now):
            self.set_lifecycle(Lifecycle.DEGRADED, f"stale ticks ({age:.0f}s)")
        elif self.lifecycle == Lifecycle.DEGRADED:
            self.set_lifecycle(Lifecycle.RUNNING)

        self.risk.refresh()
        self.news_ctx = self.news.get(vol_probe=self.monitor.vol_probe)
        self.health.news(not self.news.last_error, self.news.last_error)

        halted_by_file = self.check_kill_switch()
        if cfg.emergency.flatten_on_max_drawdown and self.risk.drawdown_pct() >= cfg.risk.max_drawdown_pct \
                and self.client.positions():
            self.flatten_all(f"max drawdown {cfg.risk.max_drawdown_pct}% reached")

        if time.time() - self._t_recon >= cfg.system.reconcile_s:
            self.run_reconcile()
            if self.recon_critical and self.lifecycle == Lifecycle.RUNNING:
                self.set_lifecycle(Lifecycle.HALTED, "; ".join(self.recon_critical))

        m5 = self.client.last_bar_time("M5")
        new_bar = m5 is not None and m5 != self.last_bar
        if new_bar:
            self.last_bar = m5
            self.health.candle(m5)
            self.refresh_views()
        if self.views:
            self.mgr.manage(self.views, self.news_ctx)
        if self.state.data.get("pending") or self.state.data.get("pending_sim"):
            ok_, why_ = self.can_enter()
            blocked = None if ok_ and not self.news_ctx.blackout else (why_ if not ok_ else "news blackout")
            if blocked is None and getattr(self.cfg.policy, "use_micro_gate", True) and self.micro is not None and self.micro.score < self.cfg.micro.no_trade_below:
                blocked = f"market quality {self.micro.score:.0f} below entry threshold"
            if blocked is None and self.regime is not None and getattr(self.regime, "no_trade", False):
                blocked = f"regime {self.regime.label}: stand down"
            self.execution.manage_pending(blocked)
        if new_bar and not halted_by_file:
            self.candle_step()
        if self.armed is not None:
            self.refine_step(pd.Timestamp(self.client.utc_now()))
        hl = self.news.provider_health() if hasattr(self.news, "provider_health") else {}
        cal = [v for k, v in hl.items() if k.startswith("calendar:")]
        if cal and not any(v["fresh"] for v in cal) and time.time() - self._feed_alert_at > 3600:
            self._feed_alert_at = time.time()
            self.bus.publish("safety", {"kind": "NEWS_FEED_FAILURE", "level": "WARN",
                                        "detail": "no economic-calendar provider is fresh; event blackouts may be missing"})

    # ---- reliability ---------------------------------------------------
    def connection_step(self):
        """Returns True when connected AND reconciled. Never hammers MT5: attempts follow the backoff policy."""
        if not self.client.healthy():
            self.health.mt5_ok = False
            if self.reconnect.state == "CONNECTED":
                self.reconnect.on_lost()
                self.bus.publish("safety", {"kind": "CONNECTION_LOST", "detail": "MT5 connection lost; trading blocked", "level": "WARN"})
            self.set_lifecycle(Lifecycle.DEGRADED, "MT5 connection lost")
            if not self.reconnect.should_attempt():
                return False
            ok = self.client.try_reconnect()
            self.reconnect.record(ok)
            self.journal.event("reconnect_attempt", ok=ok, **self.reconnect.snapshot())
            if self.reconnect.state == "COOLDOWN":
                self.bus.publish("safety", {"kind": "RECONNECT_CEILING", "level": "CRITICAL",
                                            "detail": f"reconnect ceiling reached, cooling down {self.cfg.reliability.cooldown_s:.0f}s"})
            if not ok:
                return False
            self.reconcile_required = True
        elif self.reconnect.state != "CONNECTED":
            self.reconnect.record(True)
            self.reconcile_required = True
        if self.reconcile_required:
            if time.time() - self._t_sync < 30 and self._t_sync:
                return False
            self._t_sync = time.time()
            return self.post_reconnect_sync()
        return True

    def post_reconnect_sync(self):
        """Account, metadata, server time, positions, pending orders, candles and state must all check out before trading."""
        checks = {}
        acc = self.client.account()
        checks["account"] = acc is not None and acc.equity > 0
        self.client._refresh_offset(force=True)
        checks["server_time"] = self.client.server_time_drift_s() <= self.cfg.execution.server_time_max_drift_s
        checks["symbol_metadata"] = self.client.symbol_info() is not None
        if self.cfg.features.broker_fail_closed:
            checks["broker_diagnosis"] = self.client.broker_diagnosis().status != "FAIL"
        known = self.state.data.get("pending", {})
        pend = [o for o in self.client.orders() if str(o.ticket) not in known]
        checks["pending_orders"] = not pend                 # unknown pending orders must be investigated
        rep = self.run_reconcile()
        checks["positions"] = not rep.critical
        self.last_bar = None
        self.refresh_views(force=True)
        checks["candles"] = all(tf in self.views for tf in ("M5", "M15", "H1"))
        self.state.save()
        checks["state_saved"] = True
        ok = all(checks.values())
        self.journal.event("post_reconnect_reconciliation", ok=ok, **checks)
        if ok:
            self.reconcile_required = False
            self.bus.publish("safety", {"kind": "RECOVERED", "detail": "reconnected and reconciled: " + ", ".join(checks), "level": "INFO"})
        else:
            failed = [k for k, v in checks.items() if not v]
            self.set_lifecycle(Lifecycle.DEGRADED, "reconciliation pending: " + ", ".join(failed))
        return ok

    # ---- M1 execution refinement -------------------------------------------
    def refine_step(self, now):
        a = self.armed
        if a is None:
            return None
        ok, why = self.can_enter()
        if not ok or self.news_ctx.blackout or now >= a.expires_at:
            reason = why if not ok else ("news blackout" if self.news_ctx.blackout else "M1 window expired")
            return self._disarm("EXPIRED" if now >= a.expires_at else "CANCELLED", reason)
        # A setup is not permission to trade forever: re-check the live microstructure/regime state
        # immediately before the M1 execution decision, as a discretionary expert would.
        if getattr(self.cfg.policy, "use_micro_gate", True) and self.micro is not None and self.micro.score < self.cfg.micro.no_trade_below:
            return self._disarm("CANCELLED", f"market quality deteriorated to {self.micro.score:.0f}")
        current_regime = classify_regime(self.views, self.cfg, self.micro, self.news_ctx) if self.views else self.regime
        if current_regime is not None and getattr(current_regime, "no_trade", False):
            return self._disarm("CANCELLED", f"regime changed to {current_regime.label}: stand down")
        if current_regime is not None and a.dec.setup and a.dec.setup not in getattr(current_regime, "allowed", ()): 
            return self._disarm("CANCELLED", f"setup {a.dec.setup} no longer allowed in {current_regime.label}")
        bar = self.client.last_bar_time("M1")
        if bar is None or (bar == self._m1_bar and a.checks):
            return None
        self._m1_bar = bar
        try:
            df, _ = self.client.rates("M1", self.cfg.m1.bars)
            m1 = analyze_tf(df, "M1", self.cfg.sessions.bounds, self.cfg.strategy.adx_bands)
        except Exception as ex:
            m1 = None
            log.warning("M1 data unavailable: %s", ex)
        t = self.client.tick()
        price = t.bid if a.dec.direction > 0 else t.ask
        res = m1_check(a.dec, m1, price, self.cfg)
        a.checks += 1
        a.last_reason = res.reason
        a.history.append(res.reason)
        if res.action == "ABORT":
            return self._disarm("M1_ABORTED", res.reason)
        if res.action == "WAIT":
            return None
        positions = self.client.positions()
        ok, why = self.risk.gate(positions, self.regime.label if self.regime else None)
        if not ok:
            return self._disarm("CANCELLED", "risk gate at entry: " + why)
        out = self.execution.execute(a.dec, a.ms, positions, self.news_ctx)
        self.armed = None
        if out.executed and self.v5_brain is not None:
            det = out.details or {}
            self.v5_brain.reporter.trade_entry_feedback({
                "symbol": self.client.symbol, "direction": a.dec.direction, "setup": a.dec.setup,
                "entry": (det.get("execution") or {}).get("fill", a.dec.entry), "sl": a.dec.sl, "tp": a.dec.tp,
                "lots": out.lots, "risk_pct": a.dec.risk_pct, "rr": a.dec.rr, "mode": self.cfg.system.mode,
                "ticket": out.ticket or (det.get("execution") or {}).get("ticket", 0)
            })
        det = out.details or {}
        stage = "EXECUTED" if out.executed else "EXECUTION_REJECTED"
        self.bus.publish("record", records.build(a.dec, a.ms, self.news_ctx, self.cfg, self.client.symbol, stage,
                                                  det.get("risk_authorization"), {"status": "GO", "reason": res.reason, "checks": a.checks},
                                                  det.get("execution")))
        log.info("M1 refinement GO (%s): %s", res.reason, out.reason)
        return out

    def _disarm(self, stage, reason):
        a, self.armed = self.armed, None
        a.dec.wait_reason = "M1_REFINEMENT"
        self.bus.publish("record", records.build(a.dec, a.ms, self.news_ctx, self.cfg, self.client.symbol, stage,
                                                  refine={"status": stage, "reason": reason, "checks": a.checks, "history": a.history[-5:]}))
        log.info("armed decision %s: %s", stage.lower(), reason)
        return None

    # ---- candle loop: new trade evaluation ----------------------------
    def can_enter(self):
        if self.lifecycle != Lifecycle.RUNNING:
            return False, f"engine {self.lifecycle.value}"
        if self.state.needs_recovery:
            return False, "state recovery pending"
        if self.recon_critical:
            return False, "unresolved reconciliation problem"
        if self.reconcile_required:
            return False, "post-reconnect reconciliation pending"
        if getattr(self.cfg.news, "enabled", True) and getattr(self.cfg.news, "calendar_require_fresh_for_entries", True) and getattr(self.cfg.features, "require_fresh_calendar_entries", True) and self.cfg.features.news_redundancy:
            n = self.news_ctx
            if n is not None and not getattr(n, "calendar_ok", False):
                return False, "economic calendar unavailable/stale: entries fail closed"
        if self.armed is not None:
            return True, "ok"
        missing = [tf for tf in ("M5", "M15", "H1") if tf not in self.views]
        if missing:
            return False, f"missing market data for {','.join(missing)}"
        return True, "ok"      # styles whose timeframes are missing (e.g. W1 history) are skipped individually

    def _apply_profile(self):
        """V5.1: instrument profile -> calendar currencies, news direction, intermarket signs, weekend rule."""
        from instruments import profile_for
        try:
            info = self.client.symbol_info()
        except Exception:
            info = None
        self.profile = profile_for(self.client.symbol, info)
        if hasattr(self.news, "set_instrument"):
            self.news.set_instrument(self.profile)
        if getattr(self, "macro", None) is not None:
            self.macro.profile = self.profile
        self.risk.core.trades_weekends = self.profile.trades_weekends
        self.journal.event("instrument_profile", **self.profile.to_dict())
        log.info("instrument profile: %s %s calendar=%s", self.profile.symbol, self.profile.asset_class,
                 ",".join(self.profile.calendar_currencies))

    def _dynamic_spread_cap(self):
        prof = getattr(self, "profile", None)
        if prof is None or prof.asset_class not in ("index", "energy", "crypto") or not self.views.get("M5"):
            self.risk.core.spread_cap_points = None
            return
        try:
            point = float(self.client.symbol_info().point) or 1e-9
            self.risk.core.spread_cap_points = self.cfg.execution.max_spread_atr_frac * float(self.views["M5"]["atr"]) / point
        except Exception:
            self.risk.core.spread_cap_points = None

    def _adaptive_market_select(self):
        """Select the best account-affordable market before entry analysis.

        Symbol switching is only allowed while the agent has no managed positions/pending order.
        Once a market is selected, the normal pipeline is rebuilt for that symbol and then the
        complete decision/news/intermarket/critic/chief/risk/execution chain runs unchanged.
        """
        if self.v5_brain is None or not getattr(self.cfg.brain, "adaptive_market_selection", True):
            return False
        if getattr(self.cfg.brain, "adaptive_selection_flat_only", True):
            try:
                if self.client.all_positions():
                    return False
            except Exception:
                try:
                    if self.client.positions():
                        return False
                except Exception:
                    return False
            if self.state.data.get("pending") or self.state.data.get("pending_sim"):
                return False
        try:
            result = self.v5_brain.select_market_if_due(primary_views=self.views, allow_switch=True)
            if not result or not result.get("changed"):
                return False
            selected = result.get("selected")
            if not selected:
                return False
            # A symbol switch invalidates every symbol-local cache/profile.
            self.views.clear()
            self.raw_frames.clear()
            self._cache.clear()
            self._tf_last.clear()
            self._tf_retry.clear()
            self.last_bar = None
            self.regime = None
            self.micro = None
            self.armed = None
            self._apply_profile()
            self.refresh_views(force=True)
            self.news_ctx = self.news.get(vol_probe=self.monitor.vol_probe)
            if self.macro is not None:
                try:
                    self.macro.profile = self.profile
                    self.macro.refresh()
                except Exception:
                    pass
            self.journal.event("adaptive_market_selected", symbol=selected, reason=result.get("reason", ""),
                               ranking=[{"symbol": r.symbol, "score": r.ranking_score, "affordable": r.affordable}
                                        for r in (result.get("rows") or [])[:5]])
            self.alerts.send("INFO", f"Adaptive market selector chose {selected} as the best account-affordable candidate", key="adaptive_select:" + selected)
            return True
        except Exception as ex:
            log.exception("adaptive market selection failed; retaining current market: %s", ex)
            return False

    def candle_step(self):
        cfg = self.cfg
        self._adaptive_market_select()
        ok, why = self.can_enter()
        if not ok:
            log.info("no new trades: %s", why)
            return None
        positions = self.client.positions()
        now = self.client.utc_now()
        tick = self.client.tick()
        mid = (tick.ask + tick.bid) / 2
        reg = classify_regime(self.views, cfg, self.micro, self.news_ctx)
        if self.regime is None or reg.label != self.regime.label:
            self.journal.event("regime", label=reg.label, direction=reg.direction, reasons=reg.reasons)
            if self.regime is not None:
                self.alerts.send("INFO", f"Regime {self.regime.label} -> {reg.label}: {'; '.join(reg.reasons)}", key="regime" + reg.label)
        self.regime = reg
        self._dynamic_spread_cap()
        ok, why = self.risk.gate(positions, reg.label)
        if not ok:
            log.info("no new trades: %s", why)
            self.wait_counts["RISK_GATE"] += 1
            return None
        ms = build_state(self.views, now, cfg.sessions.bounds, self.client.spread_price(), cfg.sessions.phases,
                         regime=reg, micro=self.micro, liquidity=build_map(self.views, mid), news=self.news_ctx)
        open_trades = list(self.state.trades.values())
        exclude = {(t["style"], t["direction"]) for t in open_trades}
        for d in {t["direction"] for t in open_trades}:
            exclude |= {(s, -d) for s in cfg.strategy.styles}
        aux = None
        if self.macro is not None:
            try:
                aux = self.macro.refresh()
            except Exception as ex:
                log.warning("macro feeds unavailable: %s", ex)
        ms.aux = aux
        dec = decide(ms, mid, self.news_ctx, cfg, exclude, self.calibration, self.edge_book, aux, self.meta_model)
        self.last_decision = dec
        if self.shadow is not None:
            try:
                self.shadow.evaluate(ms, mid, self.news_ctx, exclude, dec, self.calibration, self.edge_book, aux)
            except Exception as ex:
                log.warning("shadow evaluation failed: %s", ex)
        log.info("decision: %s %s %s %s score=%s regime=%s mq=%.0f | %s", dec.action, dec.wait_reason or dec.setup,
                 dec.style, dec.direction or "", dec.score, reg.label, dec.market_quality, "; ".join(dec.reasons[:4]))
        out = None
        v5_run = None
        if self.v5_brain is not None:
            try:
                v5_run = self.v5_brain.run(lifecycle=self.lifecycle.value, ms=ms, decision=dec,
                                           news_ctx=self.news_ctx, positions=positions, frames=self.raw_frames,
                                           aux=aux, force_health=False,
                                           reconciliation_ok=not bool(self.recon_critical),
                                           state_recovery_pending=bool(self.state.needs_recovery or self.reconcile_required))
                # Advisory by default. When explicitly enforced, V5 can only veto or reduce a deterministic candidate.
                if self.v5_brain.enforce and dec.is_entry:
                    if v5_run.decision.permission in ("BLOCK", "NO_TRADE"):
                        dec.action, dec.wait_reason = "WAIT", "V5_BRAIN"
                        dec.reasons = list(dec.reasons) + ["V5 Brain veto: " + "; ".join(v5_run.decision.blockers[:3] or v5_run.decision.reasons[:3])]
                    elif v5_run.decision.permission == "REDUCE":
                        reduced = max(0.0, min(float(dec.risk_pct), float(dec.risk_pct) * v5_run.decision.risk_multiplier))
                        if reduced < dec.risk_pct:
                            dec.risk_pct = reduced
                            dec.reasons = list(dec.reasons) + [f"V5 Brain reduced risk to {reduced:.3f}%"]
            except Exception as ex:
                # AI/reporting must never break deterministic trading. Record and continue.
                log.exception("V5 brain workflow failed (deterministic kernel continues): %s", ex)
        if dec.is_entry:
            from risk import calendar_risk_multiplier
            m, why = calendar_risk_multiplier(cfg, getattr(self.news_ctx, "calendar_configured_families", 1),
                                              getattr(self.news_ctx, "calendar_remote_families", 1))
            if m is None:
                dec.action, dec.wait_reason = "WAIT", "NEWS"
                dec.reasons = list(dec.reasons) + [why]
            elif m < 1.0:
                dec.risk_pct = round(dec.risk_pct * m, 4)
                dec.reasons = list(dec.reasons) + [why]
        if dec.is_entry and cfg.features.m1_refinement:
            if self.armed is None:
                ts = pd.Timestamp(now)
                self.armed = Armed(dec, ms, ts, ts + pd.Timedelta(minutes=cfg.m1.window_min))
                self._m1_bar = None
                self.bus.publish("record", records.build(dec, ms, self.news_ctx, cfg, self.client.symbol, "DECISION",
                                                          refine={"status": "ARMED", "window_min": cfg.m1.window_min}))
                self.journal.decision(dec, 0.0, "armed for M1 refinement")
                self.refine_step(ts)
            return dec
        if dec.is_entry:
            out = self.execution.execute(dec, ms, positions, self.news_ctx)
            if out.executed and self.v5_brain is not None:
                det = out.details or {}
                self.v5_brain.reporter.trade_entry_feedback({
                    "symbol": self.client.symbol, "direction": dec.direction, "setup": dec.setup,
                    "entry": (det.get("execution") or {}).get("fill", dec.entry), "sl": dec.sl, "tp": dec.tp,
                    "lots": out.lots, "risk_pct": dec.risk_pct, "rr": dec.rr, "mode": cfg.system.mode,
                    "ticket": out.ticket or (det.get("execution") or {}).get("ticket", 0)
                })
            if not out.executed:
                dec.wait_reason = "EXECUTION"
            log.info("execution: %s", out.reason)
            det = out.details or {}
            self.bus.publish("record", records.build(dec, ms, self.news_ctx, cfg, self.client.symbol,
                                                      "EXECUTED" if out.executed else "EXECUTION_REJECTED",
                                                      det.get("risk_authorization"), None, det.get("execution")))
        else:
            self.wait_counts[f"{dec.action}:{dec.wait_reason}"] += 1
            self.bus.publish("record", records.build(dec, ms, self.news_ctx, cfg, self.client.symbol, "DECISION"))
        self.journal.decision(dec, out.lots if out else 0.0, out.reason if out else "")
        return dec

    # ---- periodic ----------------------------------------------------
    def heartbeat(self):
        snap = self.health.snapshot(self.lifecycle.value, self.client, self.state, self.risk)
        snap.update(reconnect=self.reconnect.snapshot(), mt5_pulls=getattr(self.client, "pulls", None),
                    broker_diag=getattr(getattr(self.client, "diag", None), "status", None),
                    armed=bool(self.armed), model_versions=self.versions,
                    providers=self.news.provider_health() if hasattr(self.news, "provider_health") else {},
                    regime=self.regime.label if self.regime else None,
                    market_quality=round(self.micro.score) if self.micro else None,
                    event_phase=self.news_ctx.event_phase, calendar_state=getattr(self.news_ctx, "calendar_state", None),
                    event_label=getattr(self.news_ctx, "event_label", None), instrument=getattr(self, "profile", None) and self.profile.asset_class,
                    calendar_age_h=getattr(self.news_ctx, "calendar_cache_age_h", None),
                    news_age_h=(round(max(0.0, (time.time() - getattr(self.news, "heads_at", 0)) / 3600), 3)
                                if getattr(self.news, "heads_at", 0) else None),
                    waits=dict(self.wait_counts.most_common(6)))
        if self.v5_brain is not None and self.lifecycle == Lifecycle.RUNNING:
            try:
                self.v5_brain.scan_universe_if_due(primary_views=self.views)
            except Exception as ex:
                log.warning("V5 universe scan failed: %s", ex)
        log.info("heartbeat %s", snap)
        self.journal.event("heartbeat", **snap)
        why = self.health.degraded_reasons(snap.get("tick_age_s", 0), self.cfg.execution.max_tick_age_s)
        if why and self.lifecycle == Lifecycle.RUNNING and not all(w.startswith("news") for w in why):
            self.alerts.send("WARN", "health: " + "; ".join(why), key="health")

    def routine_reports(self, now):
        """V5.1: Phase A pre-market If-Then plan (daily) and Phase C weekly review (Saturday), as in the documents."""
        nc = self.cfg.notify
        today = str(now.date())
        try:
            if nc.premarket_plan and now.hour >= nc.premarket_hour_utc and getattr(self, "_plan_day", None) != today \
                    and now.weekday() < 5 and self.views:
                from premarket import build_plan
                self._plan_day = today
                plan = build_plan(self.views, self.news, getattr(self, "profile", None), now)
                pdir = Path(self.cfg.system.data_dir) / "premarket"
                pdir.mkdir(parents=True, exist_ok=True)
                (pdir / f"{today}.txt").write_text(plan)
                self.alerts.send("INFO", plan[:3500], key="premarket" + today)
            if nc.weekly_review and now.weekday() == nc.weekly_review_weekday and getattr(self, "_review_day", None) != today:
                from premarket import weekly_review
                self._review_day = today
                txt = weekly_review(self.journal.read_jsonl(self.journal.trades_path), now)
                self.alerts.send("INFO", txt[:3500], key="weekly" + today)
        except Exception as ex:
            log.warning("routine report failed: %s", ex)

    def daily_report(self, now):
        today = str(now.date())
        self.routine_reports(now)
        if self._report_day == today or now.hour != self.cfg.emergency.daily_report_hour_utc:
            return
        self._report_day = today
        if self.notifier is not None and self.cfg.notify.daily_report:
            recs = self.journal.read_jsonl(self.journal.dir / "decision_records.jsonl")
            st = daily_stats(self.journal.read_jsonl(self.journal.trades_path), recs, today)
            hs = self.health.snapshot(self.lifecycle.value, self.client, self.state, self.risk)
            self.bus.publish("daily", {"stats": st, "health": {k: hs.get(k) for k in ("lifecycle", "uptime_h", "errors", "equity")}})
            self.drift_check()
            return
        closes = [r for r in self.journal.read_jsonl(self.journal.trades_path)
                  if r.get("type") == "close" and r.get("t", "").startswith(today)]
        net = sum(r.get("net_pnl", 0) for r in closes)
        wins = sum(1 for r in closes if r.get("net_pnl", 0) > 0)
        self.alerts.send("INFO", f"Daily report {today}: {len(closes)} closed, {wins} wins, net {net:+.2f}, "
                                 f"equity {self.client.account().equity:.2f}", key="daily" + today)

    def drift_check(self):
        """Compare recent live outcomes with what the production model expected. Drift never rewrites anything:
        it alerts and (optionally) falls back to the default, un-learned behaviour until a human reviews it."""
        if not (self.cfg.features.drift_monitor and self.calibration is not None):
            return None
        from drift import drift_report
        closes = [r for r in self.journal.read_jsonl(self.journal.trades_path) if r.get("type") == "close"][-200:]
        recs = self.journal.read_jsonl(self.journal.dir / "decision_records.jsonl")[-3000:]
        rep = drift_report(self.calibration.m, closes, recs, self.cfg.learning)
        self.journal.event("drift_check", **{k: v for k, v in rep.items() if k != "details"})
        if rep["status"] == "DRIFT":
            self.bus.publish("safety", {"kind": "MODEL_DRIFT", "level": "WARN", "detail": "; ".join(rep["findings"])})
            if self.cfg.features.drift_auto_safe:
                self.calibration, self.edge_book = None, None
                self.journal.event("drift_safe_mode", versions=self.versions)
        return rep

    def step(self):
        """One guarded iteration; returns False if the engine must stop."""
        try:
            self.fast_step()
            self.health.loop_ok()
            now = self.client.utc_now()
            if time.time() - self._t_hb >= self.cfg.emergency.heartbeat_s:
                self._t_hb = time.time()
                self.heartbeat()
            self.daily_report(now)
        except Exception as ex:
            log.exception("loop error")
            self.health.loop_error()
            if self.health.consecutive_errors >= self.cfg.emergency.max_consecutive_errors:
                self.set_lifecycle(Lifecycle.HALTED, f"{self.health.consecutive_errors} consecutive loop errors: {ex}")
        return not self.stop_flag.is_set()

    # ---- run ---------------------------------------------------------
    def _news_worker(self):
        while not self.stop_flag.is_set():
            try:
                self.news.refresh()
            except Exception:
                log.exception("news worker error")
            self.stop_flag.wait(self.cfg.system.news_loop_s)

    def run(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, lambda *_: self.stop_flag.set())
            except ValueError:
                pass
        self.startup()
        threading.Thread(target=self._news_worker, daemon=True, name="news").start()
        while True:
            t0 = time.time()
            if not self.step():
                break
            self.stop_flag.wait(max(0.0, self.cfg.system.fast_loop_s - (time.time() - t0)))
        self.shutdown()

    def shutdown(self):
        self.set_lifecycle(Lifecycle.SHUTDOWN)
        self.state.save()
        self.journal.event("shutdown", positions=len(self.client.positions()))
        self.alerts.send("INFO", "Agent stopped. Open positions keep their stop loss and take profit on the broker.", key="stop")
        self.alerts.flush()
        if self.v5_brain is not None:
            self.v5_brain.reporter.close()
        self.client.shutdown()
