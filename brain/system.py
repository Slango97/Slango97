"""Top-level V5 Brain System used by the trading engine."""
from __future__ import annotations

from .reporting import V5Reporter
from .workflow import V5Workflow
from .universe import MarketUniverseBrain


class V5BrainSystem:
    def __init__(self, cfg, *, client, risk_engine=None, alerts=None):
        self.cfg = cfg
        self.client = client
        self.workflow = V5Workflow(cfg, client=client, risk_engine=risk_engine, alerts=alerts)
        self.reporter = V5Reporter(cfg, alerts=alerts)
        self.universe = MarketUniverseBrain(cfg)
        self._universe_last = 0.0
        self.enabled = bool(getattr(cfg.brain, "enabled", True))
        self.enforce = bool(getattr(cfg.brain, "enforce", False))

    def run(self, *, lifecycle, ms, decision, news_ctx, positions=None, frames=None, aux=None, force_health=False,
            reconciliation_ok=True, state_recovery_pending=False):
        if not self.enabled:
            return None
        run = self.workflow.run(lifecycle=lifecycle, ms=ms, decision=decision, news_ctx=news_ctx,
                                positions=positions, frames=frames, aux=aux,
                                reconciliation_ok=reconciliation_ok, state_recovery_pending=state_recovery_pending)
        self.reporter.publish(run, frames=frames, force_health=force_health)
        return run

    def select_market_if_due(self, *, primary_views=None, force=False, allow_switch=True):
        """Adaptive market funnel: rank -> affordability -> regime/confluence -> select one candidate.

        This stage is deliberately pre-order. Full news/intermarket/critic/chief/risk/execution
        analysis still runs on the selected market in the normal engine path.
        """
        if not self.enabled or not bool(getattr(self.cfg.brain, "adaptive_market_selection", True)):
            return None
        # Fixed-symbol multi-market launcher instances must remain pinned. Adaptive selection
        # is the account-level mode (SYSTEM__SYMBOL=AUTO) unless explicitly overridden.
        fixed = str(getattr(self.cfg.system, "symbol", "AUTO") or "AUTO").upper() != "AUTO"
        if fixed and not bool(getattr(self.cfg.brain, "adaptive_selection_allow_fixed_symbol", False)):
            return None
        if not allow_switch:
            return None
        import time
        every = float(getattr(self.cfg.brain, "adaptive_selection_refresh_min", 5.0)) * 60.0
        now = time.time()
        last = getattr(self, "_selection_last", 0.0)
        if not force and now - last < every:
            return None
        self._selection_last = now
        rows = self.universe.scan(self.client, primary_symbol=getattr(self.client, "symbol", ""),
                                  primary_views=primary_views)
        ranked = self.universe.rank(self.client, rows, cfg=self.cfg)
        self.reporter.selection_report(ranked)
        eligible = [r for r in ranked if r.status == "OK" and r.affordable and r.ranking_score >= float(getattr(self.cfg.brain, "adaptive_selection_min_score", 65.0))]
        if not eligible:
            return {"selected": None, "rows": ranked, "reason": "no account-affordable market met the selection threshold"}
        best = eligible[0]
        if best.symbol == getattr(self.client, "symbol", ""):
            return {"selected": best.symbol, "rows": ranked, "changed": False, "reason": "current market remains the best eligible candidate"}
        if hasattr(self.client, "switch_symbol"):
            self.client.switch_symbol(best.symbol)
            return {"selected": best.symbol, "rows": ranked, "changed": True, "reason": "adaptive selector chose higher-ranked affordable market"}
        return {"selected": None, "rows": ranked, "reason": "client does not support safe symbol switching"}

    def scan_universe_if_due(self, *, primary_views=None, force=False):
        if not self.enabled or not bool(getattr(self.cfg.brain, "universe_enabled", True)):
            return []
        import time
        every = float(getattr(self.cfg.brain, "universe_refresh_min", 60.0)) * 60.0
        now = time.time()
        if not force and now - self._universe_last < every:
            return []
        self._universe_last = now
        rows = self.universe.scan(self.client, primary_symbol=getattr(self.client, "symbol", ""),
                                  primary_views=primary_views)
        self.reporter.universe_report(rows)
        return rows
