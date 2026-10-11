"""Historical simulator. Stage 1 (signals) runs the SAME analysis, regime, liquidity, microstructure and
candidate-generation code as live trading on completed candles only. Stage 2 (simulate) applies the policy,
risk rules and trade management with spread, slippage, commission and swap. Stage 1 is slow and cacheable,
stage 2 is fast, which is what makes walk-forward optimization and ablation practical (see optimize.py).

    python backtest.py --csv XAUUSD_M1.csv --timeframe M1 --offset 2 --start 2023-01-01 --out results
    python backtest.py --synthetic 12000          # plumbing check on fake data (results are meaningless)

CSV columns: time, open, high, low, close, tick_volume, spread(points, optional). Events CSV (optional):
time (UTC), title[, forecast, previous, actual].
Limits: the LLM news layer cannot be replayed; same-bar SL/TP ties resolve to the stop (conservative).
"""
import argparse
import json
import pickle
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from analysis import analyze_tf, build_state
from analytics import execution_report, format_analytics, mfe_mae_report
from config import Config
from decision import apply_policy, generate
from diagnostics import diagnose
from events import EventEngine
from indicators import validate_ohlc
from liquidity import build_map
from management import PosView, plan
from metrics import format_report, summarize
from microstructure import score_market
from news import NewsContext
from regime import classify as classify_regime
from risk import RiskCore
import multiprocessing as mp
from config import phases_for
from costs import CostModel
from indicators import add_indicators
from refine import m1_check

TFS = {"M5": ("5min", pd.Timedelta(minutes=5)), "M15": ("15min", pd.Timedelta(minutes=15)),
       "H1": ("1h", pd.Timedelta(hours=1)), "H4": ("4h", pd.Timedelta(hours=4)),
       "D1": ("1D", pd.Timedelta(days=1)), "W1": ("W-MON", pd.Timedelta(days=7))}
AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "tick_volume": "sum"}
OHLCV = ["open", "high", "low", "close", "tick_volume"]


@dataclass
class BTConfig:
    initial_balance: float = 10000.0
    contract_size: float = 100.0
    point: float = 0.01
    spread_default: float = 0.25          # price units, used when the CSV has no spread column
    slippage: float = 0.05                # adverse price units per market fill / stop
    commission_per_lot_side: float = 3.5
    swap_per_lot_night: float = 0.0
    leverage: float = 200.0
    volume_min: float = 0.01
    volume_step: float = 0.01
    volume_max: float = 50.0
    stops_level: float = 0.0
    decision_step: int = 1
    price_basis: str = "bid"              # MT5 builds CFD/forex bars from BID prices (official MT5 help); "mid" if yours are mid
    bars: int = 500
    min_tf_bars: int = 60


@dataclass
class DecisionPoint:
    i: int
    t: object
    cands: list
    rejected: list
    regime: object
    micro: object
    session: str
    news: object
    mgmt: dict
    near_event: bool = False


@dataclass
class Prepared:
    sim: object
    step: object
    idx: object
    O: object
    H: object
    L: object
    C: object
    TV: object
    SP: object
    frames: dict
    dps: list = field(default_factory=list)
    phase: object = None          # session phase per bar (for costs and session statistics)
    sp_raw: object = None         # observed spread in price units (NaN where missing)
    m1: object = None             # M1 frame (with indicators) when the input is M1


def phase_array(idx, phases):
    from config import DST_MARK, session_phase
    if phases and phases[0] == DST_MARK:
        return np.array([session_phase(t, phases) for t in idx], dtype=object)
    mins = np.asarray(idx.hour * 60 + idx.minute)
    out = np.full(len(idx), "OFF", dtype=object)
    for name, a, b in phases:
        out[(mins >= a) & (mins < b)] = name
    return out


def counterfactual(H, L, C, SP, i, d, entry, sl, tp, horizon, bid_shift=0.5):
    """What a rejected candidate would have done: first touch of stop or target within the horizon."""
    r = counterfactual_ex(H, L, C, SP, i, d, entry, sl, tp, horizon, bid_shift)
    return None if r is None else r[0]


