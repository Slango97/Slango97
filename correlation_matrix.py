"""Live cross-market correlation matrix (V5.8, roadmap step 3).

The V3-V5 portfolio model used hand-written factor loadings for 7 symbols; every other pair (US30/NAS100,
USOIL, BTCUSD, USDCAD ...) and every broker-suffixed name (XAUUSDs, GOLD) fell back to a guessed 0.5.

This module measures correlations from the broker's own closed H1 bars for the whole watchlist:

* log returns aligned on common timestamps (no look-ahead, no forward-filled gaps);
* two windows - long (default 720 H1 bars ~ 6 weeks) and short (120 bars ~ 1 week) - because correlations jump
  in stress and a slow estimate alone reacts too late;
* shrinkage toward the factor-model prior, rho = w * sample + (1 - w) * prior with w = n / (n + k), so a thin or
  noisy overlap cannot produce an extreme value;
* stored per pair as a RANGE (low, high). `portfolio.py` takes, for every pair of positions, the end of the range
  that makes the combined risk LARGER, and gives offsetting (hedged) positions only `corr_hedge_credit` of their
  theoretical risk reduction - hedges are what break first in a crisis.

The matrix is refreshed on the heartbeat, persisted atomically (shared by multi-market instances), and ignored
when older than `corr_max_age_h` (falls back to the factor prior). It can only change how risk is MEASURED;
every limit, stop and gate stays as configured.
"""
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from market_specs import canonical


def market_key(symbol):
    return canonical(symbol) or str(symbol or "").upper()


def _shrunk(sample, prior, n, k):
    if sample is None or not math.isfinite(sample):
        return prior
    w = n / (n + k) if n > 0 else 0.0
    return max(-1.0, min(1.0, w * sample + (1.0 - w) * prior))


def compute(closes, pc, prior_fn=None):
    """closes: {market: pd.Series of closes indexed by time}. Returns the serialisable matrix dict."""
    rets = {}
    for mk, s in closes.items():
        s = pd.Series(s).astype(float)
        s = s[(s > 0) & np.isfinite(s)]
        s = s[~s.index.duplicated(keep="last")].sort_index()
        if len(s) > 2:
            rets[mk] = np.log(s).diff().dropna()
    markets = sorted(rets)
    pairs = {}
    for i, a in enumerate(markets):
        for b in markets[i + 1:]:
            j = pd.concat([rets[a], rets[b]], axis=1, join="inner").dropna()
            j = j[(j.iloc[:, 0] != 0) | (j.iloc[:, 1] != 0)]               # drop closed-market flat bars
            prior = prior_fn(a, b) if prior_fn else 0.0
            row = {"prior": round(prior, 4)}
            for name, bars in (("long", pc.corr_lookback_bars), ("short", pc.corr_short_bars)):
                w = j.iloc[-bars:]
                n = len(w)
                sample = float(w.iloc[:, 0].corr(w.iloc[:, 1])) if n >= pc.corr_min_overlap else None
                row[name] = None if sample is None or not math.isfinite(sample) else round(sample, 4)
                row[f"n_{name}"] = n
                row[f"{name}_shrunk"] = round(_shrunk(row[name], prior, n if sample is not None else 0, pc.corr_shrink_k), 4)
            vals = [row["long_shrunk"], row["short_shrunk"]]
            row["low"], row["high"] = round(min(vals), 4), round(max(vals), 4)
            pairs[f"{a}|{b}"] = row
    return {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "timeframe": pc.corr_timeframe,
            "markets": markets, "pairs": pairs}


class CorrelationMatrix:
    def __init__(self, data):
        self.data = data or {}
        self.pairs = self.data.get("pairs", {})

    def age_h(self, now=None):
        try:
            gen = datetime.fromisoformat(str(self.data["generated_at"]).replace("Z", "+00:00"))
            return ((now or time.time()) - gen.timestamp()) / 3600
        except Exception:
            return math.inf

    def range(self, a, b):
        """(low, high) correlation for two symbols, or None when the pair was not measured."""
        ka, kb = market_key(a), market_key(b)
        if ka == kb:
            return 1.0, 1.0
        row = self.pairs.get(f"{ka}|{kb}") or self.pairs.get(f"{kb}|{ka}")
        if not row:
            return None
        return float(row["low"]), float(row["high"])

    def strong_pairs(self, threshold=0.7):
        out = []
        for k, row in self.pairs.items():
            v = row["high"] if abs(row["high"]) >= abs(row["low"]) else row["low"]
            if abs(v) >= threshold:
                out.append((k.replace("|", "/"), v))
        return sorted(out, key=lambda x: -abs(x[1]))

    def save(self, path):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        os.replace(tmp, p)

    @classmethod
    def load(cls, path):
        try:
            return cls(json.loads(Path(path).read_text()))
        except Exception:
            return None


def fetch_closes(client, markets, timeframe, bars, resolve=None):
    """Closed bars for every watchlist market the broker offers ({market: close series}); missing ones are skipped."""
    out = {}
    for mk in markets:
        sym = resolve(mk) if resolve else mk
        if not sym:
            continue
        try:
            df, _ = client.rates_for(sym, timeframe, bars)
            if df is not None and len(df):
                out[market_key(mk)] = df["close"]
        except Exception:
            continue
    return out


def main():
    """Offline: matrix from exported history (export_history.py --markets), e.g.
    python correlation_matrix.py --history history --out data/correlation_matrix.json"""
    import argparse

    from backtest import load_csv
    from config import Config
    from market_research import discover
    from portfolio import static_correlation
    ap = argparse.ArgumentParser(description=main.__doc__)
    ap.add_argument("--history", default="history")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    cfg = Config.from_env()
    pc = cfg.portfolio
    closes = {}
    for mk, (csv, meta) in discover(a.history).items():
        df = load_csv(csv, float(meta.get("server_utc_offset_h_now") or 0.0))
        closes[mk] = df["close"].resample("1h").last().dropna()
    data = compute(closes, pc, lambda x, y: static_correlation(x, y, pc))
    m = CorrelationMatrix(data)
    m.save(a.out or pc.corr_file)
    for pair, v in m.strong_pairs(0.5):
        print(f"{pair:>16}: {v:+.2f}")
    print(f"saved {a.out or pc.corr_file} ({len(data['pairs'])} pairs)")


if __name__ == "__main__":
    main()
