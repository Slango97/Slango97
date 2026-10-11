"""Evidence for (or against) the AI layers — V5.9, roadmap step 4.

Measures, out of sample and after the fact, three things the system lets AI influence:

1. **News intelligence direction** (data/intel_log.jsonl): did price move the way each LLM view said, over the
   view's own validity (60 / 240 min horizons, capped at its expiry)? Moves are measured in ATR units so
   markets are comparable; moves smaller than 0.25 ATR count as "no call". Verdict uses a one-sided exact
   binomial test against a coin (50%).
2. **News vetoes** (decision_records.jsonl candidates blocked with code INTEL): what those trades would have
   returned (first touch of SL or TP on later bars; SL wins a same-bar tie; open trades are marked to market
   after 48 h). Vetoes that mostly block winners are a cost, not protection.
3. **V5 brain vetoes** (v5_brain.jsonl runs where the deterministic kernel wanted to trade but the Chief /
   LLM layer said BLOCK or NO_TRADE): the same counterfactual. This is the evidence to look at before
   setting BRAIN__ENFORCE=true.

Scored items are appended once to data/intel_outcomes.jsonl (so evidence accumulates beyond the broker's bar
history) and the aggregate verdict goes to data/intel_evidence.json, which `intel_evidence.py` reads:
an LLM view may ADD score only when the news-intel verdict is PROVEN.

    python intel_evaluation.py --history history          # offline, prices from export_history.py output
The engine also runs it once a day with live MT5 prices.
"""
import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from correlation_matrix import market_key

MIN_MOVE_ATR = 0.25
MAX_HOLD_H = 48


# ---------------------------------------------------------------- statistics


def binom_tail_ge(k, n, p=0.5):
    """P(X >= k) for X ~ Binomial(n, p) (exact, log-space)."""
    if n <= 0 or k <= 0:
        return 1.0
    lp, lq = math.log(p), math.log(1 - p)
    terms = [math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq
             for i in range(max(0, k), n + 1)]
    if not terms:
        return 0.0
    m = max(terms)
    return min(1.0, math.exp(m) * sum(math.exp(t - m) for t in terms))


def mean_t(values):
    v = np.asarray([x for x in values if x is not None and math.isfinite(x)], dtype=float)
    if len(v) < 2:
        return (float(v.mean()) if len(v) else None), None, len(v)
    sd = float(v.std(ddof=1))
    return float(v.mean()), (float(v.mean() / (sd / math.sqrt(len(v)))) if sd > 0 else None), len(v)


# ---------------------------------------------------------------- price helpers


def _naive(t):
    t = pd.Timestamp(t)
    return t.tz_convert(None) if t.tzinfo is not None else t


def _atr(df, i, n=14):
    lo = max(1, i - 3 * n)
    h, low, c = df["high"].values[lo:i + 1], df["low"].values[lo:i + 1], df["close"].values[lo - 1:i]
    tr = np.maximum(h - low, np.maximum(abs(h - c), abs(low - c)))
    return float(tr[-n:].mean()) if len(tr) else None


def score_opinion(op, df, horizons):
    """Directional outcome of one LLM view (None when the price data does not cover it yet)."""
    if df is None or len(df) < 30:
        return None
    t0 = _naive(op["t"])
    idx = df.index
    i0 = idx.searchsorted(t0)                       # first bar starting at/after the opinion (no look-ahead)
    if i0 >= len(idx) or i0 < 15:
        return None
    atr = _atr(df, i0 - 1)
    if not atr or atr <= 0:
        return None
    p0 = float(df["open"].iloc[i0])
    out = {"id": op["id"], "kind": "opinion", "t": str(op["t"]), "instrument": market_key(op.get("instrument")),
           "bias": op.get("bias", 0), "confidence": op.get("confidence"), "trade_veto": bool(op.get("trade_veto")),
           "risk_level": op.get("risk_level"), "moves_atr": {}}
    for h in horizons:
        hh = min(h, float(op.get("expiry_minutes") or h))
        t1 = t0 + pd.Timedelta(minutes=hh)
        if idx[-1] < t1:
            return None                                # horizon not finished yet: score later
        i1 = max(i0, idx.searchsorted(t1, side="right") - 1)
        w = df.iloc[i0:i1 + 1]
        out["moves_atr"][str(h)] = {"close": round((float(df["close"].iloc[i1]) - p0) / atr, 3),
                                    "range": round((float(w["high"].max()) - float(w["low"].min())) / atr, 3)}
    return out