def counterfactual_ex(H, L, C, SP, i, d, entry, sl, tp, horizon, bid_shift=0.5):
    """Triple-barrier label with side-aware execution. bid = price - bid_shift * spread, ask = bid + spread:
    bid_shift 0.0 for bid-based bars (MT5 default), 0.5 for mid-based bars. entry is the decision mid price."""
    j = min(len(C), i + 1 + horizon)
    if j <= i + 1:
        return None
    hh, ll, sp = H[i + 1:j], L[i + 1:j], SP[i + 1:j]
    entry_sp = float(SP[i]) if np.isfinite(SP[i]) else 0.0
    entry_side = entry + d * 0.5 * entry_sp
    risk = abs(entry_side - sl)
    if risk <= 0:
        return None
    spn = np.nan_to_num(sp, nan=0.0)
    if d > 0:                                            # longs exit on the bid
        exit_low, exit_high = ll - bid_shift * spn, hh - bid_shift * spn
        sl_hit, tp_hit = exit_low <= sl, exit_high >= tp
    else:                                                # shorts exit on the ask
        exit_low, exit_high = ll - bid_shift * spn + spn, hh - bid_shift * spn + spn
        sl_hit, tp_hit = exit_high >= sl, exit_low <= tp
    a = int(np.argmax(sl_hit)) if sl_hit.any() else 10 ** 9
    b = int(np.argmax(tp_hit)) if tp_hit.any() else 10 ** 9
    if a == b == 10 ** 9:
        last_sp = float(sp[-1]) if np.isfinite(sp[-1]) else 0.0
        last_exit = C[j - 1] - bid_shift * last_sp + (0.0 if d > 0 else last_sp)
        return (last_exit - entry_side) * d / risk, j - 1
    return (-1.0, i + 1 + a) if a <= b else (abs(tp - entry_side) / risk, i + 1 + b)


_WORK = {}


def _chunk(dps):
    btr, prep = _WORK["btr"], _WORK["prep"]
    return btr._signals_for(prep, dps)


def _init_worker(btr, prep):
    _WORK["btr"], _WORK["prep"] = btr, prep


def load_csv(path, server_utc_offset_h=0.0):
    df = pd.read_csv(path, sep=None, engine="python")
    df.columns = [c.strip().lower().strip("<>") for c in df.columns]
    df["time"] = pd.to_datetime(df["date"] + " " + df["time"]) if "date" in df else pd.to_datetime(df["time"])
    df = df.set_index("time").sort_index()
    df = df[~df.index.duplicated(keep="last")].rename(columns={"tickvol": "tick_volume"})
    if "tick_volume" not in df:
        df["tick_volume"] = df["vol"] if "vol" in df else 1.0
    if "spread" not in df:
        df["spread"] = np.nan
    df.index = df.index - pd.Timedelta(hours=server_utc_offset_h)
    return df[OHLCV + ["spread"]]


def load_events(path):
    ev = pd.read_csv(path)
    out = []
    for r in ev.to_dict("records"):
        t = pd.Timestamp(r["time"])
        t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
        out.append({"title": str(r.get("title", "event")), "time": t.to_pydatetime(), "forecast": r.get("forecast"),
                    "previous": r.get("previous"), "actual": r.get("actual")})
    return out


def synthetic_m5(n=12000, seed=7, start="2024-01-01"):
    """Regime-switching random walk for plumbing tests only; says nothing about real markets."""
    rng = np.random.default_rng(seed)
    drift = np.sin(np.linspace(0, 18, n)) * 0.03
    vol = 0.35 + 0.25 * (np.sin(np.linspace(0, 40, n)) > 0.6)
    close = 2000 + np.cumsum(rng.normal(drift, vol))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + rng.exponential(0.2, n)
    low = np.minimum(open_, close) - rng.exponential(0.2, n)
    idx = pd.date_range(start, periods=n, freq="5min")
    keep = idx.dayofweek < 5
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "tick_volume": rng.integers(50, 500, n).astype(float), "spread": np.full(n, 25.0)}, index=idx)[keep]


def build_frames(m5):
    frames = {}
    for tf, (rule, delta) in TFS.items():
        df = m5[OHLCV] if tf == "M5" else m5.resample(rule, label="left", closed="left").agg(AGG).dropna(subset=["open"])
        frames[tf] = (df, (df.index + delta).values)
    return frames


def swap_nights(t_open, t_close):
    off = pd.Timedelta(hours=22)
    return max(0, int(((t_close - off).floor("D") - (t_open - off).floor("D")).days))


def compact(v):
    return {"atr": v["atr"], "close": v["close"], "ema20": v["ema20"],
            "swing_lows": v["swing_lows"][-5:], "swing_highs": v["swing_highs"][-5:]}


