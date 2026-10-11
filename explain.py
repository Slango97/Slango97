"""Telegram messages generated only from Decision Records and trade records (plain text, no markup)."""
import time
from collections import Counter

ARROW = {"BUY": "BUY", "SELL": "SELL"}


def _sel(rec):
    return next((c for c in rec["candidates"] if c["status"] == "SELECTED"), None)


def _mtf(c):
    return " | ".join(f"{a['tf']} {a['role']}: {a['state']}" for a in c.get("mtf_alignment", []))


def fmt_open(rec):
    c = _sel(rec)
    if c is None:
        return None
    ra, ex, n = rec.get("risk_authorization") or {}, rec.get("execution") or {}, rec.get("news") or {}
    lines = [f"OPEN {c['direction']} {rec.get('symbol') or 'MARKET'} - {c['setup']} ({c['style']})",
             f"Entry {ex.get('fill', c['entry'])}  SL {c['sl']}  TP {c['tp']}  R:R {c['rr']}",
             f"Risk {ra.get('risk_pct', 0):.2f}% ({ra.get('lots', '?')} lots), tier {ra.get('tier', 'fixed')}",
             f"Regime {(rec.get('regime') or {}).get('label', '?')} | session {rec['session']} | market quality {(rec.get('market_quality') or {}).get('score', '?')}",
             f"Score {c['score']}" + (f" | P(win) {c['p_win']:.0%} | E[R] {c['expected_r']:+.2f}" if c.get("p_win") is not None else ""),
             f"Pro-quality {c.get('pro_quality_score', 0):.0f}/100 | evidence domains {c.get('strong_evidence_domains', 0)}/{c.get('required_evidence_domains', 0)} | adaptive risk {c.get('adaptive_risk_mult', 1.0):.0%}",
             f"MTF: {_mtf(c)}",
             f"Structure/liquidity: {rec.get('liquidity', '')}",
             f"Momentum: {c['components'].get('momentum', 0)}/10 | conflict index {c['conflict']:.2f}",
             f"News: {n.get('event_phase', 'n/a')}, intel {n.get('status', 'n/a')} {n.get('bias', 0):+.2f}"]
    if rec.get("m1_refinement"):
        lines.append(f"M1: {rec['m1_refinement'].get('reason', '')}")
    lines.append("Why: " + "; ".join(c["positive_evidence"][:5]))
    if c["negative_evidence"]:
        lines.append("Against: " + "; ".join(c["negative_evidence"][:3]))
    lines.append("Risk authorization: " + ("APPROVED" if ra.get("ok", True) else "DENIED"))
    return "\n".join(lines)


def notable_rejection(rec, min_score):
    cands = [c for c in rec["candidates"] if c["status"] == "REJECTED" and c["score"] >= min_score]
    return max(cands, key=lambda c: c["score"]) if cands else None


def fmt_rejected(rec, c):
    lines = [f"NO TRADE - considered {c['direction']} {c['setup']} ({c['style']}), score {c['score']}",
             f"Levels {c['entry']} / SL {c['sl']} / TP {c['tp']} (R:R {c['rr']})",
             "Blocked by: " + "; ".join(f"{b['code']}: {b['message']}" for b in c["blockers"][:5]),
             f"Regime {(rec.get('regime') or {}).get('label', '?')} | session {rec['session']}",
             f"MTF: {_mtf(c)}"]
    if c["barriers"]:
        lines.append("Barriers: " + ", ".join(f"{b['name']} {b['price']:.2f} ({b['r']}R)" for b in c["barriers"][:3]))
    return "\n".join(lines)


def fmt_close(t):
    fmt = lambda x, f: (f.format(x) if isinstance(x, (int, float)) else "n/a")
    return "\n".join([
        f"CLOSE {'BUY' if t.get('direction', 1) > 0 else 'SELL'} {t.get('symbol') or 'MARKET'} - {t.get('setup', '?')} ({t.get('style', '?')})",
        f"Entry {fmt(t.get('entry'), '{:.2f}')}  Exit {fmt(t.get('exit'), '{:.2f}')}  P/L {fmt(t.get('net_pnl'), '{:+.2f}')}  R {fmt(t.get('r_multiple'), '{:+.2f}')}",
        f"Exit reason: {t.get('reason', '?')}",
        f"MFE {fmt(t.get('mfe_r'), '{:+.2f}')}R  MAE {fmt(t.get('mae_r'), '{:+.2f}')}R  hold {fmt(t.get('hold_h'), '{:.1f}')}h",
        f"Regime {t.get('regime', '?')} | session {t.get('session', '?')}"])


