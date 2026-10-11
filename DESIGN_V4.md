# V4 design record

Produced for the V4 Master Specification. V3 remains a separate, runnable package (xauusd_agent_v3.zip); V4 is a
controlled copy with every new behaviour behind a feature flag.

## 1. Baseline and regression strategy

- The V3 test suite is frozen unchanged in `tests/v3_baseline/`. `python tests/run_tests.py --v3-baseline` re-runs it with
  `V4_FEATURES=off` (every V4 flag off): **109/109 pass**, so V3 behaviour is reproducible and comparable at any time.
- `python tests/run_tests.py` runs the V3 tests plus the V4 acceptance tests in V4 mode: **133/133 pass**.
- Superseded V3 expectation (documented, changed only in the V4 copy of the test):
  `test_event_lifecycle...`: 40 minutes before CPI is now `CAUTION` instead of `CLEAR` (new caution window, spec 2.5).

## 2. Module map (V4 architecture, spec section 3)

| Spec module | Files |
|---|---|
| 1 Market data | mt5_client.rates (validated, UTC), indicators.validate_ohlc, engine.refresh_views (HTF fetch schedule) |
| 2 Broker intelligence | broker_diag.py (+ mt5_client.broker_diagnosis, fail-closed sizing) |
| 3 Regime engine | regime.py, microstructure.py, sessions.py, context.py (monthly/yearly references) |
| 4 Structure engine | smc.py (swings, HH/HL/LH/LL, BOS/CHoCH quality, displacement, FVG, OB, sweeps), liquidity.py, levels.barriers |
| 5 Technical features | indicators.py |
| 6 MTF decision engine | mtf.py (roles + alignment/conflict table), refine.py (M1 under M5) |
| 7 Quant engine | calibration.py (buckets), metrics.py (tails), optimize.py (WFO, ablation, Monte Carlo), drift.py |
| 8 News/intelligence | events.py, sources.py, news.py (redundant providers, health), intel.py (provider chain) |
| 9 Signal fusion | decision.py (generate + policy, all blockers, priority order), confluence.py, setups.py |
| 10 Risk engine | risk.py, portfolio.py, diagnostics.py |
| 11 Execution engine | execution.py, trade_manager.py, management.py, reconciliation.py |
| 12 Learning pipeline | learning.py, registry.py, drift.py |
| 13 Explainability/event bus | records.py, bus.py |
| 14 Telegram/ops | explain.py, alerts.py, health.py |
| 15 Persistence/audit | state.py, journal.py (decisions.csv, decision_records.jsonl, trades.jsonl, events.jsonl), registry manifest |

Dependency direction: data -> analysis -> regime/structure -> decision.generate -> decision.apply_policy -> risk ->
execution. The intelligence layer (intel/news/sources) imports nothing from execution, risk or MT5; a test enforces it.

## 3. Design diff vs V3

| Area | V3 | V4 | Flag |
|---|---|---|---|
| Decision priority | regime -> news -> micro -> candidates | data -> broker -> risk -> market quality -> news -> regime -> HTF/MTF -> trigger -> M1 -> expected R -> LLM | (structural) |
| Blockers | first failing gate counted | every gate evaluated, all blockers recorded per candidate | decision_records |
| MTF | role scores | + explicit per-role alignment and conflict table | (structural) |
| Execution timing | enter on M5 decision | M5 decision is ARMED; M1 confirms, delays (no chasing) or cancels | m1_refinement |
| Macro context | none | monthly/yearly open/high/low/mid as references and liquidity, never votes | macro_context |
| Target feasibility | S/R (3+ touches) rejects | + opposing HTF zones and macro extremes; targets capped before barriers | target_feasibility_v4 |
| Sessions | 7 phases | + measured session statistics; optional Sydney/Tokyo split | sessions.split_asia |
| News | one calendar, RSS | several calendar providers + manual file, health, de-dup, staleness; per-component errors; CAUTION phase | news_redundancy, event_caution |
| Intelligence | one provider | provider chain (Anthropic -> OpenAI / OpenAI-compatible) | intel.fallback_providers |
| Broker | warn if calc differs >10% | diagnose cause, trust broker calc only with independent confirmation, otherwise HALT; no silent tick fallback | broker_fail_closed |
| Reconnect | 3 blocking retries | backoff + jitter + ceiling + cooldown; full reconciliation before trading | reconnect_backoff |
| Observability | text alerts | records -> event bus -> Telegram OPEN / REJECTED / CLOSE / SAFETY / DAILY | telegram_explain, rejected_alerts |
| Learning | optimize.py files | registry with versions, gates, explicit approval, rollback; drift monitor | registry_models, drift_monitor |
| Research | windowed indicators, 1 process | precomputed causal indicators, parallel workers, realistic costs, M1 replay, rejected-opportunity dataset | precomputed_indicators, realistic_costs, rejected_dataset |

