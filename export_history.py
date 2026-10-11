"""Export research data from YOUR broker (run on the MT5 workstation):
    python export_history.py --years 3 --out history
    python export_history.py --years 3 --out history --aux USDX,US10Y,EURUSD

Writes history/<SYMBOL>_M1.csv (time in broker server time, OHLC, tick_volume, spread in points), one H1 file per
available auxiliary symbol (for macro_feeds.py), and export_meta.json (server UTC offset, symbol specification).
Use the meta file's offset as --offset for backtest.py / optimize.py / evidence.py."""
import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd


def export(mt5, symbol, tf, start, end, chunk_days=30):
    parts, t = [], start
    while t < end:
        t2 = min(end, t + timedelta(days=chunk_days))
        r = mt5.copy_rates_range(symbol, tf, t, t2)
        if r is not None and len(r):
            parts.append(pd.DataFrame(r))
        t = t2
    if not parts:
        return None
    df = pd.concat(parts).drop_duplicates("time").sort_values("time")
    df["time"] = pd.to_datetime(df["time"], unit="s").dt.strftime("%Y-%m-%d %H:%M:%S")
    return df


def main():
    import MetaTrader5 as mt5
    from config import Config
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--years", type=float, default=3)
    ap.add_argument("--out", default="history")
    ap.add_argument("--aux", default="", help="comma-separated auxiliary symbols (exported at H1 if available)")
    a = ap.parse_args()
    cfg = Config.from_env()
    if not mt5.initialize():
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
    from symbol_resolver import discover_gold_symbol
    cand = discover_gold_symbol(mt5, preferred="XAUUSD", aliases=cfg.system.symbol_aliases, pin=cfg.system.symbol)
    if cand is None:
        raise SystemExit("tradable gold symbol not found in MT5 inventory")
    sym = cand.name
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    end = datetime.now(timezone.utc) + timedelta(days=1)
    start = end - timedelta(days=365 * a.years)
    m1 = export(mt5, sym, mt5.TIMEFRAME_M1, start, end)
    if m1 is None:
        raise SystemExit("no M1 history returned: open the symbol chart and scroll back, or raise 'Max bars in chart'")
    m1[["time", "open", "high", "low", "close", "tick_volume", "spread"]].to_csv(out / f"{sym}_M1.csv", index=False)
    tick = mt5.symbol_info_tick(sym)
    offset = round((tick.time - time.time()) / 3600) if tick else None
    info = mt5.symbol_info(sym)
    aux = {}
    for s in [x.strip() for x in a.aux.split(",") if x.strip()]:
        if mt5.symbol_info(s) is None:
            aux[s] = "not offered by this broker"
            continue
        mt5.symbol_select(s, True)
        h1 = export(mt5, s, mt5.TIMEFRAME_H1, start, end, 120)
        if h1 is not None:
            h1[["time", "close"]].to_csv(out / f"{s}_H1.csv", index=False)
            aux[s] = f"{len(h1)} H1 bars"
    meta = {"symbol": sym, "bars_m1": len(m1), "first": m1["time"].iloc[0], "last": m1["time"].iloc[-1],
            "server_utc_offset_h_now": offset, "note": "offset may differ by 1h across DST changes",
            "contract_size": info.trade_contract_size, "digits": info.digits, "point": info.point, "aux": aux,
            "exported_at": datetime.now(timezone.utc).isoformat()}
    (out / "export_meta.json").write_text(json.dumps(meta, indent=2))
    print(json.dumps(meta, indent=2))
    mt5.shutdown()


if __name__ == "__main__":
    main()
