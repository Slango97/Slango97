"""Human-facing Telegram intelligence for the V5 brain (Enhancement Specification V5.5).

Policy (spec section 2): the engine keeps monitoring continuously; Telegram only receives
* startup / daily market health / daily news briefing - once per Lebanon-local day;
* a news event - once when it is announced, once when it is released (again only if the numbers change);
* best opportunity / no-trade / selector state - only when the state changes materially;
* trade open, TP1, break-even and close - once per ticket and stage, with the executed levels.
Every message names the CURRENT active market and shows Asia/Beirut time. Charts are rendered from real MT5
frames and, for execution charts, from the exact candidate passed to the execution engine (see chart.py).
Nothing here can influence a trading gate.
"""
from __future__ import annotations

from pathlib import Path

from instruments import affected_markets, profile_for
from pricefmt import fmt_price
from timefmt import fmt_local, local_date, zone_name

from .chart import plan_from_decision, render_execution, render_indicators, render_structure
from .notify import NotificationManager
from .telegram import TelegramBrain

BRAND = "SLANGO PRO TRADER"
IMPACT_RANK = {"High": 0, "Medium": 1, "Low": 2}
MECHANISM = {
    "inflation": "hotter-than-expected inflation -> higher yields / hawkish {cb} -> {ccy} stronger",
    "policy": "hawkish surprise -> higher rate expectations -> {ccy} stronger (dovish = weaker)",
    "growth": "stronger-than-expected data -> {ccy} stronger and risk appetite firmer",
}
CB = {"USD": "Fed", "EUR": "ECB", "GBP": "BoE", "JPY": "BoJ", "AUD": "RBA", "NZD": "RBNZ", "CAD": "BoC", "CHF": "SNB"}


def _arrow(x):
    return "↑" if x > 0 else "↓" if x < 0 else "→"


