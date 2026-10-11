"""Python-native AI workflow engine for V5.

This replaces an external agent orchestrator/n8n dependency for the trading workflow.
It provides parallel specialist execution, bounded synthesis, persistence hooks and a
single safety boundary before the deterministic execution kernel.
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .chief import ChiefTraderBrain
from .connection import ConnectionBrain
from .intermarket import IntermarketBrain
from .models import BrainRun
from .news import NewsMacroBrain
from .opportunity import OpportunityBrain
from .quant import QuantitativeBrain
from .risk import RiskGuardianBrain
from .llm import DirectLLMGateway
from .safety import evaluate
from .technical import TechnicalBrain

log = logging.getLogger("v5.workflow")


class V5Workflow:
    def __init__(self, cfg, *, client, risk_engine=None, alerts=None):
        self.cfg = cfg
        self.client = client
        self.risk_engine = risk_engine
        self.max_workers = max(2, int(getattr(cfg.brain, "max_workers", 4)))
        self.path = Path(getattr(cfg.brain, "journal_file", "data/v5_brain.jsonl"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = ConnectionBrain()
        self.technical = TechnicalBrain()
        self.quant = QuantitativeBrain()
        self.news = NewsMacroBrain()
        self.intermarket = IntermarketBrain()
        self.opportunity = OpportunityBrain()
        self.risk = RiskGuardianBrain()
        self.chief = ChiefTraderBrain()
        self.chief.min_score = float(getattr(cfg.strategy, "min_score", 65.0))
        from .critic import TradeCriticBrain
        self.critic = TradeCriticBrain()
        self.llm = DirectLLMGateway(cfg)
        self._health_at = {}
        self.last_run = None

    def _append(self, obj):
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(obj, default=str) + "\n")
        except OSError as ex:
            log.warning("V5 brain journal write failed: %s", ex)

    def run(self, *, lifecycle: str, ms, decision, news_ctx, positions=None, frames=None, aux=None,
            reconciliation_ok: bool = True, state_recovery_pending: bool = False) -> BrainRun:
        symbol = getattr(self.client, "symbol", "")
        try:
            account = self.client.account()
        except Exception:
            account = None
        conn = self.connection.run(self.client, self.cfg, lifecycle)
        # Specialist analysis can execute in parallel because none of the processors mutates shared state.
        with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="v5brain") as ex:
            fut_tech = ex.submit(self.technical.run, getattr(ms, "views", {}) or {}, ms, frames, symbol)
            fut_news = ex.submit(self.news.run, news_ctx, symbol)
            fut_quant = ex.submit(self.quant.run, decision, account, positions or [], self.cfg,
                                  getattr(ms.micro, "score", 100.0) if getattr(ms, "micro", None) is not None else 100.0)
            fut_inter = ex.submit(self.intermarket.run, aux, symbol)
            tech = fut_tech.result()
            news = fut_news.result()
            quant = fut_quant.result()
            inter = fut_inter.result()
        opp = self.opportunity.run(decision, tech, news, quant)
        critic = self.critic.run(decision, tech, news, inter, ms, self.cfg)          # attack the candidate first
        tick_age = conn.facts.get("tick_age_s")
        calendar_required = bool(self.cfg.news.enabled and self.cfg.news.calendar_require_fresh_for_entries
                                 and self.cfg.features.require_fresh_calendar_entries and self.cfg.features.news_redundancy)
        calendar_ok = bool(news.facts.get("calendar_ok", False)) if calendar_required else True
        safety = evaluate(lifecycle=lifecycle, tick_age_s=tick_age,
                          max_tick_age_s=self.cfg.execution.max_tick_age_s,
                          calendar_ok=calendar_ok,
                          reconciliation_ok=bool(reconciliation_ok),
                          state_recovery_pending=bool(state_recovery_pending))
        risk = self.risk.run(decision, safety, account, positions or [], self.risk_engine, getattr(ms, "regime", None), self.cfg)
        chief = self.chief.run(symbol=symbol, decision=decision, connection=conn, technical=tech, quant=quant,
                               news=news, intermarket=inter, opportunity=opp, risk=risk, critic=critic)
        from instruments import profile_for
        from intel import instrument_drivers
        _desc, _drivers = instrument_drivers(profile_for(symbol))
        llm_payload = {"instrument": symbol, "instrument_drivers": f"{_desc}: {_drivers}",
                       "deterministic": chief.to_dict(), "technical": tech.to_dict(), "news": news.to_dict(),
                       "quantitative": quant.to_dict(), "intermarket": inter.to_dict(), "risk": risk.to_dict()}
        llm = self.llm.reason(llm_payload)
        if llm is not None:
            # Advisory-only merge: LLM can only reduce permission/risk, never increase them.
            chief.confidence = min(chief.confidence, float(llm.get("confidence", chief.confidence)))
            chief.risk_multiplier = min(chief.risk_multiplier, float(llm.get("risk_multiplier", chief.risk_multiplier)))
            chief.reasons = chief.reasons + [f"LLM advisory: {x}" for x in llm.get("reasons", [])[:3]]
            chief.contradictions = chief.contradictions + [str(x) for x in llm.get("contradictions", [])[:3]]
            if chief.permission == "APPROVE" and llm.get("permission") in ("REDUCE", "NO_TRADE", "BLOCK"):
                chief.permission = str(llm["permission"])
                if chief.permission != "APPROVE":
                    chief.risk_multiplier = min(chief.risk_multiplier, 0.75 if chief.permission == "REDUCE" else 0.0)
        run = BrainRun(datetime.now(timezone.utc).isoformat(timespec="seconds"), symbol, lifecycle,
                       findings={"connection": conn, "technical": tech, "news": news, "intermarket": inter,
                                 "quantitative": quant, "opportunity": opp, "critic": critic, "risk": risk},
                       decision=chief,
                       report={"deterministic_action": decision.action, "deterministic_score": decision.score,
                               "setup": decision.setup, "style": decision.style,
                               "market_health": round((conn.score + tech.score + news.score + inter.score + quant.score) / 5.0, 1),
                               "calendar_required": calendar_required,
                               "safety": {"allowed": safety.allowed, "reasons": list(safety.reasons)}})
        self.last_run = run
        self._append(run.to_dict())
        return run
