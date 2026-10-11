import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("recon")
COMMENT_RE = re.compile(r"^([a-z]{2})(\d+) r([0-9.]+)$")


@dataclass
class ReconReport:
    created: list = field(default_factory=list)
    closed: list = field(default_factory=list)
    fixed: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    critical: list = field(default_factory=list)


def make_comment(code, score, risk_dist):
    """Compact order comment that lets a position be rebuilt even if state.json is lost."""
    return f"{code}{score:.0f} r{risk_dist:.2f}"[:31]


def reconcile(cfg, client, state, journal, risk, views, alerts=None, on_close=None):
    """Make internal state match MT5. Every agent position ends up with a management record,
    closed trades are accounted for from deal history, and unprotected positions are repaired
    or reported as critical."""
    rep = ReconReport()
    positions = client.positions()
    live = {str(p.ticket): p for p in positions}
    styles = cfg.strategy.styles
    by_code = {s.code: n for n, s in styles.items()}
    info = client.symbol_info()

    # 1. trades the state knows about that no longer exist in MT5 -> closed
    for t in [t for t in state.trades if t not in live]:
        st = state.trades[t]
        summ = client.deal_summary(int(t))
        if summ is None:
            st["recon_tries"] = st.get("recon_tries", 0) + 1
            if st["recon_tries"] >= 5:
                rep.issues.append(f"ticket {t}: gone from MT5 with no deal history, dropped")
                journal.event("trade_lost_history", ticket=int(t))
                state.trades.pop(t)
            continue
        d, risk_dist = st["direction"], st.get("risk") or 0.0
        r_price = (summ["exit_price"] - st["entry"]) * d / risk_dist if risk_dist > 0 else None
        r_money = summ["net"] / st["risk_money"] if st.get("risk_money") else None
        mfe, mae = st.get("mfe_r"), st.get("mae_r")
        if risk_dist > 0 and hasattr(client, "excursions") and st.get("opened_ts"):
            ex = client.excursions(d, st["entry"], st["opened_ts"], summ["exit_ts"])
            if ex:
                mfe, mae = round(max(ex[0], 0) / risk_dist, 3), round(-max(ex[1], 0) / risk_dist, 3)
        rec = dict(ticket=int(t), style=st["style"], setup=st.get("setup"), score=st.get("score"),
                            direction=d, entry=st["entry"], exit=summ["exit_price"], volume=summ["volume"],
                            net_pnl=round(summ["net"], 2), commission=summ["commission"], swap=summ["swap"],
                            r_multiple=None if r_money is None else round(r_money, 3),
                            r_price=None if r_price is None else round(r_price, 3),
                            reason=st.get("close_reason") or summ["reason"], session=st.get("session"),
                            vol_regime=st.get("vol_regime"), regime=st.get("regime"), risk_tier=st.get("risk_tier"),
                            event_phase=st.get("event_phase"), target_kind=st.get("target_kind"),
                            p_win=st.get("p_win"), exp_r=st.get("exp_r"), market_quality=st.get("market_quality"),
                            signal_price=st.get("signal_price"), requested_price=st.get("requested_price"),
                            slippage=st.get("slippage"), slippage_r=st.get("slippage_r"), spread=st.get("spread"),
                            latency_ms=st.get("latency_ms"), mfe_r=mfe, mae_r=mae, lots=st.get("lots"),
                            hold_h=round((summ["exit_ts"] - st.get("opened_ts", summ["exit_ts"])) / 3600, 2))
        journal.trade_close(**rec)
        if on_close:
            on_close(rec)
        journal.event("lifecycle", ticket=int(t), state="CLOSED", net=summ["net"], reason=summ["reason"])
        risk.record_result(summ["net"], r_money, {k: st.get(k) for k in ("regime", "setup", "session", "vol_regime",
                                                                          "slippage_r", "spread", "event_phase")})
        state.trades.pop(t)
        rep.closed.append((int(t), summ["net"]))
        if alerts and on_close is None:
            alerts.send("INFO", f"Trade {t} {st['style']} closed: {summ['net']:+.2f} ({summ['reason']})", key=f"closed{t}")

    # 2. positions MT5 has that the state does not know -> rebuild a management record
    for t, p in live.items():
        if t in state.trades:
            continue
        d = 1 if p.type == 0 else -1
        m = COMMENT_RE.match(p.comment or "")
        style = by_code.get(m.group(1), "day") if m else "day"
        if m:
            risk_dist, score = float(m.group(3)), float(m.group(2))
        elif p.sl and (p.price_open - p.sl) * d > 0:
            risk_dist, score = abs(p.price_open - p.sl), 0.0
        else:
            v = views.get(styles[style].tf) if views else None
            risk_dist, score = (v["atr"] if v else 5.0), 0.0
        sl0 = p.price_open - d * risk_dist
        state.trades[t] = {
            "style": style, "setup": "RECONSTRUCTED", "score": score, "direction": d, "entry": p.price_open,
            "sl0": sl0, "tp": p.tp, "risk": risk_dist,
            "risk_money": p.volume * client.calc_loss_per_lot(d, p.price_open, sl0), "lots": p.volume,
            "opened_ts": p.time, "be": bool(p.sl and (p.sl - p.price_open) * d >= 0), "partial": False,
            "lifecycle": "OPEN", "last_sl": p.sl or None, "reconstructed": True}
        rep.created.append(int(t))
        journal.event("reconstructed", ticket=int(t), style=style, risk=risk_dist, from_comment=bool(m))
        if alerts:
            alerts.send("WARN", f"Position {t} had no state: rebuilt as {style} (risk {risk_dist:.2f})", key=f"recon{t}")

    # 3. verify protection and detect manual changes
    for t, p in live.items():
        st = state.trades[t]
        d = st["direction"]
        if not p.sl:
            sl0 = st.get("last_sl") or st["sl0"]
            valid = (p.price_current - sl0) * d >= client.min_stop_distance()
            if valid and not client.read_only and client.modify_sltp(p, sl=sl0):
                st["last_sl"] = sl0
                rep.fixed.append(f"ticket {t}: stop loss restored at {sl0:.2f}")
                journal.event("sl_restored", ticket=int(t), sl=sl0)
                if alerts:
                    alerts.send("WARN", f"Position {t} had no stop loss: restored at {sl0:.2f}", key=f"nosl{t}")
            else:
                rep.critical.append(f"ticket {t}: NO STOP LOSS and it could not be restored")
            continue
        if st.get("last_sl") is not None and abs(p.sl - st["last_sl"]) > 2 * info.point:
            rep.issues.append(f"ticket {t}: SL {p.sl} differs from agent's last SL {st['last_sl']} (manual change adopted)")
            if (p.sl - st["entry"]) * d >= 0:
                st["be"] = True
        st["last_sl"] = p.sl
        if st.get("tp") and p.tp and abs(p.tp - st["tp"]) > 2 * info.point:
            rep.issues.append(f"ticket {t}: TP changed outside the agent ({st['tp']} -> {p.tp})")
            st["tp"] = p.tp

    state.save()
    for msg in rep.issues + rep.critical:
        log.warning("recon: %s", msg)
    return rep