class V5Reporter:
    def __init__(self, cfg, alerts=None, market=None):
        self.cfg = cfg
        self.tg = TelegramBrain(cfg, alerts)
        data = Path(cfg.system.data_dir)
        self.chart_dir = data / "charts"
        self.nm = NotificationManager(data / "notify_state.json")
        self.tz = zone_name(cfg)
        self.market = market                     # callable -> current adaptive selection (spec section 13)
        self.last_run = None
        nc = getattr(cfg, "notify", None)
        self.views = tuple(getattr(nc, "chart_views", ("structure", "indicators", "execution")) or ())
        self.lead_min = float(getattr(nc, "news_announce_lead_min", 120.0))
        self.announce_medium = bool(getattr(nc, "news_announce_medium", True))
        self.explain_active = bool(getattr(getattr(cfg, "features", None), "telegram_explain", False))

    # ---- helpers -----------------------------------------------------------
    def _sym(self, fallback=""):
        try:
            cur = self.market() if callable(self.market) else self.market
        except Exception:
            cur = None
        return cur or fallback or "MARKET"

    def _head(self, icon, title, sym=None):
        return f"{icon} {BRAND} | Active market: {self._sym(sym)}\n{title} — {fmt_local(None, self.tz)}"

    def _charts_on(self):
        return bool(getattr(self.cfg.brain, "telegram_charts", True))

    def _frame(self, frames, tf=None):
        fm = frames or {}
        for k in (tf, "M15", "M5", "H1"):
            if k and fm.get(k) is not None:
                return k, fm[k]
        return None, None

    def _send_analysis_charts(self, sym, frames, views, tag, caption):
        """Charts A (structure) and B (indicators) from real MT5 frames and the kernel's own analysis view."""
        if not self._charts_on():
            return
        tf, df = self._frame(frames)
        if df is None:
            return
        stamp = fmt_local(None, self.tz)
        title = f"{BRAND} | {sym} {tf}"
        if "structure" in self.views:
            p = render_structure(df, (views or {}).get(tf), path=self.chart_dir / f"{sym}_{tag}_structure.png",
                                 title=title, stamp=stamp)
            if p:
                self.tg.photo(p, caption=f"{caption}\nA — market structure ({tf})")
        if "indicators" in self.views:
            p = render_indicators(df, path=self.chart_dir / f"{sym}_{tag}_indicators.png", title=title, stamp=stamp,
                                  bounds=self.cfg.sessions.bounds)
            if p:
                self.tg.photo(p, caption=f"{caption}\nB — indicators used by the kernel ({tf})")

    def _send_execution_chart(self, plan, frames, tag, caption):
        if not self._charts_on() or "execution" not in self.views or plan is None:
            return None
        _, df = self._frame(frames, plan.timeframe)
        if df is None:
            return None
        p = render_execution(df, plan, path=self.chart_dir / f"{plan.symbol}_{tag}_execution.png",
                             title=BRAND, stamp=fmt_local(None, self.tz))
        if p:
            self.tg.photo(p, caption=caption)
        return p

    # ---- news ----------------------------------------------------------------
    def _watch(self):
        wl = list(getattr(self.cfg.brain, "watchlist", ()) or ())
        sym = self._sym()
        return wl if any(sym.upper().startswith(w) for w in wl) else [sym] + wl

    def _affected(self, ev, limit=4):
        rows = affected_markets(ev.get("currency", "USD"), ev.get("category", "growth"), self._watch())
        return ", ".join(f"{s} {_arrow(e)}" for s, e in rows[:limit]) or "no configured market"

    def _active_effect(self, ev):
        from instruments import instrument_effect
        try:
            return instrument_effect(profile_for(self._sym()), ev.get("currency", "USD"), ev.get("category", "growth"))
        except Exception:
            return 0.0

    def _announce_text(self, ev):
        ccy = ev.get("currency", "USD")
        mins = float(ev.get("minutes_to") or 0)
        eff = self._active_effect(ev)
        mech = MECHANISM.get(ev.get("category"), MECHANISM["growth"]).format(cb=CB.get(ccy, ccy + " central bank"), ccy=ccy)
        lines = [self._head("📅", f"UPCOMING {ev.get('impact', '').upper()}-IMPACT NEWS"),
                 f"{ev.get('title')} ({ccy}) at {fmt_local(ev['time'], self.tz)} — in {mins:.0f} min",
                 f"Forecast {ev.get('forecast') or 'n/a'} | Previous {ev.get('previous') or 'n/a'}",
                 f"Mechanism: {mech}",
                 f"Affected markets (if data beats forecast): {self._affected(ev)}"]
        if eff:
            lines.append(f"{self._sym()}: strong {ccy} data -> {'up' if eff > 0 else 'down'}; "
                         "no new entries inside the event window, trade only after price confirms.")
        return "\n".join(lines)

    def _release_text(self, ev):
        from events import currency_surprise
        ccy = ev.get("currency", "USD")
        g = currency_surprise(ev, self.cfg.events.classes)
        eff = self._active_effect(ev)
        verdict = ("better than expected" if g > 0 else "worse than expected" if g < 0 else "in line") if g is not None else "surprise not computable"
        lines = [self._head("📊", "NEWS RELEASED"),
                 f"{ev.get('title')} ({ccy}) at {fmt_local(ev['time'], self.tz)}",
                 f"Actual {ev.get('actual')} vs forecast {ev.get('forecast') or 'n/a'} (previous {ev.get('previous') or 'n/a'})",
                 f"For {ccy}: {verdict}" + (f" (surprise {g:+.2f})" if g is not None else "")]
        if g is not None and eff:
            d = g * eff
            lines.append(f"Implication for {self._sym()}: {'bullish' if d > 0 else 'bearish' if d < 0 else 'neutral'} "
                         f"pressure; initial spike can reverse — the absorption window applies before any entry.")
        lines.append(f"Other affected markets: {self._affected(ev)}")
        return "\n".join(lines)

    def _news_briefing(self, events, local_day):
        today = [e for e in events if local_date(e["time"], self.tz) == local_day and e.get("status") == "UPCOMING"]
        today.sort(key=lambda e: (IMPACT_RANK.get(e.get("impact"), 3), e["time"]))
        lines = [self._head("📰", "DAILY NEWS BRIEFING")]
        if not today:
            lines.append("No high/medium-impact releases for the configured currencies today.")
        for i, e in enumerate(today[:10], 1):
            lines.append(f"{i}. [{e.get('impact')}] {fmt_local(e['time'], self.tz, '%H:%M')} {e.get('currency')} "
                         f"{e.get('title')} — affects {self._affected(e, 3)}")
        return "\n".join(lines)

    def _publish_news(self, run, local_day):
        n = run.findings["news"].facts
        events = list(n.get("events") or [])
        if events and self.nm.daily_due("news_briefing", local_day):
            self.tg.text(self._news_briefing(events, local_day))
        for ev in events:
            eid = ev.get("id")
            if not eid:
                continue
            relevant = bool(ev.get("relevant"))
            important = ev.get("impact") == "High" or (self.announce_medium and relevant and ev.get("impact") == "Medium")
            if not important:
                continue
            self.nm.event_stage(eid, "DISCOVERED")
            if ev.get("status") == "UPCOMING" and 0 < float(ev.get("minutes_to") or 0) <= self.lead_min:
                if self.nm.event_stage(eid, "ANNOUNCED"):
                    self.tg.text(self._announce_text(ev))
            elif ev.get("status") == "RELEASED":
                if self.nm.event_stage(eid, "RELEASED", detail=[ev.get("actual"), ev.get("forecast"), ev.get("previous")]):
                    self.tg.text(self._release_text(ev))
        # Event-window state for the ACTIVE market: report entering / leaving a blackout once.
        state = {"blackout": bool(n.get("blackout")), "phase": n.get("event_label") or n.get("event_phase")}
        prev = self.nm.peek_state(f"news_window:{run.symbol}")
        if self.nm.state_changed(f"news_window:{run.symbol}", state) and (prev is not None or state["blackout"]):
            if state["blackout"]:
                self.tg.text(self._head("⛔", "NEW-ENTRY RESTRICTION", run.symbol) + "\n"
                             + (n.get("blackout_reason") or "event risk window") + f"\nPhase: {state['phase']}")
            elif prev and prev.get("blackout"):
                self.tg.text(self._head("✅", "EVENT WINDOW CLEARED", run.symbol) + f"\nPhase: {state['phase']}")

    # ---- daily health ---------------------------------------------------------
    def _health_line(self, run):
        f, d = run.findings, run.decision
        bias = "BUY" if d.direction > 0 else "SELL" if d.direction < 0 else "NEUTRAL"
        return (f"{run.symbol}: health {run.report.get('market_health', 0):.0f}/100 | Tech {f['technical'].score:.0f} "
                f"Quant {f['quantitative'].score:.0f} News {f['news'].score:.0f} Inter {f['intermarket'].score:.0f} | "
                f"bias {bias} | state {d.permission}")

    def _health_message(self, run):
        f = run.findings
        lines = [self._head("🧠", "DAILY MARKET HEALTH", run.symbol), self._health_line(run),
                 f"Confidence {run.decision.confidence:.0%} (decision confidence, not a profit probability)"]
        if f["news"].warnings:
            lines.append("News: " + "; ".join(f["news"].warnings[:2]))
        return "\n".join(lines)

    # ---- main entry -----------------------------------------------------------
    def publish(self, run, *, frames=None, force_health=False, deterministic=None, views=None):
        self.last_run = run
        sym = run.symbol or self._sym()
        local_day = local_date(None, self.tz)
        universe_daily = bool(getattr(self.cfg.brain, "universe_enabled", True))
        if force_health or (not universe_daily and self.nm.daily_due("market_health", local_day)):
            self.tg.text(self._health_message(run))
        self._publish_news(run, local_day)

        d = run.decision
        opp_f = run.findings["opportunity"]
        if d.action in ("BUY", "SELL") and d.setup_quality >= float(getattr(self.cfg.brain, "opportunity_report_threshold", 80)):
            # Material change = market, side, setup or permission changes, or quality moves a 10-point band.
            fp = [sym, d.action, d.facts.get("setup"), d.permission, int(d.setup_quality // 10)]
            if self.nm.state_changed(f"opportunity:{sym}", fp):
                self.nm.state_changed(f"no_trade:{sym}", None)
                self._opportunity(run, sym, frames, deterministic, views)
            return
        no_trade_threshold = float(getattr(self.cfg.brain, "no_trade_report_threshold", 55.0))
        best_candidate = float(opp_f.facts.get("best_candidate_score", 0.0) or 0.0)
        if d.action not in ("BUY", "SELL") and max(opp_f.score, best_candidate) >= no_trade_threshold:
            codes = [str(b).split(":")[0][:40] for b in (d.blockers or d.reasons)[:2]]
            if self.nm.state_changed(f"no_trade:{sym}", [d.facts.get("setup"), codes], min_interval_s=900):
                self.nm.state_changed(f"opportunity:{sym}", None)
                why = "; ".join((d.blockers or d.reasons)[:5]) or "no executable setup"
                self.tg.text(self._head("🚫", "NO TRADE", sym) + f"\nOpportunity quality {opp_f.score:.0f}/100 | "
                             f"best candidate {best_candidate:.0f}/100\nWhy not entered: {why}")

    def _opportunity(self, run, sym, frames, dec, views):
        d = run.decision
        sc = abs(float(d.facts.get("entry") or 0) - float(d.facts.get("sl") or 0)) or None
        msg = [self._head("🎯", "BEST OPPORTUNITY", sym),
               f"{d.action} {d.facts.get('setup', '?')} ({d.facts.get('style', '?')}) | quality {d.setup_quality:.0f}/100 | "
               f"confidence {d.confidence:.0%} | permission {d.permission}",
               f"Entry {fmt_price(d.facts.get('entry'), sc)} | SL {fmt_price(d.facts.get('sl'), sc)} | "
               f"TP {fmt_price(d.facts.get('tp'), sc)} | RR {d.facts.get('rr')}",
               "Reasons: " + "; ".join(d.reasons[:5])]
        if d.contradictions:
            msg.append("Contradictions: " + "; ".join(d.contradictions[:4]))
        msg.append("Not an order: risk, news, broker and execution gates still decide.")
        self.tg.text("\n".join(msg))
        self._send_analysis_charts(sym, frames, views, "opportunity", f"{sym} {d.action} — opportunity analysis")
        if dec is not None and d.permission in ("APPROVE", "REDUCE") and getattr(dec, "is_entry", False):
            # Execution chart from the SAME decision object the engine hands to the execution kernel.
            tf = self._style_tf(dec)
            plan = plan_from_decision(dec, sym, tf, valid_until=self._validity())
            self._send_execution_chart(plan, frames, "ready", f"{sym} {plan.side} — trade-ready plan (pre-risk-gate)")

    def _style_tf(self, dec):
        try:
            return self.cfg.strategy.styles[dec.style].tf
        except Exception:
            return "M15"

    def _validity(self):
        try:
            from datetime import datetime, timedelta, timezone
            return fmt_local(datetime.now(timezone.utc) + timedelta(minutes=float(self.cfg.m1.window_min)), self.tz, "%H:%M")
        except Exception:
            return ""

    # ---- market selection / universe ------------------------------------------
    def selection_report(self, rows):
        rows = list(rows or [])
        if not rows:
            return
        best = next((r for r in rows if r.affordable and r.status == "OK" and getattr(r, "evidence", "") in ("", "PASS")), None)
        if not self.nm.state_changed("selector", best.symbol if best else None):
            return                                       # internal re-ranking continues silently
        lines = [self._head("🧭", "ADAPTIVE MARKET SELECTOR")]
        for i, r in enumerate(rows[:5], 1):
            bias = "BUY" if r.bias > 0 else "SELL" if r.bias < 0 else "NEUTRAL"
            aff = "affordable" if r.affordable else "INELIGIBLE"
            mlr = f"{r.min_lot_risk_pct}%" if r.min_lot_risk_pct is not None else "?"
            ev = f" | evidence {r.evidence}" if getattr(r, "evidence", "") else ""
            lines.append(f"{i}. {r.symbol or r.requested} | rank {r.ranking_score:.0f} | {aff} | {bias} | {r.regime or '?'} | min-lot risk {mlr}{ev}")
        if best:
            lines.append(f"Selected: {best.symbol} ({best.ranking_score:.0f}/100) — full news/intermarket/critic/chief/risk gates follow.")
        else:
            lines.append("No market passed account-affordability + selection threshold: remain flat.")
        self.tg.text("\n".join(lines))

    def universe_report(self, rows):
        """Daily consolidated market health across the configured universe (spec section 4)."""
        rows = list(rows or [])
        if not rows or not self.nm.daily_due("market_health", local_date(None, self.tz)):
            return
        lines = [self._head("🌐", "DAILY MARKET HEALTH — universe")]
        ranked = sorted(rows, key=lambda x: x.health, reverse=True)
        for r in ranked[:12]:
            bias = "BUY" if r.bias > 0 else "SELL" if r.bias < 0 else "NEUTRAL"
            warn = f" ⚠ {r.warnings[0]}" if r.warnings else ""
            lines.append(f"{r.symbol or r.requested} | {r.status} | health {r.health:.0f} | {bias} | {r.regime or '?'}{warn}")
        best = next((r for r in ranked if r.status == "OK"), None)
        if best:
            lines.append(f"Best current market: {best.symbol or best.requested} (health {best.health:.0f})")
        if self.last_run is not None:
            lines.append("Active: " + self._health_line(self.last_run))
        corr = self._correlation_line()
        if corr:
            lines.append(corr)
        lines.append("Analysis only — orders remain controlled by the deterministic execution kernel.")
        self.tg.text("\n".join(lines))

    # ---- exposure scaling ----------------------------------------------------------
    def exposure_report(self, scale, parts):
        """V5.7: one message when the risk multiplier moves to another 10% band (never on every heartbeat)."""
        band = round(float(scale) * 10) / 10
        prev = self.nm.peek_state("exposure_scale")
        if not self.nm.state_changed("exposure_scale", band) or (prev is None and band >= 1.0):
            return
        names = {"drawdown": "drawdown", "market_vol": "market volatility", "account_vol": "account volatility"}
        why = ", ".join(f"{names[k]} x{v:.2f}" for k, v in parts.items() if k in names) or "all conditions normal"
        extra = f" | account vol {parts['account_vol_pct']}%/yr" if "account_vol_pct" in parts else ""
        icon, title = ("🟢", "RISK BACK TO FULL SIZE") if band >= 1.0 else ("🟠", "RISK SCALED DOWN")
        self.tg.text(self._head(icon, title) + f"\nPer-trade risk x{scale:.2f} ({why}){extra}\n"
                     "Scaling only reduces risk; it recovers automatically as equity and volatility normalise.")

    def _correlation_line(self):
        """V5.8: strongest live correlations (markets that are effectively the same bet)."""
        try:
            from correlation_matrix import CorrelationMatrix
            pc = self.cfg.portfolio
            m = CorrelationMatrix.load(pc.corr_file)
            if m is None or m.age_h() > pc.corr_max_age_h:
                return ""
            top = m.strong_pairs(pc.corr_report_threshold)[:6]
            if not top:
                return "Correlation: no market pair above " + f"{pc.corr_report_threshold:.1f}"
            return "Correlated (one bet, sized together): " + ", ".join(f"{p} {v:+.2f}" for p, v in top)
        except Exception:
            return ""

    # ---- trade lifecycle --------------------------------------------------------
    def trade_entry_feedback(self, trade_record, dec=None, frames=None):
        ticket = trade_record.get("ticket") or f"{trade_record.get('symbol')}:{trade_record.get('setup')}:{trade_record.get('entry')}"
        if not self.nm.trade_stage(ticket, "TRADE_OPEN"):
            return
        symbol = trade_record.get("symbol") or self._sym()
        side = "BUY" if trade_record.get("direction", 1) > 0 else "SELL"
        sc = abs(float(trade_record.get("entry") or 0) - float(trade_record.get("sl") or 0)) or None
        caption = (f"{side} {symbol} {trade_record.get('setup', '?')} | entry {fmt_price(trade_record.get('entry'), sc)} "
                   f"SL {fmt_price(trade_record.get('sl'), sc)} TP {fmt_price(trade_record.get('tp'), sc)}")
        if not self.explain_active:                  # the record-based OPEN report already carries the full text
            tp1 = trade_record.get("tp1")
            self.tg.text(self._head("✅", "TRADE OPENED", symbol) + "\n"
                         f"{side} {trade_record.get('setup', '?')} | lots {trade_record.get('lots', '?')} | "
                         f"risk {trade_record.get('risk_pct', '?')}% | RR {trade_record.get('rr', '?')} | mode {trade_record.get('mode', '?')}\n"
                         f"Entry {fmt_price(trade_record.get('entry'), sc)} | SL {fmt_price(trade_record.get('sl'), sc)}"
                         + (f" | TP1 {fmt_price(tp1, sc)}" if tp1 else "") + f" | TP {fmt_price(trade_record.get('tp'), sc)}")
        if dec is not None:
            ex = {"fill": trade_record.get("entry"), "sl": trade_record.get("sl"), "tp": trade_record.get("tp"),
                  "ticket": trade_record.get("ticket") or 0}
            plan = plan_from_decision(dec, symbol, self._style_tf(dec), execution=ex)
            self._send_execution_chart(plan, frames, "open", "EXECUTED — " + caption)

    def trade_management(self, ticket, state, info=None, symbol=None):
        """TP1 / break-even: reported once per ticket when they actually happen."""
        stage = {"PARTIAL": "TP1_REACHED", "BE": "BE_PROTECTED"}.get(state)
        if stage is None or not self.nm.trade_stage(ticket, stage):
            return
        info = info or {}
        what = ("TP1 reached — partial close " + str(info.get("volume", "")) if stage == "TP1_REACHED"
                else f"Stop moved to break-even {fmt_price(info.get('sl'))}")
        self.tg.text(self._head("🛡️", "TRADE MANAGEMENT", symbol) + f"\nTicket {ticket}: {what}\n{info.get('reason', '')}")

    def trade_feedback(self, trade_record):
        ticket = trade_record.get("ticket") or f"close:{trade_record.get('t')}"
        if not self.nm.trade_stage(ticket, "TRADE_FINISHED") or self.explain_active:
            return
        symbol = trade_record.get("symbol") or self._sym()
        direction = "BUY" if trade_record.get("direction", 1) > 0 else "SELL"
        pnl, r = trade_record.get("net_pnl"), trade_record.get("r_multiple")
        icon = "🏁" if not isinstance(pnl, (int, float)) else ("🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪")
        self.tg.text(self._head(icon, "TRADE FINISHED", symbol) + "\n"
                     f"{direction} {trade_record.get('setup', '?')} | P/L {pnl if pnl is not None else '?'} | "
                     f"R {r if r is not None else '?'}\nExit reason: {trade_record.get('reason', '?')}\n"
                     f"MAE {trade_record.get('mae_r', '?')}R | MFE {trade_record.get('mfe_r', '?')}R")

    def close(self):
        self.tg.close()
