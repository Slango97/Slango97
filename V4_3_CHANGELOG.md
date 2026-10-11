# V4.3 Ultimate Hardening Changelog

## Deterministic trading logic
- Added an independent pro-quality evidence-diversity gate and minimum quality floor; high volatility requires a higher floor.
- Added adaptive risk reduction for cautious microstructure, high volatility, expansion regimes and event caution; exposure can never exceed the configured base risk from this layer.
- Added execution-level defense-in-depth checks for current market quality, regime, news/calendar freshness, spread-to-stop cost and server-clock drift.
- Added immediate re-validation of armed M1 decisions immediately before execution.

## Pending-order integrity
- Pending risk remains reserved until fill or cancellation.
- Broker-acknowledged LIMIT orders are verified as visible before state is persisted.
- Pending fills are re-authorized against current blockers, news, spread, risk, price geometry and sizing; unauthorized fills are flattened.
- Pending cancellation/flatten remain best-effort risk reduction even when broker preflight is unavailable.

## Broker / MT5 hardening
- Strict `MetaTrader5.order_check` for new entries and SL/TP modification.
- Server-time drift measured from live tick timestamps.
- Safety actions do not silently abort because preflight failed; broker send is attempted and the resulting state is verified.

## Research realism and anti-overfitting
- Side-aware bid/ask handling in counterfactual/replay paths.
- Nested purged CV for meta-label threshold selection; outer folds remain untouched for reported metrics.
- Block-bootstrap Monte Carlo retained.
- Model/artifact provenance includes code, config, dependency and data fingerprints.

## Important
V4.3 is a stronger engineering/research system, not proof of profitability. It must be validated on multi-year real XAUUSD M1 data using realistic spread, slippage, commission and out-of-sample walk-forward testing before DEMO/LIVE.
