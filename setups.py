"""What kind of trade is this? Explicit, testable definitions, so performance can be measured per setup and
setups without an out-of-sample edge can be switched off."""
import json
import logging
from pathlib import Path

log = logging.getLogger("setups")

SETUPS = ("LIQUIDITY_SWEEP_REVERSAL", "BREAKOUT_RETEST", "BOS_PULLBACK", "ORDER_BLOCK_RETEST", "FVG_RETEST",
          "TREND_CONTINUATION", "TREND_PULLBACK", "MEAN_REVERSION")


def classify(d, price, zone, vz, vc, vs, cfg):
    """Return (primary, tags). vz/vc/vs = zone, confirmation and structural timeframe views."""
    tags = []
    Lc, Lz = vc["df_len"] - 1, vz["df_len"] - 1
    sw = vc["sweep"] if vc["sweep"] and vc["sweep"]["dir"] == d else vz["sweep"] if vz["sweep"] and vz["sweep"]["dir"] == d else None
    ec = vc["last_event"]
    choch_c = bool(ec and ec["dir"] == d and ec["type"] == "CHoCH" and Lc - ec["idx"] <= 15)
    disp_c = any(de["dir"] == d and Lc - de["idx"] <= 10 for de in vc.get("disp_events", []))
    if sw and (choch_c or disp_c):
        tags.append("LIQUIDITY_SWEEP_REVERSAL")
    ez = vz["last_event"]
    if ez and ez["dir"] == d and ez["type"] == "BOS" and Lz - ez["idx"] <= 15 and ez["quality"] >= 60 \
            and abs(price - ez["level"]) <= 0.6 * vz["atr"]:
        tags.append("BREAKOUT_RETEST")
    fib = vz["fib"]
    retr = None
    if fib and fib["hi"] > fib["lo"]:
        rng = fib["hi"] - fib["lo"]
        retr = (fib["hi"] - price) / rng if d > 0 else (price - fib["lo"]) / rng
    if ec and ec["dir"] == d and ec["type"] == "BOS" and Lc - ec["idx"] <= 20 and retr is not None and 0.5 <= retr <= 0.79:
        tags.append("BOS_PULLBACK")
    if zone["ob"] and vz["struct_trend"] in (0, d):
        tags.append("ORDER_BLOCK_RETEST")
    if zone["fvg"]:
        tags.append("FVG_RETEST")
    strong = vs["ms"]["trend_dir"] == d and vs["ms"]["trend_strength"] >= 70
    if strong and vc["ms"]["momentum"] == "EXPANDING" and vc["ms"]["momentum_dir"] == d and (retr is None or retr < 0.38):
        tags.append("TREND_CONTINUATION")
    if vz["ema_trend"] == d and vz["struct_trend"] == d and retr is not None and retr >= 0.38:
        tags.append("TREND_PULLBACK")
    stretched = (d > 0 and vz["bb_pct"] < 0.15 and vz["rsi"] < 35) or (d < 0 and vz["bb_pct"] > 0.85 and vz["rsi"] > 65)
    if vz["struct_regime"] == "RANGE" and stretched:
        tags.append("MEAN_REVERSION")
    prio = cfg.policy.setup_priority
    primary = next((s for s in prio if s in tags), tags[0] if tags else "UNCLASSIFIED")
    return primary, tags


class EdgeBook:
    """Per-setup evidence produced by walk-forward testing (out-of-sample only). Setups with enough trades and a
    negative expectancy are disabled; the rest are ranked by expectancy."""

    def __init__(self, data=None, min_trades=50):
        self.data = data or {}
        self.min_trades = min_trades

    @classmethod
    def load(cls, path, min_trades=50):
        p = Path(path)
        if not p.exists():
            return None
        try:
            raw = json.loads(p.read_text())
            if raw.get("source") != "walk_forward_oos":
                log.warning("edge book %s is not out-of-sample evidence; ignoring it", path)
                return None
            return cls(raw.get("setups", {}), min_trades)
        except Exception as ex:
            log.error("edge book unreadable: %s", ex)
            return None

    def allowed(self, setup):
        s = self.data.get(setup)
        return not (s and s["n"] >= self.min_trades and s["exp_r"] < 0)

    def expectancy(self, setup):
        s = self.data.get(setup)
        return s["exp_r"] if s and s["n"] >= self.min_trades else None

    @staticmethod
    def build(trades, path=None, period=""):
        out = {"source": "walk_forward_oos", "period": period, "setups": {}}
        for setup, g in trades.groupby("setup"):
            wins, losses = g.loc[g["r"] > 0, "r"].sum(), -g.loc[g["r"] < 0, "r"].sum()
            out["setups"][setup] = {"n": int(len(g)), "exp_r": round(float(g["r"].mean()), 4),
                                    "win_rate": round(float((g["r"] > 0).mean()), 4),
                                    "pf": round(float(wins / losses), 3) if losses else None}
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(json.dumps(out, indent=2))
        return out