def counterfactual_r(t, direction, entry, sl, tp, df, max_hold_h=MAX_HOLD_H):
    """R of a candidate that was NOT taken: first touch of SL/TP after t (SL wins ties), else marked at max hold.
    None when the data does not yet cover the outcome."""
    if df is None or not all(isinstance(x, (int, float)) for x in (entry, sl, tp)) or direction not in (1, -1):
        return None
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    t0 = _naive(t)
    i0 = df.index.searchsorted(t0, side="right")
    end = t0 + pd.Timedelta(hours=max_hold_h)
    if i0 >= len(df):
        return None
    w = df.iloc[i0:]
    w = w[w.index <= end]
    for hi, lo in zip(w["high"].values, w["low"].values):
        hit_sl = lo <= sl if direction > 0 else hi >= sl
        hit_tp = hi >= tp if direction > 0 else lo <= tp
        if hit_sl:
            return -1.0
        if hit_tp:
            return round(abs(tp - entry) / risk, 3)
    if df.index[-1] < end:
        return None                                    # still "open": score later
    return round((float(w["close"].iloc[-1]) - entry) * direction / risk, 3) if len(w) else None


# ---------------------------------------------------------------- collection


def _read_jsonl(path):
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def collect(cfg, prices_for, data_dir=None):
    """Score every not-yet-scored item whose outcome is now known; append to the outcomes store. Returns new rows."""
    ic = cfg.intel
    ddir = Path(data_dir or cfg.system.data_dir)
    store = Path(ic.outcomes_file)
    done = {r.get("key") for r in _read_jsonl(store)}
    new = []
    frames = {}

    def frame(sym):
        k = market_key(sym)
        if k not in frames:
            try:
                frames[k] = prices_for(sym)
            except Exception:
                frames[k] = None
        return frames[k]

    for op in _read_jsonl(ic.log_file):
        key = "op:" + str(op.get("id"))
        if key in done or not op.get("id"):
            continue
        row = score_opinion(op, frame(op.get("instrument")), ic.evidence_horizons_min)
        if row is not None:
            new.append({"key": key, **row})
    for rec in _read_jsonl(ddir / "decision_records.jsonl"):
        for j, c in enumerate(rec.get("candidates") or []):
            if not any(b.get("code") == "INTEL" for b in c.get("blockers") or []):
                continue
            key = f"veto:{rec.get('id')}:{j}"
            if key in done:
                continue
            d = 1 if c.get("direction") == "BUY" else -1
            r = counterfactual_r(rec.get("time"), d, c.get("entry"), c.get("sl"), c.get("tp"), frame(rec.get("symbol")))
            if r is not None:
                new.append({"key": key, "kind": "news_veto", "t": rec.get("time"), "instrument": market_key(rec.get("symbol")),
                            "r": r, "setup": c.get("setup"), "intel_id": (rec.get("news") or {}).get("intel_id", "")})
    for run in _read_jsonl(Path(cfg.brain.journal_file)):
        dec, rep = run.get("decision") or {}, run.get("report") or {}
        if rep.get("deterministic_action") not in ("BUY", "SELL") or dec.get("permission") not in ("BLOCK", "NO_TRADE"):
            continue
        f = dec.get("facts") or {}
        key = f"v5:{run.get('timestamp')}:{run.get('symbol')}"
        if key in done:
            continue
        d = 1 if rep["deterministic_action"] == "BUY" else -1
        r = counterfactual_r(run.get("timestamp"), d, f.get("entry"), f.get("sl"), f.get("tp"), frame(run.get("symbol")))
        if r is not None:
            new.append({"key": key, "kind": "v5_veto", "t": run.get("timestamp"), "instrument": market_key(run.get("symbol")),
                        "r": r, "permission": dec.get("permission"), "blockers": (dec.get("blockers") or [])[:3]})
            done.add(key)
    if new:
        store.parent.mkdir(parents=True, exist_ok=True)
        with open(store, "a", encoding="utf-8") as fh:
            for row in new:
                fh.write(json.dumps(row, default=str) + "\n")
    return new


