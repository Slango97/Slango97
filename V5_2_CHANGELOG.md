# V5.2 — Adaptive Moderate-Veto Pro Trader Brain

## Main objective

Evolve V5.1 into a genuinely adaptive multi-market brain capable of operating across a wide account-equity range, including micro accounts, without forcing trades when broker minimum volume makes the requested risk impossible.

## Changes

### 1. Moderate veto architecture
- Kept genuine safety failures as hard blockers.
- Reduced the number of binary strategy blockers.
- One missing confirmation/trigger now reduces risk instead of automatically blocking when the other confirmation element is present.
- Moderate contradiction now reduces risk.
- Strong contradiction remains a hard block.
- Borderline pro-quality becomes a smaller position instead of an automatic veto.
- Strong, reliable news opposition remains a veto; weaker disagreement becomes a risk reduction.

### 2. Account-aware risk
- Added micro-account ceiling: 1.00% for equity <= $25.
- Added small-account ceiling: 0.75% for equity <= $500.
- Standard base remains 0.50% above that.
- Broker minimum volume can never be forced above the risk budget.
- This supports $5+ accounts when the broker/instrument's minimum tradable volume can fit the risk budget.

### 3. Score policy
- Composite score remains 65/100.
- Opposing-direction score gap reduced from 15 to 8.
- Pro-quality floor reduced to 58 with a separate 48 hard-quality floor.
- High-volatility quality floor reduced to 64.

### 4. Auditability
- Decision records now include policy risk multiplier.
- Risk authorization records account-equity tier and account risk cap.
- Score-failure decisions preserve rejected candidate explanations.

### 5. Safety preserved
- Deterministic risk/execution kernel remains authoritative.
- LLM cannot increase risk or create an order.
- High-impact news lifecycle remains protected.
- Two-loss account-wide lock and loss cooldown remain active.

## Validation

Targeted V5.1 + V5.2 adaptive suite: 136 passed, 3 non-fatal warnings in the executed core/integration suite.

Full repository pytest includes research/backtest workloads that are substantially longer; the release should use the supplied Windows verification script on the target MT5 workstation as the final environment-specific gate.
