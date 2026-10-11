import numpy as np
import pandas as pd

from config import session_name

DEFAULT_BOUNDS = {"ASIA": (0, 7), "LONDON": (7, 12), "OVERLAP": (12, 16), "NY": (16, 21)}


class DataError(Exception):
    pass


def validate_ohlc(df, min_bars=60):
    """Clean and sanity-check candles. Returns (clean_df, issues). Raises DataError if unusable."""
    issues = []
    d = df.copy()
    if not d.index.is_monotonic_increasing:
        d = d.sort_index()
        issues.append("out_of_order")
    dup = d.index.duplicated(keep="last")
    if dup.any():
        d = d[~dup]
        issues.append(f"duplicates:{int(dup.sum())}")
    cols = ["open", "high", "low", "close"]
    d[cols] = d[cols].astype(float)
    bad = d[cols].isna().any(axis=1) | (d[cols] <= 0).any(axis=1)
    if bad.any():
        d = d[~bad]
        issues.append(f"bad_prices:{int(bad.sum())}")
    hi = d[["open", "high", "close"]].max(axis=1)
    lo = d[["open", "low", "close"]].min(axis=1)
    incons = (d["high"] < hi - 1e-9) | (d["low"] > lo + 1e-9)
    if incons.any():
        d["high"], d["low"] = hi, lo
        issues.append(f"inconsistent_ohlc:{int(incons.sum())}")
    if "tick_volume" not in d:
        d["tick_volume"] = 1.0
    if len(d) < min_bars:
        raise DataError(f"only {len(d)} usable bars (need {min_bars})")
    step = d.index.to_series().diff().median()
    if pd.notna(step) and step > pd.Timedelta(0):
        gaps = d.index.to_series().diff()
        n_gaps = int(((gaps > 5 * step) & (gaps < pd.Timedelta(days=2))).sum())
        if n_gaps:
            issues.append(f"gaps:{n_gaps}")
    return d, issues


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def atr(df, n=14):
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def adx(df, n=14, atr_s=None):
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    a = atr_s if atr_s is not None else atr(df, n)
    pdi = 100 * plus.ewm(alpha=1 / n, adjust=False).mean() / a
    mdi = 100 * minus.ewm(alpha=1 / n, adjust=False).mean() / a
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean().fillna(0)


def macd_hist(close, fast=12, slow=26, sig=9):
    line = ema(close, fast) - ema(close, slow)
    return line - ema(line, sig)


def bollinger_pct(close, n=20, k=2.0):
    mid = close.rolling(n).mean()
    sd = close.rolling(n).std()
    return ((close - (mid - k * sd)) / (2 * k * sd).replace(0, np.nan)).fillna(0.5)


def session_vwaps(df, bounds=None):
    """Daily VWAP and session-anchored VWAP (Asia / London / overlap / NY), from tick volume."""
    bounds = bounds or DEFAULT_BOUNDS
    tp = (df["high"] + df["low"] + df["close"]) / 3
    v = df["tick_volume"].replace(0, 1)
    day = df.index.normalize()
    daily = (tp * v).groupby(day).cumsum() / v.groupby(day).cumsum()
    lab = np.array([session_name(h, bounds) for h in df.index.hour])
    sess = (tp * v).groupby([day, lab]).cumsum() / v.groupby([day, lab]).cumsum()
    return daily, sess


def classic_pivots(prev_high, prev_low, prev_close):
    p = (prev_high + prev_low + prev_close) / 3
    return {"P": p, "R1": 2 * p - prev_low, "S1": 2 * p - prev_high,
            "R2": p + (prev_high - prev_low), "S2": p - (prev_high - prev_low)}


def adx_band(x, bands=(15.0, 20.0, 25.0, 35.0)):
    if x < bands[0]:
        return "DEAD"
    if x < bands[1]:
        return "WEAK"
    if x < bands[2]:
        return "DEVELOPING"
    if x < bands[3]:
        return "STRONG"
    return "VERY_STRONG"


def atr_regime(atr_s, window=200, avg=50, long_window=1000):
    """Volatility regime from ATR percentile and ATR relative to its recent average.
    V5.1: EXTREME (which stands the agent down) needs a genuine shock - ATR at least double its recent mean, or a
    new long-history extreme that is also well above the recent mean. A sustained rise ("busier than last week")
    is HIGH, which trades smaller instead of stopping. The V3-V5 rule (top 5% of the last 200 bars) kept a moderate
    60% volatility step classified EXTREME for ~11 hours, standing the agent down for whole busy sessions."""
    cur = float(atr_s.iloc[-1])
    pct = float((atr_s.iloc[-window:] <= cur).mean())
    pct_long = float((atr_s.iloc[-long_window:] <= cur).mean())
    ratio = cur / max(float(atr_s.iloc[-avg:].mean()), 1e-12)
    if ratio >= 2.0 or (pct_long >= 0.995 and ratio >= 1.6):
        reg = "EXTREME"
    elif pct >= 0.80 or ratio >= 1.4:
        reg = "HIGH"
    elif pct <= 0.20 or ratio <= 0.7:
        reg = "LOW"
    else:
        reg = "NORMAL"
    return {"atr": cur, "atr_pct": pct, "atr_pct_long": pct_long, "atr_ratio": ratio, "regime": reg}


def slope(series, n=5, norm=1.0):
    """Change per bar over the last n bars, expressed in units of `norm` (usually ATR)."""
    if len(series) <= n:
        return 0.0
    return float((series.iloc[-1] - series.iloc[-1 - n]) / n / max(norm, 1e-12))


def add_indicators(df, bounds=None):
    d = df.copy()
    lo14, hi14 = d["low"].rolling(14).min(), d["high"].rolling(14).max()
    d["stoch_k"] = (100 * (d["close"] - lo14) / (hi14 - lo14).replace(0, np.nan)).rolling(3).mean()
    d["stoch_d"] = d["stoch_k"].rolling(3).mean()
    d["ema20"] = ema(d["close"], 20)
    d["ema50"] = ema(d["close"], 50)
    d["ema200"] = ema(d["close"], 200)
    d["rsi"] = rsi(d["close"])
    d["atr"] = atr(d)
    d["adx"] = adx(d, 14, d["atr"])
    d["macd_hist"] = macd_hist(d["close"])
    d["bb_pct"] = bollinger_pct(d["close"])
    d["vwap_day"], d["vwap_sess"] = session_vwaps(d, bounds)
    return d