# ---------------------------------------------------------------- verdicts


def evaluate(cfg, rows=None):
    """Aggregate the outcomes store into verdicts (pure function of the stored rows)."""
    ic = cfg.intel
    rows = rows if rows is not None else _read_jsonl(ic.outcomes_file)
    ops = [r for r in rows if r.get("kind") == "opinion"]
    vetoes = [r["r"] for r in rows if r.get("kind") == "news_veto"]
    v5 = [r["r"] for r in rows if r.get("kind") == "v5_veto"]
    by_h = {}
    for h in ic.evidence_horizons_min:
        calls = [(r["bias"], r["moves_atr"][str(h)]["close"]) for r in ops
                 if r.get("bias") and str(h) in r.get("moves_atr", {})]
        decided = [(b, m) for b, m in calls if abs(m) >= MIN_MOVE_ATR]
        hits = sum(1 for b, m in decided if b * m > 0)
        n = len(decided)
        signed = [b * m for b, m in calls]
        by_h[str(h)] = {"calls": len(calls), "decided": n, "hits": hits,
                        "hit_rate": round(hits / n, 3) if n else None,
                        "p_better_than_coin": round(binom_tail_ge(hits, n), 4) if n else None,
                        "p_worse_than_coin": round(binom_tail_ge(n - hits, n), 4) if n else None,
                        "mean_signed_move_atr": round(float(np.mean(signed)), 3) if signed else None}
    primary = by_h[str(ic.evidence_horizons_min[0])]
    hi = [r for r in ops if r.get("bias") and (r.get("confidence") or 0) >= 0.7 and str(ic.evidence_horizons_min[0]) in r["moves_atr"]]
    hi_dec = [r for r in hi if abs(r["moves_atr"][str(ic.evidence_horizons_min[0])]["close"]) >= MIN_MOVE_ATR]
    hi_hit = sum(1 for r in hi_dec if r["bias"] * r["moves_atr"][str(ic.evidence_horizons_min[0])]["close"] > 0)
    veto_mean, veto_t, veto_n = mean_t(vetoes)
    vol_veto = [r["moves_atr"][str(ic.evidence_horizons_min[0])]["range"] for r in ops if r.get("trade_veto")
                and str(ic.evidence_horizons_min[0]) in r["moves_atr"]]
    vol_other = [r["moves_atr"][str(ic.evidence_horizons_min[0])]["range"] for r in ops if not r.get("trade_veto")
                 and str(ic.evidence_horizons_min[0]) in r["moves_atr"]]

    n, why = primary["decided"], []
    veto_costly = veto_n >= ic.evidence_min_vetoes and veto_mean is not None and veto_mean > 0.2 and (veto_t or 0) > 2
    if n < ic.evidence_min_samples:
        verdict = "UNPROVEN"
        why.append(f"only {n} decided direction calls (need {ic.evidence_min_samples})")
    elif primary["p_worse_than_coin"] is not None and primary["p_worse_than_coin"] < ic.evidence_alpha:
        verdict = "HARMFUL"
        why.append(f"direction calls are significantly WORSE than a coin (hit rate {primary['hit_rate']:.0%})")
    elif primary["p_better_than_coin"] < ic.evidence_alpha and (primary["mean_signed_move_atr"] or 0) > 0 and not veto_costly:
        verdict = "PROVEN"
        why.append(f"hit rate {primary['hit_rate']:.0%} on {n} calls, p={primary['p_better_than_coin']}")
    else:
        verdict = "UNPROVEN"
        why.append(f"hit rate {primary['hit_rate']:.0%} on {n} calls is not distinguishable from chance "
                   f"(p={primary['p_better_than_coin']})" if primary["hit_rate"] is not None else "no decided calls")
    if veto_costly:
        verdict = "HARMFUL" if verdict != "PROVEN" else "UNPROVEN"
        why.append(f"news vetoes blocked {veto_n} trades averaging {veto_mean:+.2f}R (t={veto_t:.1f}): they cost money")
    v5_mean, v5_t, v5_n = mean_t(v5)
    if v5_n < ic.evidence_min_vetoes:
        v5_verdict, v5_why = "UNPROVEN", f"only {v5_n} scored V5 vetoes (need {ic.evidence_min_vetoes})"
    elif v5_mean < 0 and (v5_t or 0) < -2:
        v5_verdict, v5_why = "VALUABLE", f"blocked trades averaged {v5_mean:+.2f}R (t={v5_t:.1f}): vetoes avoided losers"
    elif v5_mean > 0 and (v5_t or 0) > 2:
        v5_verdict, v5_why = "COSTLY", f"blocked trades averaged {v5_mean:+.2f}R (t={v5_t:.1f}): vetoes blocked winners"
    else:
        v5_verdict, v5_why = "NEUTRAL", f"blocked trades averaged {v5_mean:+.2f}R (t={v5_t}): no measurable effect"
    return {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "news_intel": {"verdict": verdict, "summary": "; ".join(why), "horizons": by_h,
                           "high_confidence": {"decided": len(hi_dec), "hit_rate": round(hi_hit / len(hi_dec), 3) if hi_dec else None},
                           "vetoes": {"n": veto_n, "mean_r": None if veto_mean is None else round(veto_mean, 3),
                                      "t": None if veto_t is None else round(veto_t, 2)},
                           "veto_window_range_atr": round(float(np.mean(vol_veto)), 3) if vol_veto else None,
                           "other_window_range_atr": round(float(np.mean(vol_other)), 3) if vol_other else None,
                           "opinions_scored": len(ops)},
            "v5_brain_veto": {"verdict": v5_verdict, "summary": v5_why, "n": v5_n,
                              "mean_r": None if v5_mean is None else round(v5_mean, 3),
                              "t": None if v5_t is None else round(v5_t, 2),
                              "enforce_recommendation": {"VALUABLE": "evidence supports BRAIN__ENFORCE=true",
                                                         "COSTLY": "keep BRAIN__ENFORCE=false: its vetoes block winners"}
                              .get(v5_verdict, "keep BRAIN__ENFORCE=false until the evidence is conclusive")}}


