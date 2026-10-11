from helpers import candles, zigzag  # noqa: F401  (also sets sys.path)

import numpy as np
import pandas as pd

from indicators import (DataError, adx_band, atr, atr_regime, session_vwaps, validate_ohlc)
from smc import (equal_levels, fair_value_gaps, find_sweep, build_pools, key_levels, order_blocks, structure,
                 trendline)


def test_validate_fixes_duplicates_bad_prices_and_order():
    df = zigzag([2000, 2010, 2000, 2010, 2000, 2010, 2000], per_leg=15)
    messy = pd.concat([df.iloc[::-1], df.iloc[[5]]])
    messy.iloc[3, messy.columns.get_loc("close")] = 0.0
    clean, issues = validate_ohlc(messy)
    assert clean.index.is_monotonic_increasing and not clean.index.duplicated().any()
    assert (clean[["open", "high", "low", "close"]] > 0).all().all()
    assert any(i.startswith("duplicates") for i in issues) and any(i.startswith("bad_prices") for i in issues)


def test_validate_raises_when_too_few_bars():
    try:
        validate_ohlc(zigzag([2000, 2005, 2000], per_leg=5))
    except DataError:
        return
    raise AssertionError("expected DataError")


def test_adx_band_thresholds():
    assert [adx_band(x) for x in (10, 17, 22, 30, 40)] == ["DEAD", "WEAK", "DEVELOPING", "STRONG", "VERY_STRONG"]


def test_atr_regime_detects_expansion():
    calm = zigzag([2000, 2002] * 30, per_leg=6, wick=0.05)
    assert atr_regime(atr(calm))["regime"] in ("NORMAL", "LOW")
    wild = pd.concat([calm, zigzag([2000, 2040, 1990, 2050], per_leg=3, wick=3, start="2025-03-05")])
    assert atr_regime(atr(wild))["regime"] in ("HIGH", "EXTREME")


def test_session_vwap_resets_each_session():
    df = zigzag([2000, 2010, 2005, 2015] * 12, per_leg=6)
    daily, sess = session_vwaps(df)
    assert not daily.isna().any() and not sess.isna().any()
    hrs = df.index.hour
    first_london = np.where(hrs == 7)[0][0]
    tp = (df["high"] + df["low"] + df["close"]) / 3
    assert abs(sess.iloc[first_london] - tp.iloc[first_london]) < 1e-9      # session VWAP restarts at 07:00


def test_structure_labels_bullish_sequence_and_bos_quality():
    df = zigzag([2000, 2010, 2005, 2015, 2009, 2020, 2013, 2026, 2019, 2031], per_leg=8)
    st = structure(df, atr(df))
    labs = [x[3] for x in st["labels"]]
    assert "HH" in labs and "HL" in labs
    assert st["regime"] == "BULLISH" and st["trend"] == 1
    assert st["last_event"]["dir"] == 1 and 0 <= st["last_event"]["quality"] <= 100
    assert all(e["type"] in ("BOS", "CHoCH") for e in st["events"])


def test_choch_when_trend_breaks_down():
    df = zigzag([2000, 2010, 2005, 2015, 2009, 2020, 2013, 2008, 2004, 1998], per_leg=8)
    st = structure(df, atr(df))
    kinds = [(e["type"], e["dir"]) for e in st["events"]]
    assert ("CHoCH", -1) in kinds


def test_wick_poke_is_not_a_structure_break():
    base = zigzag([2000, 2010, 2004, 2010, 2004], per_leg=8)
    rows = base[["open", "high", "low", "close"]].values.tolist()
    top = max(r[1] for r in rows)
    rows.append([2005, top + 1.5, 2004.5, 2008])        # wick above the high, closes back inside
    df = candles(rows)
    st = structure(df, atr(df))
    assert st["last_event"] is None or st["last_event"]["idx"] != len(df) - 1


def test_fvg_metrics_and_full_fill_removal():
    rows = [(100, 100.5, 99.5, 100)] * 20
    rows += [(100, 100.4, 99.6, 100.2), (100.2, 104.0, 100.0, 103.8), (103.8, 105.0, 102.0, 104.5)]  # gap 100.4..102.0
    rows += [(104.5, 105, 103.5, 104)] * 3
    df = candles(rows)
    gaps = [g for g in fair_value_gaps(df, atr(df)) if g["type"] == "bull"]
    assert gaps and gaps[-1]["bottom"] == 100.4 and gaps[-1]["top"] == 102.0
    assert gaps[-1]["fill_pct"] == 0 and gaps[-1]["quality"] > 0 and gaps[-1]["status"] == "unmitigated"
    filled = candles(rows + [(104, 104.2, 99.0, 99.5)])        # price drops through the gap
    assert not [g for g in fair_value_gaps(filled, atr(filled)) if g["type"] == "bull" and g["bottom"] == 100.4]


def test_order_block_found_confirmed_and_invalidated():
    rows = [(100 + (i % 2) * 0.2, 100.8, 99.4, 100.4 - (i % 2) * 0.2) for i in range(30)]
    rows += [(100.3, 100.5, 99.0, 99.2)]                         # last down candle = the order block
    rows += [(99.2, 104.5, 99.1, 104.2)]                         # displacement up through the range
    rows += [(104.2, 105, 103.8, 104.6)] * 4
    df = candles(rows)
    a = atr(df)
    st = structure(df, a)
    obs = [z for z in order_blocks(df, a, st["all_events"]) if z["type"] == "bull"]
    assert obs and obs[-1]["bottom"] == 99.0 and 0 <= obs[-1]["quality"] <= 100 and obs[-1]["tests"] == 0
    broken = candles(rows + [(104, 104, 97.5, 98.0)])             # close below the block invalidates it
    st2 = structure(broken, atr(broken))
    assert not [z for z in order_blocks(broken, atr(broken), st2["all_events"]) if z["bottom"] == 99.0]


def test_trendline_needs_three_touches():
    two = [(10, 100.0), (30, 102.0)]
    assert trendline(two, 100, 2.0, "up") is None
    three = [(10, 100.0), (30, 102.0), (50, 104.0)]
    tl = trendline(three, 100, 2.0, "up")
    assert tl and tl["touches"] == 3 and abs(tl["value"] - 108.9) < 0.2
    assert trendline(three, 100, 2.0, "down") is None


def test_equal_highs_detected_and_swept():
    sh = [(10, 2010.00), (30, 2010.15), (50, 2004.0)]
    eq = equal_levels(sh, [], atr_val=3.0)
    assert eq and eq[0]["kind"] == "EQH" and eq[0]["touches"] == 2
    rows = [(2000, 2002, 1999, 2001)] * 60
    rows[40] = (2001, 2010.1, 2000, 2002)
    rows[41:57] = [(2002, 2004, 2000, 2003)] * 16
    rows.append((2003, 2011.5, 2002, 2005))                       # wicks above the equal highs, closes back
    df = candles(rows)
    pools = build_pools([(40, 2010.1)], [], [{"kind": "EQH", "level": 2010.1, "touches": 2, "last_idx": 40}], {})
    sw = find_sweep(df, pools)
    assert sw and sw["dir"] == -1 and sw["kind"] in ("EQH", "SWING")


def test_key_levels_previous_day_and_session_ranges():
    df = zigzag([2000, 2020, 1995, 2010] * 20, per_leg=10, start="2025-03-03 00:00")
    keys = key_levels(df, {"ASIA": (0, 7), "LONDON": (7, 12), "OVERLAP": (12, 16), "NY": (16, 21)})
    assert "PDH" in keys and "PDL" in keys and keys["PDH"] > keys["PDL"]
