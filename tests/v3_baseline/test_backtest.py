from helpers import Config  # noqa: F401

import numpy as np
import pandas as pd

from backtest import BTConfig, Backtester, build_frames, swap_nights, synthetic_m5, TFS
from metrics import breakdown, longest_streak, max_drawdown, summarize


def test_higher_timeframes_never_use_future_data():
    m5 = synthetic_m5(9000, seed=5)
    cut = 6000
    tampered = m5.copy()
    rng = np.random.default_rng(1)
    tampered.iloc[cut:, :4] = rng.uniform(100, 5000, (len(m5) - cut, 4))
    f_a, f_b = build_frames(m5), build_frames(tampered)
    t = m5.index[cut - 1] + pd.Timedelta(minutes=5)                    # end of the last untouched bar
    for tf in TFS:
        (da, ea), (db, eb) = f_a[tf], f_b[tf]
        na, nb = int(np.searchsorted(ea, np.datetime64(t), "right")), int(np.searchsorted(eb, np.datetime64(t), "right"))
        assert na == nb
        pd.testing.assert_frame_equal(da.iloc[:na], db.iloc[:nb])


def test_swap_nights_counts_22utc_rollovers():
    t = pd.Timestamp
    assert swap_nights(t("2025-03-03 10:00"), t("2025-03-03 20:00")) == 0
    assert swap_nights(t("2025-03-03 10:00"), t("2025-03-03 23:00")) == 1
    assert swap_nights(t("2025-03-03 10:00"), t("2025-03-05 10:00")) == 2


def test_backtest_runs_and_conserves_money():
    cfg = Config()
    bt = BTConfig(decision_step=4, bars=300)
    sim = synthetic_m5(7000, seed=11)
    tr, eq, summ = Backtester(cfg, bt).run(sim, 5)
    assert isinstance(tr, pd.DataFrame) and summ["signals"] >= len(tr)
    if len(tr):
        assert np.isfinite(tr["r"]).all() and (tr["lots0"] >= bt.volume_min).all()
        assert (tr["close_time"] >= tr["open_time"]).all()
        sl = tr[tr["reason"] == "stop loss"]
        assert (sl["r"] < -0.3).all() and (sl["r"] > -2.5).all()       # a full stop costs about one R plus costs
        assert abs(summ["net_profit"] - tr["net"].sum()) < 1e-6
        assert (tr["open_time"].dt.dayofweek < 5).all()


def test_backtest_is_deterministic():
    cfg, bt = Config(), BTConfig(decision_step=6, bars=300)
    sim = synthetic_m5(5000, seed=2)
    a = Backtester(cfg, bt).run(sim, 5)[0]
    b = Backtester(cfg, bt).run(sim, 5)[0]
    pd.testing.assert_frame_equal(a, b)


def trades(rows):
    return pd.DataFrame(rows, columns=["net", "r", "score", "style", "hold_h"])


def test_summary_metrics_on_known_trades():
    tr = trades([(100, 2.0, 70, "day", 1), (-50, -1.0, 72, "day", 2), (100, 2.0, 88, "swing", 3), (-50, -1.0, 66, "day", 1),
                 (-50, -1.0, 67, "scalp", 1)])
    eq = pd.Series([10000, 10100, 10050, 10150, 10100, 10050], index=pd.date_range("2025-03-03", periods=6, freq="1D"))
    s = summarize(tr, eq, 10000)
    assert s["total_trades"] == 5 and abs(s["win_rate"] - 0.4) < 1e-9 and abs(s["profit_factor"] - 200 / 150) < 1e-9
    assert abs(s["expectancy"] - 10) < 1e-9 and s["largest_losing_streak"] == 2 and abs(s["avg_r"] - 0.2) < 1e-9
    assert abs(s["max_drawdown"] - 100) < 1e-9 and abs(s["max_drawdown_pct"] - 100 / 10150 * 100) < 1e-6
    assert longest_streak([True, True, False, True]) == 2
    assert max_drawdown(eq)[0] == 100


def test_breakdown_buckets_scores_and_flags_small_samples():
    tr = trades([(100, 2.0, 70, "day", 1), (-50, -1.0, 72, "day", 2), (100, 2.0, 88, "swing", 3)])
    b = breakdown(tr, "score")
    assert set(b["score"].astype(str)) == {"70-75", "85+"} and (b["reliable"] == "low sample").all()
    assert breakdown(tr, "style").set_index("style").loc["day", "n"] == 2