def fmt_safety(kind, detail):
    return f"SAFETY {kind}: {detail}"


def daily_stats(trades, records, day):
    closes = [t for t in trades if t.get("type") == "close" and str(t.get("t", "")).startswith(day)]
    recs = [r for r in records if str(r.get("time", "")).startswith(day)]
    rs = [t.get("r_multiple") for t in closes if isinstance(t.get("r_multiple"), (int, float))]
    by = lambda k: dict(Counter(t.get(k, "?") for t in closes))
    blockers = Counter(b for r in recs if r["final_action"] in ("WAIT", "HALT") for b in r.get("blocker_codes", []))
    rejected_cands = sum(1 for r in recs for c in r["candidates"] if c["status"] == "REJECTED")
    slips = [t.get("slippage_r") for t in closes if isinstance(t.get("slippage_r"), (int, float))]
    return {"day": day, "trades": len(closes), "wins": sum(1 for x in rs if x > 0), "losses": sum(1 for x in rs if x < 0),
            "net_r": round(sum(rs), 2), "net_pnl": round(sum(t.get("net_pnl", 0) or 0 for t in closes), 2),
            "by_setup": by("setup"), "by_regime": by("regime"), "by_session": by("session"),
            "decisions": len(recs), "rejected_candidates": rejected_cands, "top_blockers": dict(blockers.most_common(5)),
            "avg_slippage_r": round(sum(slips) / len(slips), 3) if slips else None,
            "news_vetoes": sum(1 for r in recs if "INTEL" in r.get("blocker_codes", []) or (r.get("news") or {}).get("trade_veto")),
            "news_blackouts": sum(1 for r in recs if (r.get("news") or {}).get("blackout"))}


def fmt_daily(st, health=None):
    lines = [f"DAILY REPORT {st['day']}",
             f"Trades {st['trades']} (W {st['wins']} / L {st['losses']}), net {st['net_r']:+.2f}R / {st['net_pnl']:+.2f}",
             f"By setup {st['by_setup']} | by regime {st['by_regime']} | by session {st['by_session']}",
             f"Decisions {st['decisions']}, rejected candidates {st['rejected_candidates']}, top blockers {st['top_blockers']}",
             f"Execution: avg slippage {st['avg_slippage_r']}R",
             f"News/AI influence: {st['news_vetoes']} vetoes, {st['news_blackouts']} blackout decisions"]
    if health:
        lines.append(f"Health: {health}")
    return "\n".join(lines)


class TelegramNotifier:
    """Subscribes to the event bus and sends messages built from the records (alerts handles delivery)."""

    def __init__(self, cfg, alerts, bus):
        self.cfg, self.alerts = cfg, alerts
        self._last_rej = {}
        bus.subscribe("record", self.on_record)
        bus.subscribe("trade_close", lambda _, t: self.alerts.send("INFO", fmt_close(t), key=f"close{t.get('ticket')}"))
        bus.subscribe("safety", lambda _, p: self.alerts.send(p.get("level", "WARN"), fmt_safety(p["kind"], p["detail"]), key="safety" + p["kind"]))
        bus.subscribe("daily", lambda _, p: self.alerts.send("INFO", fmt_daily(p["stats"], p.get("health")), key="daily" + p["stats"]["day"]))

    def on_record(self, _, rec):
        if rec["stage"] == "EXECUTED":
            msg = fmt_open(rec)
            if msg:
                self.alerts.send("INFO", msg, key="open" + rec["id"])
            return
        if rec["stage"] != "DECISION" or not self.cfg.features.rejected_alerts:
            return
        c = notable_rejection(rec, self.cfg.notify.rejected_min_score)
        if c is None:
            return
        k = (c["style"], c["direction"], c["blockers"][0]["code"] if c["blockers"] else "")
        if time.time() - self._last_rej.get(k, 0) < 60 * self.cfg.notify.rejected_cooldown_min:
            return
        self._last_rej[k] = time.time()
        self.alerts.send("INFO", fmt_rejected(rec, c), key="rej" + "|".join(map(str, k)))
