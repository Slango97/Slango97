import logging
from dataclasses import dataclass

from reconciliation import make_comment

log = logging.getLogger("exec")


@dataclass
class Outcome:
    executed: bool
    reason: str = ""
    lots: float = 0.0
    dry_run: bool = False
    ticket: int = 0
    details: dict = None


PENDING_FIELDS = ("direction", "style", "setup", "score", "entry", "sl", "tp", "rr", "risk_pct", "session", "vol_regime", "regime",
                  "risk_tier", "market_quality", "conflict", "p_win", "exp_r", "meta_p", "target_kind", "htf_bias",
                  "components", "tags")


class ExecutionEngine:
    """Turns an ALLOWED decision into a position: re-validates the market, sizes with the risk engine,
    sends and verifies the order, and guarantees the position ends up with a stop loss."""

    def __init__(self, cfg, client, risk, manager, journal, alerts, monitor=None):
        self.cfg, self.c, self.risk, self.mgr, self.journal, self.alerts = cfg, client, risk, manager, journal, alerts
        self.monitor = monitor

    def execute(self, dec, ms, positions, news=None):
        ex, S = self.cfg.execution, self.cfg.strategy.styles[dec.style]
        d, atr = dec.direction, ms.views[S.tf]["atr"]
        self.journal.event("lifecycle", state="SIGNAL", style=dec.style, setup=dec.setup, score=dec.score, direction=d)

        t = self.c.tick()
        if t is None or self.c.tick_age_s() > ex.max_tick_age_s:
            return Outcome(False, "no fresh tick")
        drift_s = self.c.server_time_drift_s() if hasattr(self.c, "server_time_drift_s") else 0.0
        if drift_s > getattr(ex, "server_time_max_drift_s", 10.0):
            return Outcome(False, f"server time drift {drift_s:.1f}s exceeds {ex.server_time_max_drift_s:.1f}s")
        if getattr(ms, "micro", None) is not None and getattr(self.cfg.policy, "use_micro_gate", True):
            if ms.micro.score < self.cfg.micro.no_trade_below:
                return Outcome(False, f"market quality {ms.micro.score:.0f} below entry threshold")
        current_regime = getattr(ms, "regime", None)
        if current_regime is not None and getattr(current_regime, "no_trade", False):
            return Outcome(False, f"regime {current_regime.label}: stand down")
        if news is not None:
            if getattr(news, "blackout", False):
                return Outcome(False, "news blackout")
            if (getattr(self.cfg.news, "calendar_require_fresh_for_entries", True) and
                getattr(self.cfg.features, "require_fresh_calendar_entries", True) and
                getattr(self.cfg.features, "news_redundancy", False) and
                not getattr(news, "calendar_ok", False)):
                return Outcome(False, "economic calendar unavailable/stale")
        price = t.ask if d > 0 else t.bid
        if abs(price - dec.entry) > ex.max_entry_drift_atr * atr + (t.ask - t.bid):
            return Outcome(False, f"price drifted {abs(price - dec.entry):.2f} since the decision")
        if (price - dec.sl) * d <= 0 or (dec.tp - price) * d <= 0:
            return Outcome(False, "price already beyond stop or target")
        dist = abs(price - dec.sl)
        spread = float(t.ask - t.bid)
        if not (ex.min_stop_atr * atr * 0.9 <= dist <= ex.max_stop_atr * atr * 1.05) or dist < self.c.min_stop_distance():
            return Outcome(False, f"stop distance {dist:.2f} outside allowed range")
        if spread <= 0 or spread / dist > ex.max_spread_risk_frac:
            return Outcome(False, f"spread/stop cost {spread / max(dist, 1e-9):.2%} exceeds {ex.max_spread_risk_frac:.2%}")
        if abs(dec.tp - price) / dist < S.min_rr * 0.9:
            return Outcome(False, "reward/risk fell below minimum at current price")

        appr = self.risk.approve(dec, price, positions)
        auth = {"ok": appr.ok, "reason": appr.reason, "lots": appr.lots, "risk_pct": round(appr.risk_pct, 4),
                "tier": dec.risk_tier, **{k: v for k, v in appr.details.items() if not isinstance(v, dict)}}
        if not appr.ok:
            return Outcome(False, appr.reason, details={"risk_authorization": auth})

        if self.cfg.dry_run and self.cfg.features.limit_entries:
            from levels import limit_price
            lp = limit_price(dec, price, atr, self.cfg)
            if lp is not None:
                return self._place_limit_sim(dec, lp, appr, auth)
        if self.cfg.dry_run:
            self.journal.event("dry_run_trade", style=dec.style, setup=dec.setup, direction=d, lots=appr.lots,
                               entry=price, sl=dec.sl, tp=dec.tp, score=dec.score, **appr.details)
            self.risk.note_trade()
            return Outcome(True, "dry run: order not sent", appr.lots, dry_run=True,
                           details={"risk_authorization": auth, "execution": {"fill": round(price, 2), "dry_run": True}})

        comment = make_comment(S.code, dec.score, dist)
        if self.cfg.features.limit_entries:
            from levels import limit_price
            lp = limit_price(dec, price, atr, self.cfg)
            if lp is not None:
                return self._place_limit(dec, lp, appr, auth, comment)
        self.journal.event("lifecycle", state="ORDER_SENT", lots=appr.lots, price=price, sl=dec.sl, tp=dec.tp)
        spread_at_send = self.c.spread_price()
        res = self.c.send_market(d, appr.lots, dec.sl, dec.tp, comment)
        if not res.ok:
            msg = f"order failed: {res.comment} (retcode {res.retcode}, attempts {res.attempts})"
            self.journal.event("order_failed", ambiguous=res.ambiguous, retcode=res.retcode, comment=res.comment)
            self.alerts.send("CRITICAL" if res.ambiguous else "WARN", msg + (" - OUTCOME UNKNOWN, check MT5" if res.ambiguous else ""))
            return Outcome(False, msg)

        pos = res.position
        self.journal.event("lifecycle", state="ORDER_FILLED", ticket=pos.ticket, fill=res.fill_price,
                           slippage=res.slippage, latency_ms=round(res.latency_ms, 1), warnings=res.warnings)
        if self.monitor is not None:
            self.monitor.record_slippage(res.slippage)
        if res.slippage * 1.0 > ex.max_slippage_points * self.c.symbol_info().point:
            self.alerts.send("WARN", f"High slippage {res.slippage:.2f} on ticket {pos.ticket}")
        if not pos.sl:
            if not self.c.modify_sltp(pos, sl=dec.sl, tp=dec.tp):
                self.alerts.send("CRITICAL", f"Ticket {pos.ticket} opened WITHOUT stop loss and it could not be set: closing")
                self.c.close(pos)
                self.risk.note_trade()
                return Outcome(False, "opened without stop loss; closed for safety", appr.lots, ticket=pos.ticket)
            pos = self.c.position_by_ticket(pos.ticket) or pos
        self.mgr.register(pos, dec, res, appr, ms, news, spread_at_send)
        self.risk.note_trade()
        prob = f", P(win) {dec.p_win:.0%}, E[R] {dec.exp_r:+.2f}" if dec.p_win is not None else ""
        self.alerts.send("INFO", f"Opened {dec.style} {dec.action} {pos.volume} @ {pos.price_open:.2f} SL {pos.sl:.2f} "
                                 f"TP {pos.tp:.2f} | {dec.setup} in {dec.regime}, score {dec.score}{prob}", key=f"open{pos.ticket}")
        return Outcome(True, "executed", pos.volume, ticket=pos.ticket,
                       details={"risk_authorization": auth, "execution": {"fill": pos.price_open, "slippage": round(res.slippage, 3),
                                                                          "latency_ms": round(res.latency_ms, 1), "ticket": pos.ticket}})

    # ---- V4.2 limit entries -------------------------------------------------
    def _place_limit(self, dec, lp, appr, auth, comment):
        import time as _t
        exp = self._limit_expiry()
        if exp is None:
            return Outcome(False, "limit would rest into rollover / late-Friday hours", details={"risk_authorization": auth})
        ok, ticket, msg = self.c.send_limit(dec.direction, appr.lots, lp, dec.sl, dec.tp, comment, exp)
        if not ok:
            return Outcome(False, f"limit order rejected: {msg}", details={"risk_authorization": auth})
        pend = self.risk.state.data.setdefault("pending", {})
        pend[str(ticket)] = {"dec": {k: getattr(dec, k) for k in PENDING_FIELDS}, "price": lp, "lots": appr.lots,
                             "expiry": exp, "placed": _t.time(), "comment": comment, "auth": auth}
        self.risk.state.save()
        self.journal.event("lifecycle", state="LIMIT_PLACED", order=ticket, price=lp, expiry_min=self.cfg.execution.limit_expiry_min)
        return Outcome(True, f"limit placed @ {lp}", appr.lots, ticket=ticket,
                       details={"risk_authorization": auth, "execution": {"limit": lp, "order": ticket}})

    def news_ctx_blackout_reason(self):
        n = getattr(self, "_current_news", None)
        if n is not None and getattr(n, "blackout", False):
            return "news blackout: " + str(getattr(n, "blackout_reason", "event blackout"))
        if n is not None and getattr(self.cfg.news, "calendar_require_fresh_for_entries", True) and getattr(self.cfg.features, "require_fresh_calendar_entries", True) and not getattr(n, "calendar_ok", False):
            return "economic calendar unavailable/stale"
        return ""

    def _limit_expiry(self):
        """Limit expiry, cut short so a resting order never fills inside rollover hours or after the Friday cutoff."""
        import time as _t
        from datetime import timedelta
        now = self.c.utc_now()
        want = now + timedelta(minutes=self.cfg.execution.limit_expiry_min)
        sc = self.cfg.sessions
        t = now.replace(second=0, microsecond=0)
        while t < want:
            if t.hour in sc.avoid_hours_utc or t.weekday() >= 5 or (t.weekday() == 4 and t.hour >= sc.no_new_trades_friday_after_utc):
                break
            t += timedelta(minutes=1)
        if (t - now).total_seconds() < 120:
            return None
        return _t.time() + (min(t, want) - now).total_seconds()

    def _place_limit_sim(self, dec, lp, appr, auth):
        """DRY_RUN: record the limit order the live engine WOULD place; manage_pending simulates its fill or expiry."""
        exp = self._limit_expiry()
        if exp is None:
            return Outcome(False, "limit would rest into rollover / late-Friday hours", details={"risk_authorization": auth})
        import time as _t
        sim = self.risk.state.data.setdefault("pending_sim", {})
        key = f"sim{int(_t.time() * 1000)}"
        sim[key] = {"dec": {k: getattr(dec, k) for k in PENDING_FIELDS}, "price": lp, "lots": appr.lots, "expiry": exp,
                    "placed": _t.time(), "auth": auth}
        self.risk.state.save()
        self.journal.event("dry_run_limit_placed", key=key, price=lp, lots=appr.lots, sl=dec.sl, tp=dec.tp)
        return Outcome(True, f"dry run: limit @ {lp} simulated", appr.lots, dry_run=True,
                       details={"risk_authorization": auth, "execution": {"limit": lp, "dry_run": True}})

    def _manage_sim_pending(self, blocked_reason):
        import time as _t
        sim = self.risk.state.data.get("pending_sim", {})
        out, t = [], self.c.tick()
        for key, p in list(sim.items()):
            d, lp, sl = p["dec"]["direction"], p["price"], p["dec"]["sl"]
            if t is not None and ((d > 0 and t.ask <= lp) or (d < 0 and t.bid >= lp)):
                sim.pop(key)
                self.risk.note_trade()
                self.journal.event("dry_run_limit_filled", key=key, price=lp, ask=t.ask, bid=t.bid)
                out.append(("sim_filled", key))
                continue
            px = None if t is None else (t.bid if d > 0 else t.ask)
            why = blocked_reason or ("expired" if _t.time() > p["expiry"] else
                                     "price reached the stop before filling" if px is not None and (px - sl) * d <= 0 else None)
            if why:
                sim.pop(key)
                self.journal.event("dry_run_limit_cancelled", key=key, reason=why)
                out.append(("sim_cancelled", key))
        if out:
            self.risk.state.save()
        return out

    def _fill_authorized(self, pos, dec, p, blocked_reason):
        """A resting order was fully authorised when placed. At fill time only HARD reasons justify closing it:
        a news blackout, price already beyond its stop/target, a breached hard risk limit, or a size well above what
        was authorised. Soft drift - a 0.01-lot rounding change after a small equity move, the daily trade counter,
        clock windows - does not: closing a protected position would only pay the spread twice (V4.3 did)."""
        if not (getattr(self.cfg.features, "strict_pending_revalidation", True) and self.cfg.execution.pending_revalidate_on_fill):
            return True, ""
        d = dec.direction
        if blocked_reason and "news" in str(blocked_reason).lower():
            return False, "news blackout at fill: " + str(blocked_reason)
        n = getattr(self, "_current_news", None)
        if n is not None and getattr(n, "blackout", False):
            return False, "news blackout at fill"
        t = self.c.tick()
        if t is not None and self.c.tick_age_s() <= self.cfg.execution.max_tick_age_s:
            live = t.bid if d > 0 else t.ask
            if (live - dec.sl) * d <= 0 or (dec.tp - live) * d <= 0:
                return False, "price already beyond stop or target at fill"
        others = [x for x in self.c.positions() if x.ticket != pos.ticket]
        eq = self.c.account().equity
        actual = 100 * float(pos.volume) * self.c.calc_loss_per_lot(d, float(pos.price_open), dec.sl) / eq
        ok, why = self.risk.hard_limits(others, actual)
        if not ok:
            return False, "hard risk limit at fill: " + why
        authorised = float((p.get("auth") or {}).get("risk_pct", 0.0) or 0.0)
        if authorised > 0 and actual > authorised * 1.25 + 1e-9:
            return False, f"filled risk {actual:.2f}% exceeds the authorised {authorised:.2f}%"
        return True, ""

    def manage_pending(self, blocked_reason=None):
        """Detect fills (re-authorising them against hard limits only), cancel on expiry, stop-level invalidation or
        when trading is blocked. DRY_RUN simulates its virtual limits. Returns events."""
        import time as _t
        from types import SimpleNamespace
        from decision import Decision
        out = self._manage_sim_pending(blocked_reason) if self.cfg.dry_run else []
        pend = self.risk.state.data.setdefault("pending", {})
        if not pend:
            return out
        live = {str(o.ticket) for o in self.c.orders()}
        t = self.c.tick()
        for tk, p in list(pend.items()):
            d = p["dec"]["direction"]
            if tk not in live:
                cands = [x for x in self.c.positions() if getattr(x, "comment", "") == p["comment"][:31]
                         and str(x.ticket) not in self.risk.state.trades]
                pos = min(cands, key=lambda x: abs(float(x.price_open) - float(p["price"]))) if cands else None
                pend.pop(tk)
                if pos is None:
                    self.journal.event("lifecycle", state="LIMIT_GONE", order=int(tk))
                    out.append(("gone", tk))
                    continue
                dec = Decision(action="BUY" if d > 0 else "SELL", **p["dec"])
                ok, why = self._fill_authorized(pos, dec, p, blocked_reason)
                if not ok:
                    self.alerts.send("CRITICAL", f"Pending order {tk} filled but is no longer authorised: {why}; closing {pos.ticket}")
                    if not self.c.close(pos):
                        self.alerts.send("CRITICAL", f"FAILED to close unauthorised pending-fill ticket {pos.ticket}")
                    self.journal.event("lifecycle", state="LIMIT_FILL_REJECTED", order=int(tk), ticket=pos.ticket, reason=why)
                    out.append(("filled_rejected", tk))
                    continue
                if blocked_reason:
                    self.alerts.send("WARN", f"Limit {tk} filled while entries were blocked ({blocked_reason}); kept - it was "
                                             f"authorised at placement and is protected by its broker-side stop/target")
                res = SimpleNamespace(requested_price=p["price"], slippage=(pos.price_open - p["price"]) * d, latency_ms=0.0)
                self.mgr.register(pos, dec, res, None, None)
                self.risk.note_trade()
                self.journal.event("lifecycle", state="LIMIT_FILLED", order=int(tk), ticket=pos.ticket, fill=pos.price_open)
                out.append(("filled", tk))
                continue
            px = t.bid if d > 0 else t.ask
            why = blocked_reason or ("expired" if _t.time() > p["expiry"] else
                                     "price reached the stop before filling" if (px - p["dec"]["sl"]) * d <= 0 else None)
            if why and self.c.cancel_order(int(tk)):
                pend.pop(tk)
                self.journal.event("lifecycle", state="LIMIT_CANCELLED", order=int(tk), reason=why)
                out.append(("cancelled", tk))
        self.risk.state.save()
        return out
