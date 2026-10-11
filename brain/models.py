"""Models for the Python-native V5 trading brain.

The V5 layer is deliberately dependency-light: dataclasses + stdlib only.  It is an
intelligence/orchestration layer; the deterministic trading kernel remains authoritative.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class BrainFinding:
    brain: str
    status: str = "OK"
    score: float = 0.0
    confidence: float = 0.0
    bias: int = 0
    headline: str = ""
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class V5Decision:
    action: str = "WAIT"                 # BUY | SELL | WAIT | HALT
    permission: str = "NO_TRADE"         # APPROVE | REDUCE | NO_TRADE | BLOCK
    confidence: float = 0.0              # 0..1; confidence is not probability of profit
    setup_quality: float = 0.0
    risk_multiplier: float = 1.0
    symbol: str = ""
    direction: int = 0
    reasons: list[str] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BrainRun:
    timestamp: str
    symbol: str
    lifecycle: str
    findings: dict[str, BrainFinding] = field(default_factory=dict)
    decision: V5Decision = field(default_factory=V5Decision)
    report: dict[str, Any] = field(default_factory=dict)
    universe: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "symbol": self.symbol,
            "lifecycle": self.lifecycle,
            "findings": {k: v.to_dict() for k, v in self.findings.items()},
            "decision": self.decision.to_dict(),
            "report": self.report,
            "universe": self.universe,
        }