## 4. Evidence produced in this environment

Benchmarks (synthetic data, 1 CPU, same machine):

| Measure | V3 | V4 |
|---|---|---|
| Backtest stage 1, ms per decision point (1440 points, 1 process) | 32.03 | 14.29 (2.24x faster) |
| Live MT5 data pulls per hour (6 timeframes, simulated) | 72 | 20 |
- Parallel workers produce identical signals to the sequential run (tested); the speed-up could not be measured on a 1-CPU sandbox.

Correctness / robustness / failure recovery covered by tests: reconnect backoff and ceiling, no hammering during outages,
trading blocked until post-reconnect reconciliation (account, positions, pending orders, metadata, server time, candles,
state), broker discrepancy causes (consistent / metadata wrong / currency conversion / unexplained -> HALT / calc unavailable
-> sizing refused), calendar provider failover and de-duplication, feed failure surfacing, intelligence provider failover,
a 150-case fuzz proving LLM output cannot open trades past deterministic gates, decision records and Telegram formatting,
M1 arming/expiry/execution, registry promotion gates, approval, rollback and tamper detection, drift detection, learning gates.

Not available here (must be produced on your machine before Demo): multi-year M1 backtests and walk-forward results on
real XAUUSD data, a broker_diag.py report from your terminal, DRY_RUN logs with Telegram output, fault-injection on a live
MT5 terminal (unplug the network, restart the terminal) and parallel-worker timings on a multi-core machine.


## V4.2 additions

New modules: redact.py (secret redaction), release_manifest.py (release integrity), export_history.py and evidence.py
(evidence on your data), metalabel.py (learned filter with purged CV), macro_feeds.py (gold drivers), shadow.py
(shadow testing), telegram_cmd.py (authenticated commands). Changed: news.py (calendar policy, backoff, UTC, title
mapping), registry.py (metadata verification, HMAC), alerts.py, execution.py + mt5_client.py (limit orders),
engine.py (meta model, drivers, pending orders, shadow, commands), backtest.py (labels, dataset, limits, drivers),
config.py (DST-aware sessions, new flags), smc.py (vectorised swings), optimize.py (parallel trials).
The dependency rule still holds: intel/news/sources/macro_feeds/telegram_cmd import nothing from execution or MT5;
Telegram commands act only through the kill-switch files.


## 4 V4.3 hardening and pro-trader layer

V4.3 keeps the deterministic control plane authoritative while adding stronger decision selectivity and execution freshness.

- **Evidence quality:** `pro_quality.py` requires independent evidence diversity and a separate quality-score floor. High/EXTREME volatility raises the floor.
- **Adaptive exposure:** risk is reduced, never increased, in cautious microstructure, high volatility, expansion regimes and event caution.
- **Freshness:** an armed M1 decision is cancelled if the live market-quality or regime state becomes unsafe before execution.
- **Pending integrity:** pending LIMIT orders reserve risk, are verified after placement, and are re-authorized at fill against current blockers and broker risk.
- **Execution preflight:** entry/SLTP requests use broker `order_check`; cancellation/flatten remains best-effort risk reduction.
- **Research realism:** long/short counterfactual and replay paths are side-aware for spread; meta-label thresholds are selected inside nested inner folds; block-bootstrap Monte Carlo is retained.
- **Provenance:** model/registry artifacts remain tied to code/config/dependency/data fingerprints.

These changes improve robustness but do not establish profitability. Profitability must be established from untouched multi-year XAUUSD M1 OOS evidence after realistic costs.
