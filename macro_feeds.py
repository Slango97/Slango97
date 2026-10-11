"""Real gold drivers as contradiction evidence (never as BUY/SELL votes on their own).

Live: broker symbols if offered (dollar index, US 10-year yield, EURUSD as an inverse-USD proxy), plus CSV feeds you
maintain for data brokers rarely offer (e.g. GLD holdings, GVZ volatility, real yields): data/aux/<name>.csv with
columns time,value. Each feed becomes a bias in [-1, 1] already signed for gold (+ = supportive of gold).
Research: backtest/evidence --aux name=path.csv:sign[,name2=path2.csv:sign]
"""
import logging
import time
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger("macro")


def bias_from_series(values, lookback=24, norm=240):
    """Recent change relative to its own typical size, squashed to [-1, 1]. Uses only the values given (causal)."""
    v = np.asarray(values, float)
    v = v[~np.isnan(v)]
    if len(v) <= lookback + 5:
        return 0.0
    ch = v[lookback:] - v[:-lookback]
    sd = np.std(ch[-norm:]) or 1e-12
    return float(np.tanh((v[-1] - v[-1 - lookback]) / sd / 1.5))


def _read(path, offset_h=0.0):
    df = pd.read_csv(path)
    df.columns = [c.lower().strip() for c in df.columns]
    col = "value" if "value" in df else "close"
    s = pd.Series(df[col].astype(float).values, index=pd.to_datetime(df["time"]) - pd.Timedelta(hours=offset_h)).sort_index()
    return s[~s.index.duplicated(keep="last")]


def load_aux_specs(spec, offset_h=0.0):
    """'usd_index=history/USDX_H1.csv:-1,us10y=history/US10Y_H1.csv:-1' -> {name: (series, sign)}"""
    out = {}
    for part in [p.strip() for p in spec.split(",") if p.strip()]:
        name, rest = part.split("=", 1)
        path, _, sign = rest.rpartition(":")
        if not path:
            path, sign = rest, "1"
        out[name.strip()] = (_read(path, offset_h), float(sign))
    return out


class MacroFeeds:
    def __init__(self, cfg, client):
        self.mc, self.c = cfg.macro, client
        self.values, self.status, self._at, self._resolved = {}, {}, 0.0, {}

    def _symbol(self, candidates):
        key = tuple(candidates)
        if key not in self._resolved:
            self._resolved[key] = next((s for s in candidates if self.c.symbol_available(s)), None)
        return self._resolved[key]

    def refresh(self, force=False):
        if not force and time.time() - self._at < 60 * self.mc.refresh_min:
            return self.values
        self._at = time.time()
        vals, st = {}, {}
        prof = getattr(self, "profile", None)
        for name, candidates, sign in self.mc.feeds:
            if prof is not None:                      # V5.1: DXY / US10Y read for the traded instrument, not only gold
                sign = {"usd_index": prof.usd_beta, "us10y": prof.yields_beta, "eurusd_usd_proxy": -prof.usd_beta}.get(name, sign)
            sym = self._symbol(candidates)
            if sym is None:
                st[name] = "not offered by broker"
                continue
            try:
                df = self.c.aux_rates(sym, "H1", 300)
                age_h = (pd.Timestamp.now(tz="UTC").tz_localize(None) - df.index[-1]).total_seconds() / 3600
                if age_h > self.mc.max_age_h:
                    st[name] = f"stale ({age_h:.0f}h)"
                    continue
                vals[name] = round(sign * bias_from_series(df["close"].values, self.mc.lookback_bars), 3)
                st[name] = f"ok via {sym}"
            except Exception as ex:
                st[name] = f"error: {ex}"
        for name, path, sign, max_age_h in self.mc.csv_feeds:
            p = Path(path)
            if not p.exists():
                continue
            try:
                s = _read(p)
                age_h = (pd.Timestamp.now(tz="UTC").tz_localize(None) - s.index[-1]).total_seconds() / 3600
                if age_h > max_age_h:
                    st[name] = f"stale ({age_h:.0f}h)"
                    continue
                vals[name] = round(sign * bias_from_series(s.values, min(self.mc.lookback_bars, max(2, len(s) // 4))), 3)
                st[name] = "ok via csv"
            except Exception as ex:
                st[name] = f"error: {ex}"
        self.values, self.status = vals, st
        return vals

    def get(self):
        return dict(self.values)
