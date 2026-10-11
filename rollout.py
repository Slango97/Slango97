"""Enforced deployment ladder (V5.10, roadmap step 5): DRY_RUN -> DEMO -> LIVE_PILOT -> LIVE_FULL.

Before V5.10 the promotion path was a table in the README; a LIVE account needed only CONFIRM_LIVE. Now:

* every stage is judged from the system's OWN journals of that stage (trades, simulated entries, incidents);
* `python rollout.py promote --to <STAGE> --by <name>` succeeds only when every criterion passes, and records
  who promoted, when, the evidence snapshot (hash-chained) and - for live stages - the real account login/server;
* LIVE mode refuses to start (HALTED) unless the connected account was promoted to LIVE_PILOT or LIVE_FULL;
* LIVE_PILOT trades at most ROLLOUT__PILOT_RISK_PCT per trade with ROLLOUT__PILOT_MAX_OPEN_TRADES positions;
* the engine reviews live stages daily and DEMOTES automatically (LIVE_FULL -> LIVE_PILOT; a failing pilot halts
  with the durable safety latch). Promotions are manual; demotions are automatic - risk only goes down by itself.

    python rollout.py status                      # checklist for the next stage
    python rollout.py promote --to DEMO --by Salim
    python rollout.py promote --to LIVE_PILOT --by Salim --login 1234567 --server Broker-Live
    python rollout.py demote --to DEMO --reason "review"
"""
import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

STAGES = ("DRY_RUN", "DEMO", "LIVE_PILOT", "LIVE_FULL")
LIVE_STAGES = ("LIVE_PILOT", "LIVE_FULL")
OPERATOR_HALTS = ("file present", "explicit resume", "flattened")
_CACHE = {}


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _parse(t):
    try:
        d = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


# ---------------------------------------------------------------- state


def load(cfg):
    p = Path(cfg.rollout.state_file)
    try:
        mt = p.stat().st_mtime
    except OSError:
        return None
    hit = _CACHE.get(str(p))
    if hit and hit[0] == mt:
        return hit[1]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        data = None
    _CACHE[str(p)] = (mt, data)
    return data


def save(cfg, state):
    p = Path(cfg.rollout.state_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, p)
    _CACHE.pop(str(p), None)
    return state


def ensure(cfg, data_dirs=None):
    """Current state, creating DRY_RUN (since the first journal entry, else now) on first use."""
    st = load(cfg)
    if isinstance(st, dict) and st.get("stage") in STAGES:
        return st
    since = _now()
    for d in data_dirs or [cfg.system.data_dir]:
        ev = Path(d) / "events.jsonl"
        if ev.exists():
            try:
                first = _parse(json.loads(ev.open(encoding="utf-8").readline()).get("t"))
                since = min(since, first) if first else since
            except Exception:
                pass
    return save(cfg, {"stage": "DRY_RUN", "since": _iso(since), "account": None, "history": []})


def stage(cfg):
    st = load(cfg)
    return st.get("stage", "DRY_RUN") if isinstance(st, dict) else "DRY_RUN"


# ---------------------------------------------------------------- evidence from the journals


def _jsonl(path):
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def default_dirs(cfg):
    """The instance's data dir plus every per-market sub-folder with a journal (START_MULTI uses data\\SYMBOL)."""
    root = Path(cfg.system.data_dir)
    dirs = [root]
    if root.exists():
        dirs += [p for p in sorted(root.iterdir()) if p.is_dir() and (p / "events.jsonl").exists()]
    return dirs


