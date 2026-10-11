import logging
import time

from management import PosView, plan

log = logging.getLogger("manager")


class TradeManager:
    def __init__(self, cfg, client, state, journal, alerts=None):
        self.cfg, self.c, self.state, self.journal, self.alerts = cfg, client, state, journal, alerts

    def transition(self, ticket, new_state, **info):
        st = self.state.trades.get(str(ticket))
        if st:
            st["lifecycle"] = new_state
        self.journal.event("lifecycle", ticket=ticket, state=new_state, **info)

    def register(self, pos, dec, res, approval, ms, news=None, spread_at_send=None):
        """Create the management record for a freshly filled position, using ACTUAL fill data, plus the
        execution analytics (signal vs requested vs filled price, slippage, spread, latency)."""
        d = dec.direction
        sl0 = pos.sl or dec.sl
        risk = abs(pos.price_open - sl0)
        risk_money = pos.volume * self.c.calc_loss_per_lot(d, pos.price_open, sl0)
        rec = {"style": dec.style, "setup": dec.setup, "score": dec.score, "direction": d,
               "entry": pos.price_open, "sl0": sl0, "tp": pos.tp or dec.tp, "tp1": getattr(dec, "tp1", None),
               "tp1_frac": self.cfg.strategy.tp1_close_frac if hasattr(self, "cfg") else 0.5, "risk": risk, "risk_money": risk_money,
               "lots": pos.volume, "signal_price": dec.entry, "requested_price": res.requested_price,
               "slippage": res.slippage, "slippage_r": round(res.slippage / risk, 4) if risk else 0.0,
               "signal_to_fill_r": round((pos.price_open - dec.entry) * d / risk, 4) if risk else 0.0,
               "latency_ms": round(res.latency_ms, 1), "spread": spread_at_send if spread_at_send is not None else self.c.spread_price(),
               "session": dec.session, "vol_regime": dec.vol_regime, "regime": dec.regime, "risk_tier": dec.risk_tier,
               "market_quality": dec.market_quality, "conflict": dec.conflict, "p_win": dec.p_win, "exp_r": dec.exp_r,
               "event_phase": getattr(news, "event_phase", None), "target_kind": dec.target_kind,
               "htf_bias": dec.htf_bias, "components": dec.components, "tags": dec.tags,
               "opened_ts": time.time(), "be": False, "partial": False, "lifecycle": "OPEN",
               "mfe_r": 0.0, "mae_r": 0.0, "last_sl": pos.sl or None, "reconstructed": False}
        self.state.trades[str(pos.ticket)] = rec
        self.state.save()
        self.journal.trade_open(ticket=pos.ticket, **{k: v for k, v in rec.items() if k != "components"},
                                components=dec.components)
        self.transition(pos.ticket, "POSITION_OPEN", fill=pos.price_open, volume=pos.volume)
        return rec

    def manage(self, views, news):
        if getattr(self.c, "read_only", False):
            return                      # DRY_RUN never touches real positions
        info = self.c.symbol_info()
        spread = self.c.spread_price()
        min_stop = self.c.min_stop_distance()
        tick = self.c.tick()
        for p in self.c.positions():
            st = self.state.trades.get(str(p.ticket))
            if not st:
                continue
            S = self.cfg.strategy.styles.get(st["style"])
            if S is None:
                continue
            d = st["direction"]
            px = tick.bid if d > 0 else tick.ask
            if st.get("risk"):
                r_now = (px - st["entry"]) * d / st["risk"]
                st["mfe_r"] = round(max(st.get("mfe_r", 0.0), r_now), 4)
                st["mae_r"] = round(min(st.get("mae_r", 0.0), r_now), 4)
            pv = PosView(d, tick.bid if d > 0 else tick.ask, p.sl, p.volume, self.c.pos_age_hours(p),
                         info.volume_min, info.volume_step)
            for a in plan(st, pv, views.get(S.trail_tf), news.blackout, spread, min_stop, S):
                if not self._apply(p, st, a):
                    break
        self.state.save()

    def _apply(self, p, st, a):
        if a.kind == "close":
            ok = self.c.close(p)
            if ok:
                log.info("ticket %s closed: %s", p.ticket, a.reason)
                self.transition(p.ticket, "CLOSING", reason=a.reason)
                st["close_reason"] = a.reason
            return False
        if a.kind == "partial":
            ok = self.c.close(p, a.volume)
            if ok:
                st["partial"] = True
                log.info("ticket %s: %s", p.ticket, a.reason)
                self.transition(p.ticket, "PARTIAL", volume=a.volume, reason=a.reason)
            return ok
        if a.kind == "move_sl":
            ok = self.c.modify_sltp(p, sl=a.value)
            if ok:
                st["last_sl"] = a.value
                if a.flag == "be":
                    st["be"] = True
                log.info("ticket %s: SL -> %.2f (%s)", p.ticket, a.value, a.reason)
                self.transition(p.ticket, "BE" if a.flag == "be" else "TRAILING", sl=a.value, reason=a.reason)
            return ok
        return False
