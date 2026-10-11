"""Hard safety boundary for the V5 AI workflow.

The AI layer may recommend, summarize, rank, or veto a candidate.  It may never
increase deterministic risk or bypass the existing execution/risk kernel.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SafetyVerdict:
    allowed: bool
    risk_multiplier: float
    reasons: tuple[str, ...] = ()


def evaluate(*, lifecycle: str, tick_age_s: float | None, max_tick_age_s: float,
              calendar_ok: bool, daily_loss_hit: bool = False,
              drawdown_hit: bool = False, reconciliation_ok: bool = True,
              state_recovery_pending: bool = False) -> SafetyVerdict:
    reasons: list[str] = []
    if lifecycle != "RUNNING":
        reasons.append(f"engine lifecycle {lifecycle}")
    if tick_age_s is None or tick_age_s < -5 or tick_age_s > max_tick_age_s:
        reasons.append("market data freshness outside safety window")
    if not calendar_ok:
        reasons.append("economic calendar unavailable/stale")
    if daily_loss_hit:
        reasons.append("daily loss limit reached")
    if drawdown_hit:
        reasons.append("maximum drawdown reached")
    if not reconciliation_ok:
        reasons.append("reconciliation not clean")
    if state_recovery_pending:
        reasons.append("state recovery pending")
    return SafetyVerdict(not reasons, 1.0 if not reasons else 0.0, tuple(reasons))


def clamp_risk_multiplier(x: Any) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except Exception:
        return 0.0
