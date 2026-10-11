import numpy as np
import pandas as pd

SCORE_BINS = [0, 65, 70, 75, 80, 85, 101]
SCORE_LABELS = ["<65", "65-70", "70-75", "75-80", "80-85", "85+"]


def max_drawdown(equity):
    """Largest peak-to-trough fall of an equity series: (money, fraction of the peak)."""
    if equity is None or len(equity) == 0:
        return 0.0, 0.0
    peak = equity.cummax()
    dd = peak - equity
    return float(dd.max()), float((dd / peak).max())


def longest_streak(flags):
    best = cur = 0
    for f in flags:
        cur = cur + 1 if f else 0
        best = max(best, cur)
    return best


def _ratio(num, den):
    return float(num / den) if den else float("inf") if num > 0 else 0.0


def summarize(trades, equity, initial_balance):
    n = len(trades)
    if n == 0:
        return {"total_trades": 0}
    net = trades["net"]
    wins, losses = net[net > 0], net[net < 0]
    dd_money, dd_frac = max_drawdown(equity)
    sharpe = sortino = 0.0
    if equity is not None and len(equity) > 2:
        daily = equity.resample("1D").last().dropna().pct_change().dropna()
        if len(daily) > 2 and daily.std() > 0:
            sharpe = float(daily.mean() / daily.std() * np.sqrt(252))
            down = daily[daily < 0]
            if len(down) > 1 and down.std() > 0:
                sortino = float(daily.mean() / down.std() * np.sqrt(252))
    r = trades["r"].dropna()
    return {
        "total_trades": n, "winning_trades": int((net > 0).sum()), "losing_trades": int((net < 0).sum()),
        "win_rate": float((net > 0).mean()), "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "profit_factor": _ratio(wins.sum(), -losses.sum()), "expectancy": float(net.mean()),
        "net_profit": float(net.sum()), "return_pct": float(net.sum() / initial_balance * 100),
        "max_drawdown": dd_money, "max_drawdown_pct": dd_frac * 100,
        "recovery_factor": _ratio(net.sum(), dd_money), "sharpe": sharpe, "sortino": sortino,
        "largest_losing_streak": longest_streak(list(net < 0)),
        "avg_r": float(r.mean()) if len(r) else 0.0, "median_r": float(r.median()) if len(r) else 0.0,
        "avg_hold_h": float(trades["hold_h"].mean()) if "hold_h" in trades else 0.0,
        "payoff_ratio": _ratio(float(wins.mean()) if len(wins) else 0.0, -float(losses.mean()) if len(losses) else 0.0),
        **tail_stats(r, trades),
    }


def tail_stats(r, trades=None):
    if len(r) < 2:
        return {}
    srt = np.sort(r.values)
    k = max(1, int(len(srt) * 0.05))
    out = {"worst_r": float(srt[0]), "best_r": float(srt[-1]), "cvar5_r": float(srt[:k].mean()),
           "skew_r": float(r.skew()) if len(r) > 2 else 0.0, "kurtosis_r": float(r.kurt()) if len(r) > 3 else 0.0,
           "autocorr_r": float(r.autocorr()) if len(r) > 3 and r.std() > 0 else 0.0}
    if trades is not None and "score" in trades and len(r) > 5:
        out["score_r_spearman"] = float(trades.loc[r.index, "score"].rank().corr(r.rank()))
    return {k: (0.0 if v != v else round(v, 4)) for k, v in out.items()}


def breakdown(trades, by, min_n=30):
    """Performance grouped by a column; groups with fewer than min_n trades are flagged as noise."""
    if len(trades) == 0 or by not in trades:
        return pd.DataFrame()
    key = trades[by]
    if by == "score":
        key = pd.cut(trades["score"], bins=SCORE_BINS, labels=SCORE_LABELS, right=False)
    rows = []
    for k, g in trades.groupby(key, observed=True):
        net = g["net"]
        rows.append({by: k, "n": len(g), "win_rate": round((net > 0).mean() * 100, 1),
                     "avg_r": round(g["r"].mean(), 3), "profit_factor": round(_ratio(net[net > 0].sum(), -net[net < 0].sum()), 2),
                     "net": round(net.sum(), 2), "reliable": "yes" if len(g) >= min_n else "low sample"})
    return pd.DataFrame(rows)


BREAKDOWNS = ("setup", "regime", "style", "session", "score", "risk_tier", "news_condition", "vol_regime", "direction")


def format_report(summary, trades):
    out = ["BACKTEST SUMMARY", "=" * 60]
    for k, v in summary.items():
        if isinstance(v, dict):
            continue
        out.append(f"{k:<24}{v:>14.3f}" if isinstance(v, float) else f"{k:<24}{v:>14}")
    for by in BREAKDOWNS:
        b = breakdown(trades, by)
        if len(b):
            out += ["", f"BY {by.upper()}", "-" * 60, b.to_string(index=False)]
    for k in ("no_trade_reasons", "regimes_seen"):
        if summary.get(k):
            out += ["", k.upper().replace("_", " "), "-" * 60] + [f"{a:<34}{b:>8}" for a, b in summary[k].items()]
    out += ["", "Score buckets only mean something once each bucket has a reliable sample (30+ trades).",
            "A backtest is a necessary check, not proof of future profit."]
    return "\n".join(out)
