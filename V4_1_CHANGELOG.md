# Expert AI Agent Pro Trader V4.1 — Hardening Release

## Purpose
V4.1 is a focused safety, reliability, reproducibility and research-hardening release on top of V4.0. The trading architecture and core strategy logic are preserved.

## Implemented changes

### 1. Economic-calendar safety is fail-closed
- `NEWS__BLOCK_IF_CALENDAR_UNAVAILABLE` now defaults to `true`.
- Missing, stale or insufficient calendar coverage blocks new entries.
- A last-known-good calendar cache is retained with an explicit age and is never treated as fresh merely because the process restarted.

### 2. Real independent-calendar support
- Forex Factory current-week and next-week feeds are explicitly treated as one vendor family.
- Added an optional Trading Economics JSON adapter. It is only activated when `TRADING_ECONOMICS_API_KEY` is present.
- When an independent provider is configured, V4.1 can require all configured remote calendar families to be fresh/successful before permitting new entries.
- Manual/local calendar input remains an emergency source and is not counted as independent vendor redundancy.

### 3. Minimum-lot risk is strictly fail-closed
- Removed the previous 1.5x minimum-lot risk-budget exception.
- If the broker minimum tradable volume would exceed the requested risk budget, the trade is rejected.

### 4. Reproducible learning artifacts
Learning candidates now record:
- code fingerprint
- complete config fingerprint
- dependency-version fingerprint
- feature-version fingerprint
- existing historical-data fingerprint

This makes model/parameter provenance auditable across deployments.

### 5. Stronger Monte Carlo
- Monte Carlo now supports block bootstrap resampling.
- Default V4.1 learning settings use short blocks to preserve some short-range clustering in trade outcomes rather than assuming every trade is independent.
- Number of simulations and block length are configurable under `LEARNING__MC_SIMS` and `LEARNING__MC_BLOCK_LEN`.

### 6. Verified notification delivery
- Telegram/Discord HTTP notification delivery now checks response status codes.
- Failed HTTP deliveries retry with exponential delay.
- Alert delivery remains outside the trading loop and still never raises into the trading engine.

### 7. Test/reliability cleanup
- Added V4.1 hardening tests.
- Added pytest package initialization/conftest so V3 baseline and V4 test modules can coexist without import-name collisions.
- Preserved V3 baseline regression tests.

### 8. Environment compatibility baseline
The main requirements file now uses:
- NumPy `1.26.4`
- MetaTrader5 `5.0.6231`

These match the validated Windows/Python 3.11 MT5 workstation baseline used for the V3 environment and avoid the NumPy/MetaTrader5 import incompatibility found earlier.

## Validation performed in the build environment
- Python compilation: PASS
- V4.1 hardening tests: PASS
- Fast safety/regression suite: **102 passed, 1 warning**
- All 12 V4 research tests passed when executed individually.
- Backtest safety/determinism/summary tests passed individually, including the two computationally heavier full backtest tests.

The recursive `pytest tests` invocation can still time out when multiprocessing-heavy research tests and long backtests are collected into one process tree. This is a runner/process-isolation limitation; no assertion failure was observed in the isolated tests.

## Deployment note
Do not switch to LIVE because the software passes tests. Next validation stage is:

1. Install the locked workstation dependencies.
2. Run broker diagnostics on the actual XAUUSD account.
3. Configure the independent calendar provider if desired.
4. Run V4.1 in DRY_RUN.
5. Run multi-year real XAUUSD M1 walk-forward/OOS research with realistic spread, slippage and session costs.
6. Run DEMO and compare live decisions/records with research expectations.
7. Only after evidence supports the edge should LIVE be considered.