def stage_evidence(cfg, since, data_dirs=None, until=None):
    """Everything the ladder judges, collected from every instance's journal inside [since, until)."""
    since_dt, until_dt = _parse(since), _parse(until) if until else None
    inside = lambda t: (lambda d: d is not None and d >= since_dt and (until_dt is None or d < until_dt))(_parse(t))
    closes, signals, incidents, heartbeats = [], 0, [], 0
    for d in data_dirs or default_dirs(cfg):
        for r in _jsonl(Path(d) / "trades.jsonl"):
            if r.get("type") == "close" and inside(r.get("t")) and isinstance(r.get("r_multiple"), (int, float)):
                closes.append(r)
        for e in _jsonl(Path(d) / "events.jsonl"):
            if not inside(e.get("t")):
                continue
            k = e.get("kind")
            if k == "dry_run_trade":
                signals += 1
            elif k == "heartbeat":
                heartbeats += 1
            elif k == "lifecycle_engine" and e.get("new") == "HALTED" and not any(x in str(e.get("reason", "")) for x in OPERATOR_HALTS):
                incidents.append(f"HALT: {e.get('reason')}")
            elif k == "order_failed" and e.get("ambiguous"):
                incidents.append("order outcome unknown")
            elif k == "sl_restored":
                incidents.append("position found without stop loss")
            elif k == "trade_lost_history":
                incidents.append("trade lost from broker history")
            elif k == "post_reconnect_reconciliation" and not e.get("ok"):
                incidents.append("reconciliation failed after reconnect")
    closes.sort(key=lambda r: r.get("t", ""))
    days = max(0.0, ((until_dt or _now()) - since_dt).total_seconds() / 86400) if since_dt else 0.0
    return {"days": round(days, 2), "closes": closes, "signals": signals, "incidents": incidents, "heartbeats": heartbeats}


def trade_metrics(closes, risk_pct):
    import pandas as pd

    from market_research import acceptance_metrics
    if not closes:
        return {"n": 0}
    df = pd.DataFrame({"open_time": [c["t"] for c in closes], "close_time": [c["t"] for c in closes],
                       "r": [float(c["r_multiple"]) for c in closes]})
    m = acceptance_metrics(df, risk_pct)
    sl = [abs(c["slippage_r"]) for c in closes if isinstance(c.get("slippage_r"), (int, float))]
    m["mean_abs_slippage_r"] = round(sum(sl) / len(sl), 4) if sl else None
    return m


# ---------------------------------------------------------------- criteria


def _check(name, ok, value, need):
    return {"criterion": name, "ok": bool(ok), "value": value, "need": need}


def criteria(cfg, target, data_dirs=None, state=None):
    """Checklist for promoting the CURRENT stage to `target` (must be the next rung)."""
    rc = cfg.rollout
    st = state or ensure(cfg, data_dirs)
    cur = st["stage"]
    if target not in STAGES or STAGES.index(target) != STAGES.index(cur) + 1:
        return [_check("next rung only", False, f"{cur} -> {target}", f"{cur} -> {STAGES[min(STAGES.index(cur) + 1, 3)]}")]
    ev = stage_evidence(cfg, st["since"], data_dirs)
    out = [_check("no critical incidents in this stage", len(ev["incidents"]) <= rc.max_incidents,
                  ev["incidents"][:5] or 0, f"<= {rc.max_incidents}"),
           _check("configuration valid", not [e for e in cfg.validate() if "CONFIRM_LIVE" not in e],
                  [e for e in cfg.validate() if "CONFIRM_LIVE" not in e][:3] or "ok", "no errors")]
    if target == "DEMO":
        out += [_check("days in DRY_RUN", ev["days"] >= rc.dry_run_min_days, ev["days"], f">= {rc.dry_run_min_days}"),
                _check("simulated entries", ev["signals"] >= rc.dry_run_min_signals, ev["signals"], f">= {rc.dry_run_min_signals}")]
        return out
    risk = rc.pilot_risk_pct if cur == "LIVE_PILOT" else cfg.risk.risk_per_trade_pct
    m = trade_metrics(ev["closes"], risk)
    days_need, n_need, pf_need, dd_max = ((rc.demo_min_days, rc.demo_min_trades, rc.demo_min_pf, rc.demo_max_dd_pct)
                                          if target == "LIVE_PILOT" else
                                          (rc.pilot_min_days, rc.pilot_min_trades, rc.pilot_min_pf, rc.pilot_max_dd_pct))
    pf = m.get("pf")
    out += [_check(f"days in {cur}", ev["days"] >= days_need, ev["days"], f">= {days_need}"),
            _check("closed trades", m.get("n", 0) >= n_need, m.get("n", 0), f">= {n_need}"),
            _check("profit factor", pf is not None and pf >= pf_need, pf, f">= {pf_need}"),
            _check("expectancy (R)", (m.get("exp_r") or 0) > 0, m.get("exp_r"), "> 0"),
            _check("max drawdown %", m.get("n", 0) > 0 and m.get("max_dd_pct", 100) < dd_max, m.get("max_dd_pct"), f"< {dd_max}")]
    if target == "LIVE_PILOT":
        sl = m.get("mean_abs_slippage_r")
        out.append(_check("execution slippage (mean |R|)", sl is not None and sl <= rc.demo_max_slippage_r, sl,
                          f"<= {rc.demo_max_slippage_r}"))
        if cfg.brain.require_market_evidence or Path(cfg.brain.market_evidence_file).exists():
            from market_evidence import load as load_ev
            mk = (load_ev(cfg.brain.market_evidence_file) or {}).get("markets", {})
            passed = [k for k, v in mk.items() if v.get("status") == "PASS"]
            out.append(_check("markets with walk-forward PASS", bool(passed), passed or 0, ">= 1"))
        if cfg.features.use_llm:
            from intel_evidence import news_verdict
            v = news_verdict(cfg)[0]
            out.append(_check("AI news layer not HARMFUL", v != "HARMFUL", v, "not HARMFUL"))
    if target == "LIVE_FULL":
        demo = next((h for h in reversed(st.get("history", [])) if h.get("to") == "LIVE_PILOT"), None)
        demo_exp = ((demo or {}).get("evidence") or {}).get("metrics", {}).get("exp_r")
        gap_ok = demo_exp is None or (m.get("exp_r") or 0) >= demo_exp - rc.pilot_max_expectancy_gap_r
        out.append(_check("live pilot close to DEMO expectancy", gap_ok, {"pilot": m.get("exp_r"), "demo": demo_exp},
                          f">= demo - {rc.pilot_max_expectancy_gap_r}R"))
    return out


