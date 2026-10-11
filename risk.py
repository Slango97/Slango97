import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from diagnostics import diagnose
from portfolio import PortfolioRisk

log = logging.getLogger("risk")


@dataclass
class Approval:
    ok: bool
    lots: float = 0.0
    risk_pct: float = 0.0
    reason: str = ""
    details: dict = field(default_factory=dict)


def configured_calendar_families(cfg):
    """Remote calendar vendor families that are configured AND usable (placeholders like ${KEY} need the variable)."""
    import os
    import re
    fams = set()
    for spec in cfg.news.calendar_providers:
        name, kind, src = spec[0], spec[1], str(spec[2])
        if kind == "file" or [e for e in re.findall(r"\$\{([A-Z0-9_]+)\}", src) if not os.getenv(e)]:
            continue
        fams.add(spec[3] if len(spec) > 3 else name)
    return len(fams) if cfg.features.news_redundancy else 1


def calendar_risk_multiplier(cfg, configured, working):
    """(multiplier, reason). multiplier None = entries blocked. Never increases risk."""
    nc = cfg.news
    single = working <= 1
    if single and not nc.calendar_allow_single_provider:
        return None, "single calendar vendor and calendar_allow_single_provider=false"
    mode = nc.calendar_single_provider_mode
    applies = single and (mode == "always" or (mode == "degraded_only" and configured >= 2))
    if not applies:
        return 1.0, ""
    m = min(1.0, nc.calendar_single_provider_risk_mult)
    return m, f"calendar redundancy {'lost' if configured >= 2 else 'unavailable'} ({working}/{configured} vendors): risk x{m:.2f}"


class RiskCore:
    """Pure risk rules (no MT5 calls) so live trading and the backtester behave identically."""

    def __init__(self, cfg):
        self.r, self.x, self.s = cfg.risk, cfg.execution, cfg.sessions

    def time_gate(self, now):
        if now.weekday() >= 5 and not getattr(self, 'trades_weekends', False):
            return False, "weekend"
        if now.hour in self.s.avoid_hours_utc:
            return False, f"rollover hours ({now.hour}:00 UTC)"
        if not getattr(self, 'trades_weekends', False) and now.weekday() == 4 and now.hour >= self.s.no_new_trades_friday_after_utc:
            return False, "late Friday"
        return True, "ok"

    def gate(self, now, spread_points, n_open, trades_today, day_loss_pct, week_loss_pct, dd_pct,
             open_risk_pct, unprotected=0, paused_until=None):
        ok, why = self.time_gate(now)
        if not ok:
            return False, why
        if unprotected:
            return False, f"{unprotected} position(s) without stop loss"
        if paused_until and now < paused_until:
            return False, f"paused after loss streak until {paused_until:%H:%M} UTC"
        cap = getattr(self, "spread_cap_points", None) or self.x.max_spread_points
        if self.x.session_spread_caps:
            from config import phases_for, session_phase
            cap = self.x.session_spread_caps.get(session_phase(now, phases_for(self.s)), cap)
        if spread_points > cap:
            return False, f"spread {spread_points:.0f} pts too wide (cap {cap})"
        if n_open >= self.r.max_open_trades:
            return False, "max open trades"
        if trades_today >= self.r.max_trades_per_day:
            return False, "max trades today"
        if day_loss_pct >= self.r.max_daily_loss_pct:
            return False, f"daily loss limit ({day_loss_pct:.2f}%)"
        if week_loss_pct >= self.r.max_weekly_loss_pct:
            return False, f"weekly loss limit ({week_loss_pct:.2f}%)"
        if dd_pct >= self.r.max_drawdown_pct:
            return False, f"drawdown halt ({dd_pct:.2f}%), review manually"
        if open_risk_pct >= self.r.max_open_risk_pct:
            return False, "open risk cap reached"
        return True, "ok"

    def risk_multiplier(self, loss_streak):
        return self.r.loss_streak_reduce_factor if loss_streak >= self.r.loss_streak_reduce_at else 1.0

    @staticmethod
    def lots_for(risk_money, loss_per_lot, step, vmin, vmax):
        """Largest volume within the risk budget; 0.0 if even the minimum lot is far over budget."""
        if loss_per_lot <= 0 or risk_money <= 0:
            return 0.0
        lots = round(math.floor(risk_money / loss_per_lot / step + 1e-9) * step, 8)
        lots = min(lots, vmax)
        if lots < vmin:
            # V4.1 fail-closed: never exceed the requested risk budget merely because the broker minimum lot is large.
            return 0.0
        return lots

    def projected_ok(self, open_risk_pct, new_risk_pct):
        return open_risk_pct + new_risk_pct <= self.r.max_open_risk_pct + 1e-9

    def throttle(self, now, paused_until, diag, regime_label):
        """After a pause expires, keep pausing while the regime that produced the losses is still active."""
        if paused_until is None or now < paused_until or not diag:
            return paused_until
        dom = diag.get("dominant_regime")
        if dom and regime_label == dom:
            return now + timedelta(hours=self.r.throttle_extend_h)
        return None


