# V5.4 Safety Hardening — 11 October 2026

## Scope
This release candidate applies a focused set of safety changes to the supplied V5.3 Adaptive Account Selector project. It is not a claim of profitability or production/live readiness.

## Changes
- Added a durable `data/safety_halt.json` latch when the engine enters `HALTED`.
- Removing the STOP marker no longer automatically returns the engine to `RUNNING`.
- Added a one-shot `data/resume.request` flow. Telegram `/resume <PIN>` requests a resume; the engine consumes the request and runs account-mode, broker validation/diagnostics, reconciliation and hard-risk checks before clearing the latch.
- Broker-side loss-per-lot calculation for cross-symbol positions now rejects invalid direction, non-finite/non-positive prices, stops on the wrong side, and missing/non-loss broker calculations instead of returning a zero loss.
- Portfolio risk calculation now fails closed on invalid equity/position data, missing or invalid protective stops, and invalid broker loss calculations.
- Risk approval rejects when portfolio risk cannot be computed reliably.
- Added regression tests for cross-symbol loss calculation fail-closed behavior and updated STOP/resume expectations.

## Validation performed in this build environment
- `python -m compileall -q .`: passed.
- Targeted safety/adaptive/regression tests: run after packaging; see `VALIDATION_REPORT_V5_4.md`.
- No live broker order was sent. No claim of live profitability is made.

## Important limits
- A $5 account may be unable to trade some instruments because the broker's minimum lot can exceed the allowed loss budget or margin. The system must reject those trades; it must never force a minimum lot.
- Test doubles do not prove real MT5 execution behavior. Validate on the intended broker's Demo account, with broker-specific symbols and specifications.
