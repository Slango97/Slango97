"""Human-facing reporting and chart enrichment for the V5 brain."""
from __future__ import annotations

import time
from pathlib import Path

from .chart import render_chart
from .telegram import TelegramBrain


class V5Reporter:
    def __init__(self, cfg, alerts=None):
        self.cfg = cfg
        self.tg = TelegramBrain(cfg, alerts)
        self.chart_dir = Path(cfg.system.data_dir) / "charts"
        self._last_health = {}
        self._last_news_sig = {}
        self._last_universe = 0.0

    def _health_message(self, run):
        f = run.findings
        symbol = run.symbol or "MARKET"
        d = run.decision
        lines = [
            f"🧠 MARKET HEALTH — {symbol}",
            f"Overall health: {run.report.get('market_health', 0):.0f}/100",
            f"Technical: {f['technical'].score:.0f} | Quant: {f['quantitative'].score:.0f} | News: {f['news'].score:.0f}",
            f"Intermarket: {f['intermarket'].score:.0f} | Connection: {f['connection'].score:.0f}",
            f"Bias: {'BUY' if d.direction > 0 else 'SELL' if d.direction < 0 else 'NEUTRAL'}",
            f"Confidence: {d.confidence:.0%} (decision confidence, not a profit probability)",
            f"State: {d.permission}",
        ]
        if f["news"].warnings:
            lines.append("News: " + "; ".join(f["news"].warnings[:2]))
        if f["news"].facts.get("next_event"):
            lines.append("Upcoming: " + str(f["news"].facts["next_event"]))
        return "\n".join(lines)

    def _news_message(self, run):
        n = run.findings["news"]
        f = n.facts
        lines = ["📰 NEWS / MACRO UPDATE — " + (run.symbol or "MARKET"),
                 f"Status: {f.get('status')} | Calendar: {f.get('calendar_state')} | Phase: {f.get('event_phase')}",
                 f"Impact: {f.get('impact')} | Bias: {f.get('bias',0):+.2f} | Confidence: {f.get('confidence',0):.0%}"]
        if f.get("next_event"):
            lines.append("Upcoming: " + str(f["next_event"]))
        if f.get("headlines"):
            lines.append("Headlines: " + " | ".join(f["headlines"][:3]))
        if f.get("summary"):
            lines.append("Interpretation: " + str(f["summary"])[:700])
        if f.get("blackout"):
            lines.append("⚠️ New-entry restriction: " + (f.get("blackout_reason") or "event risk window"))
        return "\n".join(lines)

    def publish(self, run, *, frames=None, force_health=False):
        now = time.time()
        sym = run.symbol or "MARKET"
        every = float(getattr(self.cfg.brain, "health_report_interval_min", 60)) * 60
        last = self._last_health.get(sym, 0)
        if force_health or now - last >= every:
            self.tg.text(self._health_message(run), key=f"health:{sym}", cooldown_s=every * 0.9)
            self._last_health[sym] = now

        d = run.decision
        n = run.findings["news"]
        news_sig = repr((n.facts.get("status"), n.facts.get("calendar_state"), n.facts.get("event_phase"),
                         n.facts.get("next_event"), n.facts.get("blackout"), tuple(n.facts.get("headlines", [])[:3])))
        if news_sig != self._last_news_sig.get(sym):
            self.tg.text(self._news_message(run), key=f"news:{sym}:{hash(news_sig)}", cooldown_s=900)
            self._last_news_sig[sym] = news_sig

        if d.action in ("BUY", "SELL") and d.setup_quality >= float(getattr(self.cfg.brain, "opportunity_report_threshold", 80)):
            f = run.findings
            msg = [f"🎯 TRADE OPPORTUNITY — {sym}",
                   f"Action: {d.action} | Setup quality: {d.setup_quality:.0f}/100",
                   f"Confidence: {d.confidence:.0%}",
                   f"Permission: {d.permission}",
                   f"Setup: {d.facts.get('setup','?')} | Style: {d.facts.get('style','?')}",
                   f"Entry {d.facts.get('entry')} | SL {d.facts.get('sl')} | TP {d.facts.get('tp')} | RR {d.facts.get('rr')}",
                   "Reasons: " + "; ".join(d.reasons[:5])]
            if d.contradictions:
                msg.append("Contradictions: " + "; ".join(d.contradictions[:4]))
            self.tg.text("\n".join(msg), key=f"opp:{sym}:{d.action}:{d.facts.get('setup')}", cooldown_s=600)
            frame_map = frames or {}
            frame = frame_map.get("M15") if frame_map.get("M15") is not None else frame_map.get("M5")
            if frame is not None and getattr(self.cfg.brain, "telegram_charts", True):
                path = self.chart_dir / f"{sym}_opportunity.png"
                p = render_chart(frame, path=path, title=f"{sym} — V5 Opportunity",
                                 entry=d.facts.get("entry"), sl=d.facts.get("sl"), tp=d.facts.get("tp"),
                                 patterns=f["technical"].facts.get("candlestick_patterns"))
                if p:
                    self.tg.photo(p, caption=f"{sym} {d.action} — V5 technical/market analysis")

        no_trade_threshold = float(getattr(self.cfg.brain, "no_trade_report_threshold", 55.0))
        opp_f = run.findings["opportunity"]
        best_candidate = float(opp_f.facts.get("best_candidate_score", 0.0) or 0.0)
        if d.action not in ("BUY", "SELL") and max(opp_f.score, best_candidate) >= no_trade_threshold:
            why = "; ".join((d.blockers or d.reasons)[:6]) or "no executable setup"
            self.tg.text(f"🚫 NO TRADE — {sym}\nOpportunity quality {opp_f.score:.0f}/100\n"
                         f"Best candidate {best_candidate:.0f}/100\nWhy not entered: {why}",
                         key=f"notrade:{sym}:{d.facts.get('setup')}:{why[:60]}", cooldown_s=900)

    def selection_report(self, rows):
        rows = list(rows or [])
        if not rows:
            return
        top = rows[:5]
        lines = ["🧭 ADAPTIVE MARKET SELECTOR — account-aware funnel"]
        for i, r in enumerate(top, 1):
            bias = "BUY" if r.bias > 0 else "SELL" if r.bias < 0 else "NEUTRAL"
            aff = "AFFORDABLE" if r.affordable else "INELIGIBLE"
            lines.append(f"{i}. {r.requested} → {r.symbol or 'N/A'} | rank {r.ranking_score:.0f} | {aff} | {bias} | {r.regime or '?'} | min-lot risk {r.min_lot_risk_pct if r.min_lot_risk_pct is not None else '?'}%")
        best = next((r for r in rows if r.affordable and r.status == "OK"), None)
        if best:
            lines.append(f"Selected candidate: {best.symbol} | {best.ranking_score:.0f}/100")
            lines.append("Next: full news → intermarket → critic → chief → risk → execution gates on the selected market.")
        else:
            lines.append("No market passed account-affordability + selection threshold; remain flat.")
        self.tg.text("\n".join(lines), key=f"selector:{int(time.time() // 300)}", cooldown_s=240)

    def universe_report(self, rows):
        rows = list(rows or [])
        if not rows:
            return
        lines = ["🌐 V5 MARKET RADAR — analysis only"]
        for r in sorted(rows, key=lambda x: x.health, reverse=True):
            bias = "BUY" if r.bias > 0 else "SELL" if r.bias < 0 else "NEUTRAL"
            symbol = r.symbol or "N/A"
            lines.append(f"{r.requested} → {symbol} | {r.status} | H {r.health:.0f} | C {r.confidence:.0%} | {bias}")
            if r.patterns:
                lines.append("  " + ", ".join(r.patterns[:3]))
            if r.warnings:
                lines.append("  ⚠ " + "; ".join(r.warnings[:2]))
        lines.append("Secondary-market rows are radar/health information; live orders remain controlled by the primary deterministic execution kernel.")
        self.tg.text("\n".join(lines), key=f"universe:{int(time.time() // 3600)}", cooldown_s=3000)

    def trade_entry_feedback(self, trade_record):
        symbol = trade_record.get("symbol") or "MARKET"
        direction = trade_record.get("direction", 1)
        side = "BUY" if direction > 0 else "SELL"
        msg = (f"✅ TRADE ENTERED — {symbol}\n{side} {trade_record.get('setup','?')}\n"
               f"Entry {trade_record.get('entry','?')} | SL {trade_record.get('sl','?')} | TP {trade_record.get('tp','?')}\n"
               f"Lots {trade_record.get('lots','?')} | Risk {trade_record.get('risk_pct','?')}% | RR {trade_record.get('rr','?')}\n"
               f"Mode {trade_record.get('mode','?')}")
        self.tg.text(msg, key=f"open:{trade_record.get('ticket','') or trade_record.get('setup','')}", cooldown_s=0)

    def trade_feedback(self, trade_record):
        symbol = trade_record.get("symbol") or "MARKET"
        direction = "BUY" if trade_record.get("direction", 1) > 0 else "SELL"
        msg = (f"🏁 TRADE FEEDBACK — {symbol}\n{direction} {trade_record.get('setup','?')}\n"
               f"P/L {trade_record.get('net_pnl','?')} | R {trade_record.get('r_multiple','?')}\n"
               f"Exit reason: {trade_record.get('reason','?')}\n"
               f"MAE {trade_record.get('mae_r','?')}R | MFE {trade_record.get('mfe_r','?')}R")
        self.tg.text(msg, key=f"close:{trade_record.get('ticket','')}", cooldown_s=0)

    def close(self):
        self.tg.close()