# ---------------------------------------------------------------- transitions


def _record(st, entry):
    prev = st["history"][-1]["hash"] if st.get("history") else ""
    entry["hash"] = hashlib.sha256((prev + json.dumps(entry, sort_keys=True, default=str)).encode()).hexdigest()[:16]
    st.setdefault("history", []).append(entry)


def promote(cfg, target, by, login=None, server=None, data_dirs=None):
    """(ok, message). Records the promotion only when EVERY criterion passes."""
    st = ensure(cfg, data_dirs)
    checks = criteria(cfg, target, data_dirs, st)
    failed = [c for c in checks if not c["ok"]]
    if failed:
        return False, "not promoted: " + "; ".join(f"{c['criterion']} = {c['value']} (need {c['need']})" for c in failed)
    if not by:
        return False, "not promoted: --by <name> is required (who takes responsibility)"
    if target == "LIVE_PILOT" and not login:
        return False, "not promoted: LIVE_PILOT needs --login (and --server) of the REAL account it applies to"
    ev = stage_evidence(cfg, st["since"], data_dirs)
    risk = cfg.rollout.pilot_risk_pct if st["stage"] == "LIVE_PILOT" else cfg.risk.risk_per_trade_pct
    snapshot = {"days": ev["days"], "signals": ev["signals"], "metrics": trade_metrics(ev["closes"], risk)}
    _record(st, {"at": _iso(_now()), "from": st["stage"], "to": target, "by": by, "evidence": snapshot,
                 "checks": checks})
    st["stage"], st["since"] = target, _iso(_now())
    if target == "LIVE_PILOT":
        st["account"] = {"login": str(login), "server": str(server or "")}
    save(cfg, st)
    return True, f"promoted to {target} by {by}"


def demote(cfg, target, reason, by="system"):
    st = ensure(cfg)
    if target not in STAGES or STAGES.index(target) >= STAGES.index(st["stage"]):
        return False, f"demotion must go down: {st['stage']} -> {target}"
    _record(st, {"at": _iso(_now()), "from": st["stage"], "to": target, "by": by, "reason": reason})
    st["stage"], st["since"] = target, _iso(_now())
    if target not in LIVE_STAGES:
        st["account"] = None
    save(cfg, st)
    return True, f"demoted to {target}: {reason}"


# ---------------------------------------------------------------- runtime enforcement


