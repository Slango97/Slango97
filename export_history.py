"""Export research data from YOUR broker (run on the MT5 workstation):
    python export_history.py --years 3 --out history
    python export_history.py --years 3 --out history --aux USDX,US10Y,EURUSD
    python export_history.py --markets WATCHLIST --years 3 --out history     # V5.6: every watchlist market
    python export_history.py --markets EURUSD,USDJPY --tf M5 --years 5 --out history

Writes history/<SYMBOL>_M1.csv (time in broker server time, OHLC, tick_volume, spread in points), one H1 file per
available auxiliary symbol (for macro_feeds.py), and export_meta.json (server UTC offset, symbol specification).
Use the meta file's offset as --offset for backtest.py / optimize.py / evidence.py.
With --markets each market gets its own folder history/<MARKET>/ (CSV + export_meta.json with the broker's point,
digits, contract size, profit currency and median spread) for market_research.py."""
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


def resolve_market(mt5, market):
    """Broker symbol for a watchlist market (exact name first, then the known aliases, then a prefix match)."""
    from brain.universe import ALIASES
    for name in (market,) + tuple(ALIASES.get(market, ())):
        if mt5.symbol_info(name) is not None:
            return name
    for s in mt5.symbols_get() or ():
        if s.name.upper().startswith(market):
            return s.name
    return None


def export_markets(mt5, markets, years, out, tf_name="M1"):
    tf = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5}[tf_name]
    end = datetime.now(timezone.utc) + timedelta(days=1)
    start = end - timedelta(days=365 * years)
    summary = {}
    for mk in markets:
        sym = resolve_market(mt5, mk)
        if sym is None:
            summary[mk] = "not offered by this broker"
            print(f"{mk}: not offered by this broker")
            continue
        mt5.symbol_select(sym, True)
        df = export(mt5, sym, tf, start, end)
        if df is None:
            summary[mk] = "no history returned (open the chart and scroll back / raise Max bars in chart)"
            print(f"{mk} ({sym}): no history")
            continue
        d = Path(out) / mk
        d.mkdir(parents=True, exist_ok=True)
        df[["time", "open", "high", "low", "close", "tick_volume", "spread"]].to_csv(d / f"{sym}_{tf_name}.csv", index=False)
        info, tick = mt5.symbol_info(sym), mt5.symbol_info_tick(sym)
        spreads = df["spread"][df["spread"] > 0]
        meta = {"market": mk, "symbol": sym, "timeframe": tf_name, "bars": len(df), "first": df["time"].iloc[0],
                "last": df["time"].iloc[-1], "server_utc_offset_h_now": round((tick.time - time.time()) / 3600) if tick else None,
                "point": info.point, "digits": info.digits, "contract_size": info.trade_contract_size,
                "currency_profit": info.currency_profit, "volume_min": info.volume_min, "volume_step": info.volume_step,
                "median_spread_points": float(spreads.median()) if len(spreads) else None,
                "exported_at": datetime.now(timezone.utc).isoformat()}
        (d / "export_meta.json").write_text(json.dumps(meta, indent=2))
        summary[mk] = f"{sym}: {len(df)} {tf_name} bars {meta['first']} .. {meta['last']}"
        print(f"{mk}: {summary[mk]}")
    (Path(out) / "markets_export.json").write_text(json.dumps(summary, indent=2))
    return summary


def main():
    import MetaTrader5 as mt5
    from config import Config
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--years", type=float, default=3)
    ap.add_argument("--out", default="history")
    ap.add_argument("--aux", default="", help="comma-separated auxiliary symbols (exported at H1 if available)")
    ap.add_argument("--markets", default="", help="WATCHLIST or a comma list: export each market for market_research.py")
    ap.add_argument("--tf", choices=["M1", "M5"], default="M1", help="bar size for --markets (M5 = 5x smaller files)")
    a = ap.parse_args()
    cfg = Config.from_env()
    if not mt5.initialize():
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
    if a.markets:
        mk = list(cfg.brain.watchlist) if a.markets.strip().upper() == "WATCHLIST" else \
            [x.strip().upper() for x in a.markets.split(",") if x.strip()]
        export_markets(mt5, mk, a.years, a.out, a.tf)
        mt5.shutdown()
        return
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
