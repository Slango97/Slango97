"""The documents' pre-trade checklists, evaluated for every entry decision and written into its decision record:
the Blueprint's "Master Pre-Flight Operational Checklist" and the strategy's printed trading checklist.
Status: PASS / FAIL / INFO (INFO = the data needed to verify it is not available, e.g. no DXY feed)."""


def _item(group, check, status, detail):
    return {"group": group, "check": check, "status": status, "detail": detail}


def checklist(dec, ms, news, cfg):
    d = dec.direction
    comp = dec.components or {}
    st = cfg.strategy
    out = []
    if news is None:
        out.append(_item("Macro", "No high-impact (red) news on the instrument's currencies within the window", "INFO", "no news context"))
    else:
        nxt = getattr(news, "next_event", "") or "none scheduled"
        out.append(_item("Macro", f"No high-impact (red) news within {cfg.events.high_impact_pre_min:.0f} min / not in its reaction window",
                         "FAIL" if news.blackout else "PASS", news.blackout_reason or f"next: {nxt}"))
    aux = getattr(ms, "aux", None) or {}
    anchors = [aux[k] for k in ("usd_index", "us10y") if k in aux]
    if anchors:
        s = sum(anchors) * d
        out.append(_item("Macro", "DXY and US10Y support the direction", "PASS" if s > 0.1 else "FAIL" if s < -0.3 else "INFO",
                         ", ".join(f"{k} {aux[k]:+.2f}" for k in ("usd_index", "us10y") if k in aux)))
    else:
        out.append(_item("Macro", "DXY and US10Y support the direction", "INFO", "no DXY/US10Y feed (FEATURES__MACRO_FEEDS)"))
    geo = (getattr(news, "rules_families", None) or {}).get("geopolitics") if news is not None else None
    out.append(_item("Macro", "Risk-on / risk-off environment fits the trade",
                     "INFO" if geo is None else ("PASS" if geo * d > 0 else "FAIL"),
                     "no risk-sentiment headlines" if geo is None else f"geopolitical headlines vote {geo:+d} for this instrument"))
    htf = getattr(dec, "htf_bias", 0.0) or 0.0
    out.append(_item("Structural", "Higher-timeframe trend aligned (D1 / H4)",
                     "PASS" if htf * d > 0 or comp.get("mtf", 0) >= 0.5 * st.w_mtf else "FAIL",
                     f"HTF bias {htf:+.2f}, MTF score {comp.get('mtf', 0):.1f}/{st.w_mtf:.0f}"))
    out.append(_item("Structural", "Price at a liquidity pool / supply-demand zone",
                     "PASS" if comp.get("zone", 0) >= 0.4 * st.w_zone or comp.get("liquidity", 0) > 0 else "FAIL",
                     f"zone {comp.get('zone', 0):.1f}/{st.w_zone:.0f}, liquidity {comp.get('liquidity', 0):.1f}"))
    trig = [r for r in (dec.reasons or []) if any(k in r for k in ("divergence", "reclaimed", "lost 70", "Stochastic", "swept"))]
    ok_trig = bool(dec.triggered or dec.confirmed)
    out.append(_item("Trigger", "Lower-timeframe structural shift (+ RSI / Stochastic exhaustion)",
                     "PASS" if ok_trig else "FAIL",
                     ("confirmed" if dec.confirmed else "triggered" if dec.triggered else "no LTF confirmation")
                     + (" | " + "; ".join(trig[:2]) if trig else "")))
    stop_ok = bool(dec.invalidation) and dec.sl and (dec.entry - dec.sl) * d > 0
    out.append(_item("Risk", "Stop behind the structural invalidation level", "PASS" if stop_ok else "FAIL",
                     f"SL {dec.sl}" + (f", zone {dec.invalidation.get('zone')}" if dec.invalidation else "")))
    out.append(_item("Risk", "Risk per trade within 1-2% of equity", "PASS" if 0 < dec.risk_pct <= 2.0 else "FAIL",
                     f"{dec.risk_pct:.2f}% requested (the risk engine sizes from the stop distance)"))
    risk = abs(dec.entry - dec.sl) or 1e-9
    tp1 = getattr(dec, "tp1", None)
    r1 = abs(tp1 - dec.entry) / risk if tp1 else dec.rr
    out.append(_item("Execution", "TP1 reward:risk at least 1:1", "PASS" if r1 >= st.tp1_min_r else "FAIL",
                     f"TP1 {tp1 if tp1 else dec.tp} = {r1:.2f}R, final target {dec.rr:.2f}R"))
    return out


def summary(items):
    c = {k: sum(1 for i in items if i["status"] == k) for k in ("PASS", "FAIL", "INFO")}
    return f"{c['PASS']} pass / {c['FAIL']} fail / {c['INFO']} info"