class RiskEngine:
    def __init__(self, cfg, client, state, journal=None, on_pause=None):
        self.cfg, self.c, self.state, self.journal = cfg, client, state, journal
        self.core = RiskCore(cfg)
        self.portfolio = PortfolioRisk(cfg, client)
        self.on_pause = on_pause

    def refresh(self):
        acc = self.c.account()
        if acc is None or getattr(acc, "equity", 0) is None or float(getattr(acc, "equity", 0) or 0) <= 0:
            log.warning("risk refresh skipped: account snapshot unavailable")
            return False
        eq = float(acc.equity)
        now = self.c.utc_now()
        r = self.state.risk
        today = str(now.date())
        week = str((now - timedelta(days=now.weekday())).date())
        if r.get("day") != today:
            r.update(day=today, day_start_equity=eq, trades_today=0)
        if r.get("week") != week:
            r.update(week=week, week_start_equity=eq)
        r["peak"] = max(r.get("peak", eq), eq)
        r.setdefault("loss_streak", 0)
        self.state.save()
        return True

    def _ensure(self):
        """Counters are created lazily so no caller can hit a missing baseline."""
        if not all(k in self.state.risk for k in ("day_start_equity", "week_start_equity", "peak")):
            self.refresh()

    def loss_per_lot(self, direction, entry, sl):
        return self.c.calc_loss_per_lot(direction, entry, sl)

    @staticmethod
    def unprotected(positions):
        return [p for p in positions if not p.sl]

    def hard_limits(self, positions, extra_risk_pct=0.0):
        """Risk limits that must hold even for an order authorised earlier (no clock windows, no trade counter)."""
        self._ensure()
        r, eq = self.state.risk, self.c.account().equity
        rl = self.cfg.risk
        if self.unprotected(positions):
            return False, "unprotected position present"
        if self.paused_until() and self.c.utc_now() < self.paused_until():
            return False, "loss-streak pause active"
        for name, base, cap in (("daily loss", r["day_start_equity"], rl.max_daily_loss_pct),
                                ("weekly loss", r["week_start_equity"], rl.max_weekly_loss_pct),
                                ("drawdown", r["peak"], rl.max_drawdown_pct)):
            if 100 * (base - eq) / base >= cap:
                return False, f"{name} limit reached"
        if self.reserved_risk_pct(positions) + extra_risk_pct > rl.max_open_risk_pct * 1.05 + 1e-9:
            return False, "open-risk cap would be exceeded"
        return True, "ok"

    def _shared(self):
        import json as _json
        from pathlib import Path as _P
        p = _P(self.cfg.risk.shared_state_file)
        try:
            return _json.loads(p.read_text()) if p.exists() else {}
        except Exception:
            return {}

    def _shared_save(self, data):
        import json as _json
        import os as _os
        from pathlib import Path as _P
        p = _P(self.cfg.risk.shared_state_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(_json.dumps(data))
        _os.replace(tmp, p)

    def pending_reserved_risk_pct(self):
        total = 0.0
        for p in self.state.data.get("pending", {}).values():
            try:
                total += max(0.0, float((p.get("auth") or {}).get("risk_pct", 0.0)))
            except Exception:
                continue
        return total

    def reserved_risk_pct(self, positions):
        return self.open_risk_pct(positions) + self.pending_reserved_risk_pct()

    def open_risk_pct(self, positions):
        eq = self.c.account().equity
        total = 0.0
        for p in positions:
            if not p.sl:
                continue
            d = 1 if p.type == 0 else -1
            if (p.price_open - p.sl) * d > 0:     # stop still on the losing side of entry
                total += p.volume * self.loss_per_lot(d, p.price_open, p.sl)
        return 100 * total / eq

    def paused_until(self):
        raw = self.state.risk.get("paused_until")
        return datetime.fromisoformat(raw) if raw else None

    def gate(self, positions, regime_label=None):
        if self.cfg.features.doc_rules:             # V5.1 machine-enforced psychology protocol (documents)
            now, rr = self.c.utc_now(), self._shared()
            if rr.get("locked_day") == now.date().isoformat():
                return False, "two-loss rule: two consecutive full stop-outs; trading locked until tomorrow (UTC)"
            cu = rr.get("cooldown_until")
            if cu and now < datetime.fromisoformat(cu):
                return False, f"15-minute cooldown after a loss (until {datetime.fromisoformat(cu):%H:%M} UTC)"
        self._ensure()
        r, eq = self.state.risk, self.c.account().equity
        if r.get("paused_until"):
            new = self.core.throttle(self.c.utc_now(), self.paused_until(), r.get("pause_diag"), regime_label)
            if new is None:
                r.pop("paused_until", None)
                r.pop("pause_diag", None)
                if self.journal:
                    self.journal.event("throttle_resume", regime=regime_label)
            elif new != self.paused_until():
                r["paused_until"] = new.isoformat()
                if self.journal:
                    self.journal.event("throttle_extended", regime=regime_label, until=r["paused_until"])
        day0, wk0, peak = r["day_start_equity"], r["week_start_equity"], r["peak"]
        return self.core.gate(
            self.c.utc_now(), self.c.spread_points(), len(positions), r.get("trades_today", 0),
            100 * (day0 - eq) / day0, 100 * (wk0 - eq) / wk0, 100 * (peak - eq) / peak,
            self.reserved_risk_pct(positions), len(self.unprotected(positions)), self.paused_until())

    def drawdown_pct(self):
        self._ensure()
        r, eq = self.state.risk, self.c.account().equity
        return 100 * (r["peak"] - eq) / r["peak"]

    def approve(self, dec, price, positions):
        """Size the trade and verify it fits every limit INCLUDING its own risk and margin."""
        acc, d = self.c.account(), dec.direction
        eq = acc.equity
        mult = self.core.risk_multiplier(self.state.risk.get("loss_streak", 0))
        risk_pct = dec.risk_pct * mult
        # Account-aware risk ceiling. This makes micro/small accounts usable without
        # ever allowing a percentage above the configured 1-2% discipline.
        try:
            eqf = float(eq)
            if eqf <= self.cfg.risk.micro_account_max_equity:
                account_cap = self.cfg.risk.micro_account_risk_pct
            elif eqf <= self.cfg.risk.small_account_max_equity:
                account_cap = self.cfg.risk.small_account_risk_pct
            else:
                account_cap = self.cfg.risk.risk_per_trade_pct
            risk_pct = min(risk_pct, account_cap)
        except Exception:
            pass
        try:
            lpl = self.loss_per_lot(d, price, dec.sl)
        except Exception as ex:                       # fail closed on broker calculation problems
            return Approval(False, reason=f"broker profit calculation unavailable: {ex}")
        info = self.c.symbol_info()
        lots = self.core.lots_for(eq * risk_pct / 100, lpl, info.volume_step, info.volume_min, info.volume_max)
        det = {"risk_pct_requested": risk_pct, "loss_per_lot": lpl, "streak_mult": mult,
               "account_risk_cap_pct": account_cap if 'account_cap' in locals() else None,
               "account_equity": float(eq)}
        if lots <= 0:
            return Approval(False, reason="minimum lot exceeds risk budget", details=det)
        actual = 100 * lots * lpl / eq
        open_risk = self.reserved_risk_pct(positions)
        det.update(risk_pct_actual=actual, open_risk_pct=open_risk, projected_risk_pct=open_risk + actual)
        if not self.core.projected_ok(open_risk, actual):
            return Approval(False, reason=f"projected open risk {open_risk + actual:.2f}% exceeds "
                                          f"{self.cfg.risk.max_open_risk_pct}%", details=det)
        margin = self.c.calc_margin(d, lots, price)
        all_pos = self.c.all_positions() if hasattr(self.c, "all_positions") else positions
        try:
            ok, before, after, factors = self.portfolio.check(getattr(self.c, "symbol", "XAUUSD"), d, actual, all_pos)
        except Exception as ex:
            return Approval(False, reason=f"portfolio risk calculation unavailable: {ex}", details=det)
        det.update(portfolio_risk_before=before, portfolio_risk_after=after, factor_exposure=factors)
        if not ok:
            return Approval(False, reason=f"correlated portfolio risk {after:.2f}% exceeds "
                                          f"{self.cfg.portfolio.max_correlated_exposure_pct}%", details=det)
        if margin is not None:
            free_after = acc.margin_free - margin
            used_after = acc.margin + margin
            level_after = 100 * eq / used_after if used_after > 0 else 1e9
            det.update(margin_required=margin, free_margin_after=free_after, margin_level_after=level_after)
            if free_after < eq * self.cfg.risk.min_free_margin_pct / 100:
                return Approval(False, reason="insufficient free margin after entry", details=det)
            if level_after < self.cfg.risk.min_margin_level:
                return Approval(False, reason=f"margin level would fall to {level_after:.0f}%", details=det)
        return Approval(True, lots=lots, risk_pct=actual, details=det)

    def note_trade(self):
        self.state.risk["trades_today"] = self.state.risk.get("trades_today", 0) + 1
        self.state.save()

    def record_result(self, net_pnl, r_multiple=None, rec=None):
        """Update the loss streak after a closed trade; after a long streak pause entries and diagnose why."""
        r = self.state.risk
        hist = r.setdefault("recent", [])
        hist.append({**(rec or {}), "net": net_pnl, "r": r_multiple})
        del hist[:-30]
        if net_pnl < 0:
            r["loss_streak"] = r.get("loss_streak", 0) + 1
            if r["loss_streak"] >= self.cfg.risk.loss_streak_pause_at:
                until = datetime.now(timezone.utc) + timedelta(hours=self.cfg.risk.pause_hours)
                r["paused_until"] = until.isoformat()
                streak = hist[-r["loss_streak"]:]
                diag = diagnose(streak, hist, self.cfg.risk.throttle_dominance)
                r["pause_diag"] = diag
                log.warning("loss streak %s: entries paused until %s; %s", r["loss_streak"], until, diag["findings"])
                if self.journal:
                    self.journal.event("throttle_pause", streak=r["loss_streak"], until=r["paused_until"], **diag)
                if self.on_pause:
                    self.on_pause(r["loss_streak"], diag)
        elif net_pnl > 0:
            r["loss_streak"] = 0
        if self.cfg.features.doc_rules:
            now = self.c.utc_now()
            r = self._shared()                          # one trader: the rule counts across every market instance
            if net_pnl < 0:
                r["cooldown_until"] = (now + timedelta(minutes=self.cfg.risk.loss_cooldown_min)).isoformat()
                full = r_multiple is None or r_multiple <= -self.cfg.risk.full_loss_r
                r["full_loss_streak"] = r.get("full_loss_streak", 0) + 1 if full else 0
                if r["full_loss_streak"] >= self.cfg.risk.two_loss_lock_after:
                    r["locked_day"], r["full_loss_streak"] = now.date().isoformat(), 0
                    log.warning("two-loss rule: two consecutive full stop-outs; trading locked until tomorrow (UTC)")
                    if self.journal:
                        self.journal.event("two_loss_lock", day=r["locked_day"])
            elif net_pnl > 0:
                r["full_loss_streak"] = 0
            self._shared_save(r)
        self.state.save()