def live_permission(cfg, login, server=""):
    """(ok, reason) for running AGENT_MODE=LIVE on this account."""
    if cfg.system.mode != "LIVE" or not cfg.rollout.enforce_live:
        return True, "not enforced"
    st = load(cfg)
    if not isinstance(st, dict) or st.get("stage") not in LIVE_STAGES:
        return False, (f"rollout: LIVE needs a recorded LIVE_PILOT promotion (current stage "
                       f"{(st or {}).get('stage', 'DRY_RUN')}); run `python rollout.py status`")
    acct = st.get("account") or {}
    if str(acct.get("login")) != str(login) or (acct.get("server") and server and acct["server"] != str(server)):
        return False, f"rollout: account {login}@{server} is not the promoted live account {acct.get('login')}@{acct.get('server')}"
    return True, f"rollout stage {st['stage']}"


def limits(cfg):
    """Extra caps of the current stage: (max_risk_pct or None, max_open_trades or None)."""
    if cfg.system.mode == "LIVE" and stage(cfg) == "LIVE_PILOT":
        return cfg.rollout.pilot_risk_pct, cfg.rollout.pilot_max_open_trades
    return None, None


def review(cfg, data_dirs=None):
    """Daily automatic review of a live stage. Returns None, ('demote', msg) or ('halt', msg)."""
    st = load(cfg)
    if cfg.system.mode != "LIVE" or not isinstance(st, dict) or st.get("stage") not in LIVE_STAGES:
        return None
    rc = cfg.rollout
    ev = stage_evidence(cfg, st["since"], data_dirs)
    risk = rc.pilot_risk_pct if st["stage"] == "LIVE_PILOT" else cfg.risk.risk_per_trade_pct
    m_all = trade_metrics(ev["closes"], risk)
    recent = trade_metrics(ev["closes"][-rc.demote_window_trades:], risk)
    why = []
    if len(ev["incidents"]) > rc.max_incidents:
        why.append("critical incidents: " + "; ".join(ev["incidents"][:3]))
    if recent.get("n", 0) >= rc.demote_window_trades and recent.get("pf") is not None and recent["pf"] < rc.demote_pf_below:
        why.append(f"last {recent['n']} trades PF {recent['pf']} < {rc.demote_pf_below}")
    if m_all.get("n", 0) and m_all.get("max_dd_pct", 0) >= rc.demote_dd_pct:
        why.append(f"drawdown in {st['stage']} {m_all['max_dd_pct']}% >= {rc.demote_dd_pct}%")
    if not why:
        return None
    msg = "; ".join(why)
    if st["stage"] == "LIVE_FULL":
        demote(cfg, "LIVE_PILOT", "automatic: " + msg)
        return "demote", "LIVE_FULL -> LIVE_PILOT (automatic): " + msg
    return "halt", "LIVE_PILOT failed its review: " + msg


# ---------------------------------------------------------------- CLI


def _print_status(cfg, dirs):
    st = ensure(cfg, dirs)
    cur = st["stage"]
    print(f"Stage: {cur} since {st['since']}" + (f" | account {st['account']}" if st.get("account") else ""))
    if cur == "LIVE_FULL":
        print("Top of the ladder. Daily automatic review demotes on PF / drawdown / incidents.")
        return
    nxt = STAGES[STAGES.index(cur) + 1]
    print(f"Checklist for {nxt}:")
    for c in criteria(cfg, nxt, dirs, st):
        print(f"  [{'x' if c['ok'] else ' '}] {c['criterion']}: {c['value']} (need {c['need']})")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("--data-dirs", default="")
    p = sub.add_parser("promote")
    p.add_argument("--to", required=True, choices=STAGES[1:])
    p.add_argument("--by", required=True)
    p.add_argument("--login")
    p.add_argument("--server", default="")
    p.add_argument("--data-dirs", default="")
    d = sub.add_parser("demote")
    d.add_argument("--to", required=True, choices=STAGES[:-1])
    d.add_argument("--reason", required=True)
    a = ap.parse_args()
    from config import Config
    cfg = Config.from_env()
    dirs = [x.strip() for x in getattr(a, "data_dirs", "").split(",") if x.strip()] or None
    if a.cmd == "status":
        _print_status(cfg, dirs)
    elif a.cmd == "promote":
        ok, msg = promote(cfg, a.to, a.by, a.login, a.server, dirs)
        print(msg)
        raise SystemExit(0 if ok else 1)
    else:
        ok, msg = demote(cfg, a.to, a.reason, by="operator")
        print(msg)
        raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
