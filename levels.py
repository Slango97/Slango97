"""Setup-aware stops and targets. Risk percentage stays constant; a wider stop simply means a smaller position.

Trend continuation / pullbacks -> next structural swing
Liquidity sweep reversal       -> opposing liquidity pool
Breakout retest                -> measured move of the prior range
Mean reversion                 -> middle of the range
"""

TREND_SETUPS = ("TREND_PULLBACK", "TREND_CONTINUATION", "BOS_PULLBACK", "ORDER_BLOCK_RETEST", "FVG_RETEST")


def _stop(d, price, zone, vz, regime, lmap, sweep, cfg, spread):
    st, ex = cfg.strategy, cfg.execution
    atr = vz["atr"]
    mult = st.high_vol_pad_mult if (vz["vol_regime"] == "HIGH" or regime == "EXPANSION") else \
        st.low_vol_pad_mult if vz["vol_regime"] == "LOW" else 1.0
    pad = st.zone_pad_atr * atr * mult
    sl = zone["bottom"] - pad if d > 0 else zone["top"] + pad
    if sweep and sweep["dir"] == d:
        ext = sweep["extreme"] - d * st.sweep_sl_pad_atr * atr
        sl = min(sl, ext) if d > 0 else max(sl, ext)
    notes = []
    if lmap is not None:                    # do not leave the stop sitting on an obvious pool of stops
        for p in lmap.stop_side(d):
            if abs(p.level - sl) <= 0.3 * atr and p.weight >= 8:
                sl = p.level - d * 0.15 * atr
                notes.append(f"stop moved beyond {p.kind}")
                break
    min_dist = max(ex.min_stop_atr * atr, ex.spread_stop_mult * spread)
    if abs(price - sl) < min_dist:
        sl = price - d * min_dist
    return sl, notes


def limit_price(dec, price, atr, cfg):
    """Zone-midpoint limit if it improves on the market by at least limit_min_improve_atr; else None (use market).
    The stop and target stay where the decision put them, so reward/risk only improves."""
    z = dec.invalidation.get("zone") if dec.invalidation else None
    if not z:
        return None
    mid = (z[0] + z[1]) / 2
    d = dec.direction
    if (price - mid) * d < cfg.execution.limit_min_improve_atr * atr or (mid - dec.sl) * d <= 0:
        return None
    return round(mid, 2)


MACRO_BARRIERS = {1: ("PMH", "PYH", "CMH", "CYH"), -1: ("PML", "PYL", "CML", "CYL")}


def barriers(d, price, risk, vz, vs, macro, cfg):
    """Structural barriers between entry and target: strong S/R, opposing higher-timeframe zones and macro extremes.
    Each is expressed numerically (price and distance in R)."""
    key = lambda x: (x - price) * d
    out = [{"name": f"S/R x{z['touches']} ({v['tf']})", "price": z["level"]} for v in (vz, vs) for z in v["sr"]
           if z["touches"] >= 3 and key(z["level"]) > 0]
    if cfg.features.target_feasibility_v4:
        opp = "bear" if d > 0 else "bull"
        for z in vs["ob"] + vs["fvg"]:
            edge = z["bottom"] if d > 0 else z["top"]
            if z["type"] == opp and key(edge) > 0 and z["quality"] >= 50 and z.get("tests", 0) <= 2:
                out.append({"name": f"{vs['tf']} {'order block' if 'disp' in z else 'FVG'}", "price": edge})
        for k in MACRO_BARRIERS[d]:
            if macro and k in macro and key(macro[k]) > 0:
                out.append({"name": k, "price": macro[k]})
    for b in out:
        b["r"] = round(key(b["price"]) / risk, 2)
    return sorted(out, key=lambda b: b["r"])


def compute(setup, d, price, zone, vz, vs, lmap, regime_label, S, cfg, spread, sweep=None, event=None, macro=None):
    """Return (levels_dict, None) or (None, reject_reason)."""
    ex, st = cfg.execution, cfg.strategy
    atr = vz["atr"]
    # Decision price is the midpoint; actual XAUUSD entry pays ask for BUY / bid for SELL.
    # Compute stop/target geometry from the executable side so research does not overstate RR.
    trade_price = price + d * 0.5 * spread
    sl, notes = _stop(d, trade_price, zone, vz, regime_label, lmap, sweep, cfg, spread)
    risk = abs(trade_price - sl)
    if risk > ex.max_stop_atr * atr:
        return None, f"stop {risk / atr:.1f} ATR too wide"
    if risk <= 0 or spread / risk > ex.max_spread_risk_frac:
        return None, "spread too large relative to stop"
    min_rr = st.min_rr_mean_reversion if setup == "MEAN_REVERSION" else S.min_rr
    key = lambda x: (x - trade_price) * d
    bars_ = barriers(d, trade_price, risk, vz, vs, macro, cfg)
    if setup != "MEAN_REVERSION" and bars_ and bars_[0]["r"] < min_rr:
        b = bars_[0]
        return None, f"target unrealistic: {b['name']} at {b['r']:.1f}R blocks the path (S/R blocks the target)"

    tp, kind = None, ""
    if setup == "LIQUIDITY_SWEEP_REVERSAL" and lmap is not None:
        for p in lmap.targets(d):
            if key(p.level) / risk >= min_rr:
                tp, kind = p.level - d * 0.1 * atr, f"opposing liquidity {p.kind}"
                break
    elif setup == "BREAKOUT_RETEST" and event is not None and vz["fib"]:
        height = vz["fib"]["hi"] - vz["fib"]["lo"]
        cand = event["level"] + d * height
        if key(cand) / risk >= min_rr:
            tp, kind = cand, "measured move"
    elif setup == "MEAN_REVERSION" and vz["fib"]:
        cand = vz["fib"]["eq"]
        if key(cand) / risk >= min_rr:
            tp, kind = cand, "range mid"
    if tp is None and setup in TREND_SETUPS + ("BREAKOUT_RETEST", "LIQUIDITY_SWEEP_REVERSAL"):
        swings = [p for _, p in (vz["swing_highs"] if d > 0 else vz["swing_lows"])[-10:]]
        swings += [p for _, p in (vs["swing_highs"] if d > 0 else vs["swing_lows"])[-6:]]
        for x in sorted((x for x in swings if key(x) > 0), key=key):
            if key(x) / risk >= min_rr:
                tp, kind = x - d * 0.05 * atr, "structure"
                break
    if tp is None:
        if setup == "MEAN_REVERSION":
            return None, "no room to the range middle"
        tp, kind = trade_price + d * risk * (min_rr + S.max_rr) / 2, "ATR projection"
    rr = key(tp) / risk
    if rr > S.max_rr:
        tp, rr = trade_price + d * risk * S.max_rr, S.max_rr
        kind += " (capped)"
    if cfg.features.target_feasibility_v4 and setup != "MEAN_REVERSION":
        ahead = [b for b in bars_ if 0 < b["r"] < rr]
        if ahead:
            b = ahead[0]
            tp = b["price"] - d * 0.1 * atr
            rr = key(tp) / risk
            kind += f" (capped before {b['name']})"
    if rr < min_rr:
        return None, f"reward/risk {rr:.2f} below {min_rr}"
    return {"sl": sl, "tp": tp, "rr": round(rr, 2), "risk": risk, "target_kind": kind, "notes": notes,
            "barriers": bars_[:4], "invalidation": {"stop": round(sl, 2), "zone": [round(zone["bottom"], 2), round(zone["top"], 2)], "entry_side": round(trade_price, 2)}}, None
