"""Lightweight chart rendering for Telegram reports.  No mplfinance dependency."""
from __future__ import annotations

from pathlib import Path


def render_chart(df, *, path, title="Expert AI Agent Pro Trader", entry=None, sl=None, tp=None,
                 levels=None, patterns=None):
    if df is None or len(df) < 10:
        return None
    try:
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle
    except Exception:
        return None
    d = df.tail(90).copy()
    fig, ax = plt.subplots(figsize=(12, 7))
    for i, (_, row) in enumerate(d.iterrows()):
        o, h, l, c = map(float, (row.open, row.high, row.low, row.close))
        up = c >= o
        ax.vlines(i, l, h, linewidth=1)
        body_lo = min(o, c)
        body_h = max(abs(c - o), 1e-8)
        ax.add_patch(Rectangle((i - 0.32, body_lo), 0.64, body_h, fill=False, linewidth=1.0))
    if entry is not None:
        ax.axhline(float(entry), linestyle="--", linewidth=1.2, label=f"Entry {entry:.2f}")
    if sl is not None:
        ax.axhline(float(sl), linestyle=":", linewidth=1.2, label=f"SL {sl:.2f}")
    if tp is not None:
        ax.axhline(float(tp), linestyle=":", linewidth=1.2, label=f"TP {tp:.2f}")
    for lev in levels or []:
        try:
            ax.axhline(float(lev), linestyle="-", linewidth=0.7, alpha=0.5)
        except Exception:
            pass
    subtitle = f" | Patterns: {', '.join(patterns[:4])}" if patterns else ""
    ax.set_title(title + subtitle)
    ax.set_xlim(-1, len(d))
    ax.grid(alpha=0.15)
    ax.legend(loc="upper left", fontsize=8)
    fig.tight_layout()
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(p, dpi=130)
    plt.close(fig)
    return str(p)
