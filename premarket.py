"""Phase A (pre-market preparation) and Phase C (weekly review) from the trading documents, as reports."""
from datetime import timedelta

SESSIONS_GMT = (("Sydney", 22, 7), ("Tokyo", 0, 9), ("London", 8, 17), ("New York", 13, 22))
OVERLAPS_GMT = (("London/Tokyo overlap", 8, 9), ("London/New York overlap (peak liquidity)", 13, 17))


def _nearest(views, price, above):
    lv = []
    for tf in ("D1", "H4", "H1"):
        for z in (views.get(tf) or {}).get("sr", []) or []:
            p = z.get("level")
            if p is not None and ((p > price) if above else (p < price)):
                lv.append((abs(p - price), round(p, 2), tf, z.get("touches", 0)))
    return sorted(lv)[:2]


def build_plan(views, news, profile, now):
    """If-Then scenario plan (Blueprint Phase A): calendar audit, HTF structure, key levels, scenarios."""
    sym = getattr(profile, "symbol", "") or "instrument"
    L = [f"PRE-MARKET PLAN - {sym} - {now:%a %Y-%m-%d} (UTC)", ""]
    L.append("Sessions (GMT): " + "; ".join(f"{n} {a:02d}:00-{b:02d}:00" for n, a, b in SESSIONS_GMT))
    L.append("Focus: " + "; ".join(f"{n} {a:02d}:00-{b:02d}:00" for n, a, b in OVERLAPS_GMT))
    ev = getattr(news, "ev", None)
    day_end = now.replace(hour=23, minute=59)
    highs = [e for e in (ev.events if ev else []) if now - timedelta(hours=1) <= e["time"] <= day_end]
    meds = [e for e in (ev.medium if ev else []) if now <= e["time"] <= day_end]
    L += ["", "HIGH-IMPACT (red) - no new entries in these windows:"]
    L += [f"  {e['time']:%H:%M} {e.get('currency', '')} {e['title']} - blocked {(e['time'] - timedelta(minutes=e['before'])):%H:%M}"
          f" to {(e['time'] + timedelta(minutes=e['release'])):%H:%M}, then absorption until volatility settles" for e in highs] or ["  none"]
    L += ["MEDIUM-IMPACT (orange) - monitored, trading continues:"]
    L += [f"  {e['time']:%H:%M} {e.get('currency', '')} {e['title']}" for e in meds[:8]] or ["  none"]
    d1, h4 = views.get("D1") or {}, views.get("H4") or {}
    price = (views.get("M15") or views.get("H1") or d1).get("close")
    L += ["", f"HTF structure: D1 {d1.get('struct_trend', '?')} / H4 {h4.get('struct_trend', '?')}; price {price}"]
    if price:
        res, sup = _nearest(views, price, True), _nearest(views, price, False)
        L.append("Resistance: " + (", ".join(f"{p} ({tf} x{t})" for _, p, tf, t in res) or "none mapped"))
        L.append("Support:    " + (", ".join(f"{p} ({tf} x{t})" for _, p, tf, t in sup) or "none mapped"))
        L += ["", "IF-THEN SCENARIOS:"]
        if sup:
            s = sup[0][1]
            L.append(f"  IF price sweeps below {s} and closes back above it, THEN look for an M15 CHoCH with RSI/Stochastic "
                     f"exhaustion -> long, TP1 at the nearest structure" + (f" ({res[0][1]})" if res else ""))
        if res:
            r = res[0][1]
            L.append(f"  IF price sweeps above {r} and closes back below it, THEN look for an M15 CHoCH -> short, "
                     f"TP1 at the nearest structure" + (f" ({sup[0][1]})" if sup else ""))
            L.append(f"  IF price breaks and holds above {r}, THEN wait for the retest of {r} as support -> continuation long")
    L += ["", "Rules: max 2-5 markets; risk <= 1-2% per trade from the structural stop; two full stop-outs or -2% = day over;",
          "15-minute cooldown after any loss; no entries in red-news windows."]
    return "\n".join(L)


def weekly_review(closes, now, days=7):
    """Phase C: the documents' weekly review - win rate, expectancy, and which setups / sessions / news work."""
    t0 = now - timedelta(days=days)
    rows = [c for c in closes if c.get("type") == "close" and str(c.get("close_time") or c.get("t") or "") >= t0.isoformat()[:10]]
    if not rows:
        return f"WEEKLY REVIEW ({now:%Y-%m-%d}): no closed trades in the last {days} days."
    rs = [float(c.get("r") or 0.0) for c in rows]
    wins = [x for x in rs if x > 0]
    gl = -sum(x for x in rs if x < 0)
    L = [f"WEEKLY REVIEW ({now:%Y-%m-%d}) - {len(rs)} trades",
         f"Win rate {len(wins) / len(rs):.0%} | expectancy {sum(rs) / len(rs):+.2f}R | profit factor "
         f"{(sum(wins) / gl) if gl > 0 else float('inf'):.2f} | total {sum(rs):+.2f}R"]
    for key in ("setup", "session", "event_phase", "direction"):
        groups = {}
        for c in rows:
            groups.setdefault(str(c.get(key, "?")), []).append(float(c.get("r") or 0.0))
        best = sorted(((sum(v) / len(v), k, len(v)) for k, v in groups.items() if len(v) >= 2), reverse=True)
        if best:
            L.append(f"By {key}: best {best[0][1]} ({best[0][0]:+.2f}R, n={best[0][2]})"
                     + (f", worst {best[-1][1]} ({best[-1][0]:+.2f}R, n={best[-1][2]})" if len(best) > 1 else ""))
    losses = [c for c in rows if float(c.get("r") or 0) < 0]
    exe = [c for c in losses if abs(float(c.get("slippage_r") or 0)) > 0.15]
    L.append(f"Losses: {len(losses) - len(exe)} within plan (statistical cost of business), {len(exe)} with execution issues "
             f"(slippage > 0.15R)")
    return "\n".join(L)