class Backtester:
    def __init__(self, cfg, bt, events=None, calibration=None, edge_book=None, aux=None, meta_model=None):
        self.cfg, self.bt = cfg, bt
        self.events = events or []
        self.calibration, self.edge_book = calibration, edge_book
        self.costs = CostModel.load(cfg.execution.cost_model_file) if getattr(cfg.execution, "cost_model_file", "") else None
        self.costs = self.costs or CostModel()
        self.last_rejected = None
        self.candidate_rows = []
        self.aux = aux or {}                # name -> (pd.Series of values indexed by UTC time, sign)
        self.meta_model = meta_model

    # ---- stage 0: data -----------------------------------------------------
    def prepare(self, sim, step_min=5):
        clean, issues = validate_ohlc(sim[OHLCV], min_bars=60)
        if issues:
            print("data issues fixed:", issues)
        sim = sim.loc[clean.index].copy()
        sim[OHLCV] = clean[OHLCV]
        m5 = sim if step_min == 5 else sim.resample("5min").agg({**AGG, "spread": "mean"}).dropna(subset=["open"])
        step = pd.Timedelta(minutes=step_min)
        idx = sim.index
        ends = idx + step
        dmask = np.asarray((ends.minute % 5 == 0) & (ends.second == 0))
        dps = list(np.where(dmask)[0][::self.bt.decision_step])
        SP = np.where(sim["spread"].isna(), self.bt.spread_default, sim["spread"] * self.bt.point).astype(float)
        frames = build_frames(m5)
        pre = self.cfg.features.precomputed_indicators
        if pre:                                           # causal: each value depends only on earlier bars
            frames = {tf: (add_indicators(df, self.cfg.sessions.bounds), ends) for tf, (df, ends) in frames.items()}
        m1 = None
        if step_min == 1 and self.cfg.features.m1_refinement:
            m1 = add_indicators(sim[OHLCV], self.cfg.sessions.bounds) if pre else sim[OHLCV]
        return Prepared(sim, step, idx, *(sim[c].values.astype(float) for c in ("open", "high", "low", "close", "tick_volume")),
                        SP, frames, dps, phase_array(idx, phases_for(self.cfg.sessions)),
                        (sim["spread"] * self.bt.point).values.astype(float), m1)

    def spreads(self, prep):
        if not self.cfg.features.realistic_costs:
            return prep.SP
        c = self.costs
        mult = np.array([c.spread_session_mult.get(p, 1.0) for p in prep.phase])
        return np.where(np.isnan(prep.sp_raw), self.bt.spread_default * mult, prep.sp_raw) * c.spread_mult

    # ---- stage 1: signals (slow, cacheable) -------------------------------
    def signals(self, prep, progress=False, workers=0):
        """Stage 1. workers > 1 splits the decision points into chunks processed in parallel (identical results:
        every decision point depends only on data up to that point)."""
        if workers and workers > 1 and len(prep.dps) > 4 * workers:
            chunks = [list(c) for c in np.array_split(np.array(prep.dps), workers) if len(c)]
            ctx = mp.get_context("fork") if "fork" in mp.get_all_start_methods() else mp.get_context("spawn")
            with ctx.Pool(workers, initializer=_init_worker, initargs=(self, prep)) as pool:
                parts = pool.map(_chunk, chunks)
            return [dp for part in parts for dp in part]
        return self._signals_for(prep, prep.dps, progress)

    def _signals_for(self, prep, dps, progress=False):
        cfg, bt, st = self.cfg, self.bt, self.cfg.strategy
        eng = EventEngine(cfg)
        eng.set_events(self.events)
        ev_times = [pd.Timestamp(e["time"]).tz_convert(None) if pd.Timestamp(e["time"]).tzinfo else pd.Timestamp(e["time"]) for e in self.events]
        cache, views, out = {}, {}, []
        O, H, L, C, TV, SP = prep.O, prep.H, prep.L, prep.C, prep.TV, prep.SP
        phases = phases_for(cfg.sessions)
        for k, i in enumerate(dps):
            if progress and k % 2000 == 0:
                print(f"  signals {k}/{len(dps)}")
            t1 = prep.idx[i] + prep.step
            tn = t1.to_pydatetime().replace(tzinfo=timezone.utc)
            for tf, (df, ends) in prep.frames.items():
                n = int(np.searchsorted(ends, np.datetime64(t1), side="right"))
                if n < bt.min_tf_bars:
                    views.pop(tf, None)
                    continue
                key = (tf, n)
                if key not in cache:
                    for kk in [kk for kk in cache if kk[0] == tf]:
                        cache.pop(kk)
                    cache[key] = analyze_tf(df.iloc[max(0, n - bt.bars):n], tf, cfg.sessions.bounds, st.adx_bands,
                                            validate=False, phases=phases)
                views[tf] = cache[key]
            mgmt = {tf: compact(v) for tf, v in views.items()}
            near = any(abs((et - t1).total_seconds()) <= 5400 for et in ev_times)
            if len(views) < 3:
                out.append(DecisionPoint(i, t1, [], [], None, None, "OFF", None, mgmt, near))
                continue
            lo = max(0, i - 90)
            micro = score_market(cfg.micro, SP[i], float(np.median(SP[max(0, i - 60):i + 1])), O[lo:i + 1], H[lo:i + 1],
                                 L[lo:i + 1], C[lo:i + 1], TV[lo:i + 1], cfg.execution.max_spread_points * bt.point)
            es = eng.state(tn, lambda: (micro.range_ratio, micro.spread_ratio))
            news = NewsContext(status="NO_DATA", blackout=es.blackout, blackout_reason=es.reason, event_phase=es.phase,
                               event_bias=es.surprise_bias, impact="high" if es.blackout else "low")
            reg = classify_regime(views, cfg, micro, news)
            mid = C[i] + (0.5 - (0.0 if bt.price_basis == "bid" else 0.5)) * SP[i]
            ms = build_state(views, tn, cfg.sessions.bounds, SP[i], phases, regime=reg, micro=micro,
                             liquidity=build_map(views, mid), news=news)
            aux_now = None
            if self.aux:
                from macro_feeds import bias_from_series
                aux_now = {}
                for name, (ser, sign) in self.aux.items():
                    vals = ser.values[:int(np.searchsorted(ser.index.values, np.datetime64(t1), side="right"))]
                    if len(vals) > 30:
                        aux_now[name] = sign * bias_from_series(vals)
            cands, rejected = generate(ms, mid, cfg, aux_now)
            out.append(DecisionPoint(i, t1, cands, rejected[:4], reg, micro, ms.session, news, mgmt, near))
        return out

    # ---- stage 2: simulation (fast) ----------------------------------------
    def simulate(self, prep, signals, cfg=None, start=None, end=None):
        cfg = cfg or self.cfg
        bt, st, ex = self.bt, cfg.strategy, cfg.execution
        core = RiskCore(cfg)
        O, H, L, C, idx = prep.O, prep.H, prep.L, prep.C, prep.idx
        SP = self.spreads(prep)
        from risk import calendar_risk_multiplier, configured_calendar_families
        nfam = configured_calendar_families(cfg)            # vendor outages cannot be replayed: all configured vendors work
        cal_mult = calendar_risk_multiplier(cfg, nfam, nfam)[0] or 0.0
        bs = 0.0 if bt.price_basis == "bid" else 0.5        # bid = X - bs*spread; ask = bid + spread
        BID = lambda x, s: x - bs * s
        ASK = lambda x, s: x - bs * s + s
        MID = lambda x, s: x + (0.5 - bs) * s
        real = cfg.features.realistic_costs
        cost = self.costs
        cur = {"atr": 0.0, "near": False}
        slip = lambda i: cost.slippage(SP[i], cur["atr"], prep.phase[i], cur["near"]) if real else bt.slippage
        m1_on = cfg.features.m1_refinement and prep.m1 is not None
        armed, m1_stats, rejected_rows, seen, meta_rows = None, Counter(), [], {}, []
        limit_on, limit, lim_stats = cfg.features.limit_entries, None, Counter()
        through = cfg.execution.limit_fill_through_points * bt.point
        N = len(C)
        by_i = {dp.i: dp for dp in signals}
        i0 = start if start is not None else (signals[0].i if signals else 0)
        i1 = min(end if end is not None else N - 1, N - 1)
        step_min = prep.step.total_seconds() / 60
        balance = bt.initial_balance
        positions, trades, eq_t, eq_v, hist = [], [], [], [], []
        rs = {"day": None, "day0": balance, "week": None, "week0": balance, "peak": balance, "today": 0,
              "streak": 0, "paused": None, "diag": None}
        waits, pending = Counter(), None

        def floating(i):
            bid, ask = BID(C[i], SP[i]), ASK(C[i], SP[i])
            return sum(((bid if p["direction"] > 0 else ask) - p["entry"]) * p["direction"] * p["lots"]
                       * bt.contract_size for p in positions)

        def open_risk(eq):
            return 100 * sum(p["lots"] * abs(p["entry"] - p["sl"]) * bt.contract_size for p in positions
                             if (p["entry"] - p["sl"]) * p["direction"] > 0) / eq

        def be_regret(p, i_exit):
            S = st.styles[p["style"]]
            j = min(N, i_exit + 1 + int(min(S.max_hold_h * 60 / step_min, 3000)))
            if j <= i_exit + 1:
                return None
            hh, ll = H[i_exit + 1:j], L[i_exit + 1:j]
            d = p["direction"]
            spn = np.nan_to_num(SP[i_exit + 1:j], nan=0.0)
            bid_h, bid_l, ask_h, ask_l = BID(hh, spn), BID(ll, spn), ASK(hh, spn), ASK(ll, spn)
            tp_hit = bid_h >= p["tp"] if d > 0 else ask_l <= p["tp"]
            sl_hit = bid_l <= p["sl0"] if d > 0 else ask_h >= p["sl0"]     # V4.3 compared the wrong extremes
            if not tp_hit.any():
                return False
            return bool(not sl_hit.any() or np.argmax(tp_hit) < np.argmax(sl_hit))

        def close_pos(p, px, i, reason, lots=None):
            nonlocal balance
            t = idx[i] + prep.step
            lots = p["lots"] if lots is None else lots
            gross = (px - p["entry"]) * p["direction"] * lots * bt.contract_size
            net = gross - bt.commission_per_lot_side * lots * 2 + bt.swap_per_lot_night * lots * swap_nights(p["opened"], t)
            balance += net
            p["net"] += net
            p["lots"] = round(p["lots"] - lots, 8)
            if p["lots"] > 1e-9:
                return
            positions.remove(p)
            r = p["net"] / p["risk_money"] if p["risk_money"] else np.nan
            regret = be_regret(p, i) if p["be"] and "stop" in reason else None
            rec = {k: p[k] for k in ("direction", "style", "setup", "score", "technical", "session", "vol_regime", "regime",
                                     "risk_tier", "news_condition", "event_phase", "entry", "signal_price", "slippage_r",
                                     "spread", "lots0", "target_kind", "p_win", "exp_r", "conflict", "market_quality")}
            rec.update(open_time=p["opened"], close_time=t, exit=px, net=p["net"], r=r, reason=reason,
                       mfe_r=round(p["mfe"], 3), mae_r=round(p["mae"], 3), be_regret=regret,
                       hold_h=(t - p["opened"]).total_seconds() / 3600)
            trades.append(rec)
            hist.append({k: rec.get(k) for k in ("regime", "setup", "session", "vol_regime", "slippage_r", "spread", "event_phase")})
            if p["net"] < 0:
                rs["streak"] += 1
                if rs["streak"] >= cfg.risk.loss_streak_pause_at:
                    rs["paused"] = t + pd.Timedelta(hours=cfg.risk.pause_hours)
                    rs["diag"] = diagnose(hist[-rs["streak"]:], hist, cfg.risk.throttle_dominance)
            elif p["net"] > 0:
                rs["streak"] = 0
            if cfg.features.doc_rules:
                tz_ = lambda x: pd.Timestamp(x).tz_localize(None) if pd.Timestamp(x).tzinfo else pd.Timestamp(x)
                if p["net"] < 0:
                    rs["cool_until"] = tz_(t) + pd.Timedelta(minutes=cfg.risk.loss_cooldown_min)
                    rs["full_streak"] = rs.get("full_streak", 0) + 1 if r <= -cfg.risk.full_loss_r else 0
                    if rs["full_streak"] >= cfg.risk.two_loss_lock_after:
                        rs["lock_day"], rs["full_streak"] = tz_(t).date(), 0
                elif p["net"] > 0:
                    rs["full_streak"] = 0

        for i in range(i0, i1 + 1):
            t1 = idx[i] + prep.step
            if limit is not None:                              # resting limit order
                ldec, ldp, lp, exp_i = limit
                d = ldec.direction
                # a BUY limit fills when the ASK trades through it; a SELL limit when the BID does (V4.3 used the
                # opposite side, filling limits one full spread too easily)
                hit = (ASK(L[i], SP[i]) <= lp - through) if d > 0 else (BID(H[i], SP[i]) >= lp + through)
                stop_first = (BID(L[i], SP[i]) <= ldec.sl) if d > 0 else (ASK(H[i], SP[i]) >= ldec.sl)
                if hit and not (stop_first and O[i] * d <= lp * d):
                    pending, limit = (ldec, ldp, lp), None
                    lim_stats["filled"] += 1
                elif stop_first:
                    limit = None
                    lim_stats["invalidated"] += 1
                elif i >= exp_i:
                    limit = None
                    lim_stats["expired"] += 1
            if pending is not None:
                dec, dp = pending[0], pending[1]
                override = pending[2] if len(pending) > 2 else None
                pending = None
                d, spr, S = dec.direction, SP[i], st.styles[dec.style]
                atr = dp.mgmt[S.tf]["atr"]
                fill = override if override is not None else (ASK(O[i], spr) if d > 0 else BID(O[i], spr)) + d * slip(i)
                dist = abs(fill - dec.sl)
                bad = ((override is None and abs(fill - dec.entry) > ex.max_entry_drift_atr * atr + spr) or (fill - dec.sl) * d <= 0
                       or (dec.tp - fill) * d <= 0 or not (ex.min_stop_atr * atr * 0.9 <= dist <= ex.max_stop_atr * atr * 1.05))
                if not bad:
                    min_rr = st.min_rr_mean_reversion if dec.setup == "MEAN_REVERSION" else S.min_rr
                    bad = abs(dec.tp - fill) / dist < min_rr * 0.9
                if not bad:
                    eq = balance + floating(max(i - 1, 0))
                    risk_pct = dec.risk_pct * core.risk_multiplier(rs["streak"]) * cal_mult
                    lpl = dist * bt.contract_size
                    lots = core.lots_for(eq * risk_pct / 100, lpl, bt.volume_step, bt.volume_min, bt.volume_max)
                    margin = lots * bt.contract_size * fill / bt.leverage
                    used = sum(p["lots"] * bt.contract_size * p["entry"] / bt.leverage for p in positions)
                    bad = (lots <= 0 or not core.projected_ok(open_risk(eq), 100 * lots * lpl / eq)
                           or eq - used - margin < eq * cfg.risk.min_free_margin_pct / 100
                           or 100 * eq / (used + margin) < cfg.risk.min_margin_level)
                if bad:
                    waits["ENTRY_SKIPPED"] += 1
                else:
                    positions.append({
                        "direction": d, "entry": fill, "sl": dec.sl, "sl0": dec.sl, "tp": dec.tp, "tp1": getattr(dec, "tp1", None),
                        "tp1_frac": cfg.strategy.tp1_close_frac, "risk": dist,
                        "risk_money": lots * lpl, "lots": lots, "lots0": lots, "style": dec.style, "setup": dec.setup,
                        "score": dec.score, "technical": dec.technical, "session": dec.session, "vol_regime": dec.vol_regime,
                        "regime": dec.regime, "risk_tier": dec.risk_tier, "target_kind": dec.target_kind, "p_win": dec.p_win,
                        "exp_r": dec.exp_r, "conflict": dec.conflict, "market_quality": dec.market_quality,
                        "event_phase": dp.news.event_phase if dp.news else None,
                        "news_condition": "near_event" if dp.near_event else "none", "signal_price": dec.entry,
                        "slippage_r": (fill - dec.entry) * d / dist, "spread": spr, "opened": idx[i], "be": False,
                        "partial": False, "net": 0.0, "mfe": 0.0, "mae": 0.0})
                    rs["today"] += 1

            for p in list(positions):
                d, spr = p["direction"], SP[i]
                bid_h, bid_l = BID(H[i], spr), BID(L[i], spr)
                ask_h, ask_l = ASK(H[i], spr), ASK(L[i], spr)
                if d > 0:
                    fav, adv = bid_h - p["entry"], p["entry"] - bid_l
                    sl_hit, tp_hit = bid_l <= p["sl"], bid_h >= p["tp"]
                    sl_px, tp_px = min(p["sl"], BID(O[i], spr)) - slip(i), max(p["tp"], BID(O[i], spr))
                else:
                    fav, adv = p["entry"] - ask_l, ask_h - p["entry"]
                    sl_hit, tp_hit = ask_h >= p["sl"], ask_l <= p["tp"]
                    sl_px, tp_px = max(p["sl"], ASK(O[i], spr)) + slip(i), min(p["tp"], ASK(O[i], spr))
                p["mfe"] = max(p["mfe"], fav / p["risk"])
                p["mae"] = min(p["mae"], -adv / p["risk"])
                if sl_hit:
                    close_pos(p, sl_px, i, "stop (break-even/trail)" if p["be"] else "stop loss")
                elif tp_hit:
                    close_pos(p, tp_px, i, "take profit")

            if armed is not None:                              # M1 execution refinement (M1 data only)
                adec, adp, exp_i = armed
                sub = prep.m1.iloc[max(0, i - cfg.m1.bars + 1):i + 1]
                v1 = analyze_tf(sub, "M1", cfg.sessions.bounds, st.adx_bands, validate=False) if len(sub) >= 60 else None
                res = m1_check(adec, v1, MID(C[i], SP[i]), cfg)
                if res.action == "GO":
                    armed = None
                    m1_stats["go"] += 1
                    lp = None
                    if limit_on:
                        from levels import limit_price
                        lp = limit_price(adec, MID(C[i], SP[i]), adp.mgmt[st.styles[adec.style].tf]["atr"], cfg)
                    if lp is not None:
                        limit = (adec, adp, lp, i + int(cfg.execution.limit_expiry_min / step_min))
                        lim_stats["placed"] += 1
                    else:
                        pending = (adec, adp)
                elif res.action == "ABORT":
                    armed = None
                    m1_stats["abort"] += 1
                elif i >= exp_i:
                    armed = None
                    m1_stats["expired"] += 1
            dp = by_i.get(i)
            if dp is None:
                continue
            cur["atr"] = (dp.mgmt.get("M5") or {}).get("atr", cur["atr"])
            cur["near"] = dp.near_event
            tn = t1.to_pydatetime().replace(tzinfo=timezone.utc)
            day, week = str(tn.date()), str((t1 - pd.Timedelta(days=t1.weekday())).date())
            eq = balance + floating(i)
            if rs["day"] != day:
                rs.update(day=day, day0=eq, today=0)
            if rs["week"] != week:
                rs.update(week=week, week0=eq)
            rs["peak"] = max(rs["peak"], eq)
            eq_t.append(t1)
            eq_v.append(eq)

            blackout = bool(dp.news and dp.news.blackout)
            for p in list(positions):
                S = st.styles[p["style"]]
                d = p["direction"]
                price = BID(C[i], SP[i]) if d > 0 else ASK(C[i], SP[i])
                pv = PosView(d, price, p["sl"], p["lots"], (t1 - p["opened"]).total_seconds() / 3600, bt.volume_min, bt.volume_step)
                for a in plan(p, pv, dp.mgmt.get(S.trail_tf), blackout, SP[i], SP[i] + bt.stops_level, S):
                    if a.kind == "close":
                        close_pos(p, price - d * slip(i), i, a.reason)
                        break
                    if a.kind == "partial":
                        close_pos(p, price - d * slip(i), i, a.reason, lots=a.volume)
                        p["partial"] = True
                    elif a.kind == "move_sl":
                        p["sl"] = a.value
                        p["be"] = p["be"] or a.flag == "be"

            if dp.regime is None:
                continue
            paused = rs["paused"].to_pydatetime().replace(tzinfo=timezone.utc) if rs["paused"] is not None else None
            if paused is not None:
                newp = core.throttle(tn, paused, rs["diag"], dp.regime.label)
                rs["paused"] = pd.Timestamp(newp).tz_convert(None) if newp is not None else None
                paused = newp
            tnn = pd.Timestamp(tn).tz_localize(None) if pd.Timestamp(tn).tzinfo else pd.Timestamp(tn)
            if cfg.features.doc_rules and (rs.get("lock_day") == tnn.date() or (rs.get("cool_until") is not None and tnn < rs["cool_until"])):
                waits["DOC_LOSS_RULES"] += 1
                continue
            ok, why = core.gate(tn, SP[i] / bt.point, len(positions), rs["today"], 100 * (rs["day0"] - eq) / rs["day0"],
                                100 * (rs["week0"] - eq) / rs["week0"], 100 * (rs["peak"] - eq) / rs["peak"],
                                open_risk(eq), 0, paused)
            if not ok:
                waits["RISK_GATE"] += 1
                continue
            exclude = {(p["style"], p["direction"]) for p in positions}
            for d in {p["direction"] for p in positions}:
                exclude |= {(s, -d) for s in st.styles}
            cands = [replace(c, components=dict(c.components), reasons=list(c.reasons), tags=list(c.tags)) for c in dp.cands]
            ms = SimpleNamespace(session=dp.session, regime=dp.regime, micro=dp.micro)
            dec = apply_policy(cands, ms, dp.news, cfg, exclude, self.calibration, self.edge_book, dp.rejected, self.meta_model)
            waits[f"{dec.action}:{dec.wait_reason}" if not dec.is_entry else "SIGNAL"] += 1
            if cfg.features.rejected_dataset:
                from metalabel import features as _feat
                for c in list(dec.candidates) + ([dec] if dec.is_entry and dec not in dec.candidates else []):
                    mkey = ("m", c.style, c.direction, round(c.sl, 1), round(c.tp, 1))
                    if i - seen.get(mkey, -10 ** 9) >= 36:
                        seen[mkey] = i
                        hz = int(min(st.styles[c.style].max_hold_h * 60 / step_min, 2000))
                        cfx = counterfactual_ex(H, L, C, SP, i, c.direction, c.entry, c.sl, c.tp, hz, bs)   # V4.4.1: was 'ex',
                        if cfx is not None:                                   # which overwrote the execution config
                            meta_rows.append({"i": i, "i_end": cfx[1], "cf_r": round(float(cfx[0]), 4), "features": _feat(c),
                                              "setup": c.setup, "blocked": bool(c.blockers)})
                    if not c.blockers or c is dec:
                        continue
                    key = (c.style, c.direction, round(c.sl, 1), round(c.tp, 1))
                    if i - seen.get(key, -10 ** 9) < 36:
                        continue
                    seen[key] = i
                    hz = int(min(st.styles[c.style].max_hold_h * 60 / step_min, 2000))
                    cf = counterfactual(H, L, C, SP, i, c.direction, c.entry, c.sl, c.tp, hz, bs)
                    rejected_rows.append({"time": t1, "style": c.style, "setup": c.setup, "direction": c.direction,
                                          "score": c.score, "rr": c.rr, "blocker": c.blockers[0]["code"],
                                          "blockers": ",".join(b["code"] for b in c.blockers), "regime": c.regime,
                                          "session": c.session, "cf_r": None if cf is None else round(float(cf), 3)})
            if dec.is_entry and limit_on and not m1_on and limit is None and armed is None:
                from levels import limit_price
                lp = limit_price(dec, MID(C[i], SP[i]), dp.mgmt[st.styles[dec.style].tf]["atr"], cfg)
                if lp is not None:
                    limit = (dec, dp, lp, i + int(cfg.execution.limit_expiry_min / step_min))
                    lim_stats["placed"] += 1
                    continue
            if dec.is_entry:
                if m1_on:
                    if armed is None:
                        armed = (dec, dp, i + cfg.m1.window_min)
                        m1_stats["armed"] += 1
                else:
                    pending = (dec, dp)

        for p in list(positions):
            end_px = BID(C[i1], SP[i1]) if p["direction"] > 0 else ASK(C[i1], SP[i1])
            close_pos(p, end_px, i1, "end of window")
        tr = pd.DataFrame(trades)
        eq = pd.Series(eq_v, index=pd.DatetimeIndex(eq_t)) if eq_v else pd.Series(dtype=float)
        summary = summarize(tr, eq, bt.initial_balance) if len(tr) else {"total_trades": 0}
        summary["signals"] = waits.pop("SIGNAL", 0)
        summary["entries_skipped"] = waits.pop("ENTRY_SKIPPED", 0)
        summary["no_trade_reasons"] = dict(waits.most_common())
        summary["regimes_seen"] = dict(Counter(dp.regime.label for dp in signals if dp.regime and i0 <= dp.i <= i1))
        if m1_on:
            summary["m1_refinement"] = dict(m1_stats)
        if limit_on:
            summary["limit_entries"] = dict(lim_stats)
        rj = pd.DataFrame(rejected_rows)
        self.last_rejected = rj
        self.candidate_rows = meta_rows
        if len(rj):
            g = rj.dropna(subset=["cf_r"]).groupby("blocker")["cf_r"]
            summary["rejected_opportunities"] = {k: {"n": int(len(v)), "avg_cf_r": round(float(v.mean()), 3),
                                                     "cf_win_rate": round(float((v > 0).mean()), 3)} for k, v in g}
        return tr, eq, summary

    def run(self, sim, step_min=5):
        prep = self.prepare(sim, step_min)
        return self.simulate(prep, self.signals(prep))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv")
    ap.add_argument("--synthetic", type=int, default=0)
    ap.add_argument("--timeframe", choices=["M1", "M5"], default="M5")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--offset", type=float, default=0.0, help="broker server UTC offset in hours")
    ap.add_argument("--out", default="results")
    ap.add_argument("--balance", type=float, default=10000.0)
    ap.add_argument("--spread", type=float, default=0.25)
    ap.add_argument("--slippage", type=float, default=0.05)
    ap.add_argument("--commission", type=float, default=3.5)
    ap.add_argument("--swap", type=float, default=0.0)
    ap.add_argument("--decision-step", type=int, default=1)
    ap.add_argument("--trail-mode", choices=["structure", "atr", "ema", "hybrid"])
    ap.add_argument("--events", help="CSV: time (UTC), title[, forecast, previous, actual]")
    ap.add_argument("--save-signals", help="pickle the stage-1 signals for reuse by optimize.py")
    ap.add_argument("--workers", type=int, default=0, help="parallel signal generation (processes)")
    a = ap.parse_args()
    cfg = Config.from_env()
    if a.trail_mode:
        cfg = replace(cfg, strategy=replace(cfg.strategy, styles={k: replace(v, trail_mode=a.trail_mode) for k, v in cfg.strategy.styles.items()}))
    errs = [e for e in cfg.validate() if "CONFIRM_LIVE" not in e]
    if errs:
        raise SystemExit("config errors: " + "; ".join(errs))
    sim, step = (synthetic_m5(a.synthetic), 5) if a.synthetic else (load_csv(a.csv, a.offset), 1 if a.timeframe == "M1" else 5) if a.csv else (None, None)
    if sim is None:
        raise SystemExit("give --csv or --synthetic")
    sim = sim[(sim.index >= a.start) if a.start else slice(None)] if a.start else sim
    sim = sim[sim.index < a.end] if a.end else sim
    bt = BTConfig(initial_balance=a.balance, spread_default=a.spread, slippage=a.slippage,
                  commission_per_lot_side=a.commission, swap_per_lot_night=a.swap, decision_step=a.decision_step)
    btr = Backtester(cfg, bt, load_events(a.events) if a.events else [])
    prep = btr.prepare(sim, step)
    sig = btr.signals(prep, progress=True, workers=a.workers)
    if a.save_signals:
        with open(a.save_signals, "wb") as f:
            pickle.dump({"signals": sig, "step_min": step}, f)
    tr, eq, summary = btr.simulate(prep, sig)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    tr.to_csv(out / "trades.csv", index=False)
    eq.to_csv(out / "equity.csv", header=["equity"])
    mm, exr = mfe_mae_report(tr), execution_report(tr, "session")
    from analytics import blocker_report, session_report
    if btr.last_rejected is not None and len(btr.last_rejected):
        btr.last_rejected.to_csv(out / "rejected_opportunities.csv", index=False)
    summary["sessions"] = session_report(prep, tr, SP=btr.spreads(prep))
    summary.update(mfe_mae=mm, execution=exr)
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    report = (format_report(summary, tr) + format_analytics(mm, exr)) if len(tr) else "No trades were taken.\n" + json.dumps(summary, indent=2, default=str)
    report += blocker_report(summary.get("rejected_opportunities"), tr)
    from analytics import format_funnel, gate_funnel
    report += format_funnel(gate_funnel(btr.last_rejected))
    report += "\n\nSESSIONS\n" + "-" * 60 + "\n" + pd.DataFrame(summary["sessions"]).to_string(index=False)
    (out / "report.txt").write_text(report)
    print(report)


if __name__ == "__main__":
    main()
