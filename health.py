import time

try:
    import psutil
except ImportError:          # optional
    psutil = None


class Health:
    """Tracks liveness signals and produces heartbeat snapshots and a degraded/ok verdict."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.t_start = time.time()
        self.last_tick_ok = 0.0
        self.last_candle = None
        self.last_candle_at = 0.0
        self.news_ok_at = 0.0
        self.news_error = ""
        self.errors = 0
        self.consecutive_errors = 0
        self.mt5_ok = False

    def tick_ok(self):
        self.last_tick_ok = time.time()
        self.mt5_ok = True

    def candle(self, ts):
        if ts != self.last_candle:
            self.last_candle, self.last_candle_at = ts, time.time()

    def news(self, ok, err=""):
        if ok:
            self.news_ok_at, self.news_error = time.time(), ""
        else:
            self.news_error = err

    def loop_ok(self):
        self.consecutive_errors = 0

    def loop_error(self):
        self.errors += 1
        self.consecutive_errors += 1

    def degraded_reasons(self, tick_age_s, max_tick_age_s):
        why = []
        if not self.mt5_ok:
            why.append("MT5 not connected")
        elif tick_age_s > max_tick_age_s:
            why.append(f"stale ticks ({tick_age_s:.0f}s)")
        if self.last_candle_at and time.time() - self.last_candle_at > 2 * 3600:
            why.append("no new M5 candle for 2h")
        if self.news_error:
            why.append(f"news unavailable: {self.news_error[:60]}")
        return why

    def snapshot(self, lifecycle, client, state, risk):
        snap = {"lifecycle": lifecycle, "uptime_h": round((time.time() - self.t_start) / 3600, 2),
                "errors": self.errors, "last_candle": str(self.last_candle),
                "news_age_min": round((time.time() - self.news_ok_at) / 60, 1) if self.news_ok_at else None}
        try:
            acc = client.account()
            pos = client.positions()
            r = state.risk
            if acc is None:
                raise RuntimeError("account snapshot unavailable")
            snap.update(equity=round(acc.equity, 2), open_positions=len(pos),
                        day_pnl=round(acc.equity - r.get("day_start_equity", acc.equity), 2),
                        open_risk_pct=round(risk.open_risk_pct(pos), 2), loss_streak=r.get("loss_streak", 0),
                        tick_age_s=round(max(0.0, client.tick_age_s()), 1))
        except Exception as ex:
            snap["snapshot_error"] = str(ex)
        if psutil:
            snap.update(cpu_pct=_agent_cpu_pct(psutil), mem_mb=round(psutil.Process().memory_info().rss / 1e6))
        return snap


_PROC = None


def _agent_cpu_pct(psutil):
    """CPU used by THIS agent process, 0-100% of the whole machine (V5 reported whole-machine CPU)."""
    global _PROC
    if _PROC is None:
        _PROC = psutil.Process()
        _PROC.cpu_percent(None)                     # first call primes the counter
        return 0.0
    return round(_PROC.cpu_percent(None) / max(1, psutil.cpu_count() or 1), 1)