def publish(cfg, ev):
    p = Path(cfg.intel.evidence_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(ev, indent=2, default=str))
    tmp.replace(p)


def markdown(ev):
    n, v = ev["news_intel"], ev["v5_brain_veto"]
    lines = [f"# AI evidence report — {ev['generated_at'][:16]} UTC", "",
             f"## News intelligence: **{n['verdict']}**", n["summary"], "",
             "| Horizon (min) | Calls | Decided (>=0.25 ATR) | Hit rate | p(better than coin) | Mean signed move (ATR) |",
             "|---|---|---|---|---|---|"]
    for h, r in n["horizons"].items():
        lines.append(f"| {h} | {r['calls']} | {r['decided']} | {r['hit_rate']} | {r['p_better_than_coin']} | {r['mean_signed_move_atr']} |")
    lines += ["", f"High-confidence (>=0.7) calls: {n['high_confidence']}",
              f"News vetoes, counterfactual: {n['vetoes']} (positive mean R = the veto blocked winners)",
              f"Price range during veto windows vs others (ATR): {n['veto_window_range_atr']} vs {n['other_window_range_atr']}",
              "", f"## V5 brain vetoes: **{v['verdict']}**", v["summary"], f"Recommendation: {v['enforce_recommendation']}",
              "", "Influence rule: an LLM view may always reduce risk; it may add score only while this verdict is PROVEN."]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--history", default="history", help="export_history.py --markets output (prices)")
    ap.add_argument("--out", default="research/ai_evidence")
    a = ap.parse_args()
    from backtest import load_csv
    from config import Config
    from market_research import discover
    cfg = Config.from_env()
    sources = discover(a.history)

    def prices_for(sym):
        mk = market_key(sym)
        if mk not in sources:
            return None
        csv, meta = sources[mk]
        return load_csv(csv, float(meta.get("server_utc_offset_h_now") or 0.0))
    new = collect(cfg, prices_for)
    ev = evaluate(cfg)
    publish(cfg, ev)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "AI_EVIDENCE_REPORT.md").write_text(markdown(ev))
    print(f"scored {len(new)} new items; news intel: {ev['news_intel']['verdict']}; V5 vetoes: {ev['v5_brain_veto']['verdict']}")
    print(f"report: {out / 'AI_EVIDENCE_REPORT.md'}")


if __name__ == "__main__":
    main()
