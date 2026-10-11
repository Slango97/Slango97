# V5 Ultimate Brain — Changelog

## 5.0.0

V5 adds a Python-native specialist-brain and workflow layer on top of the V4 deterministic MT5 trading kernel. The design is based on the uploaded Professional Trading Blueprint and Ultimate Pro Trader Strategy material, while preserving the existing safety and evidence architecture.

### Added

- Python-native specialist brains for connection/account, market/technical analysis, quantitative context, news/macro, intermarket context, opportunity synthesis, risk guardianship and chief-trader synthesis.
- Internal parallel workflow orchestration; OpenAI Agents SDK and n8n are not required.
- Optional direct OpenAI model gateway for advisory reasoning only.
- Strict AI safety boundary: no raw broker-order authority, no risk increase, no bypass of deterministic risk/reconciliation controls.
- Candlestick and conservative chart-pattern hints plus existing V4 market-structure/SMC indicators.
- Market-universe radar for broker-specific symbol discovery and per-market health/confidence snapshots.
- Asynchronous Telegram Brain for health, news, opportunity, no-trade, entry and trade-feedback reporting.
- Telegram opportunity chart generation with entry/SL/TP overlays.
- V5 JSONL audit stream (`data/v5_brain.jsonl`) for brain decisions and evidence.
- V5 Windows launch/run documentation.

### Reliability hardening carried forward into V5

- Separation of MT5 terminal connectivity from market-data freshness.
- Negative tick-age jitter normalization.
- Account snapshot failure now fails closed without a `NoneType` crash.
- Reduced repeated stale-tick lifecycle log spam.
- Reconciliation/state status is explicitly passed into the V5 safety boundary.

### Validation

The V5 source tree was checked with Python bytecode compilation and the complete test inventory was executed in logical batches: **201 passed, 0 failed**. The live MT5 terminal itself cannot be exercised from the build container; Windows/MT5 verification must still be performed on the user's terminal in DRY_RUN first.

### Important scope boundary

The market-universe radar provides multi-market health/analysis readiness. The existing deterministic execution kernel still executes only the broker-selected primary symbol. Multi-symbol autonomous execution requires an additional portfolio-aware execution expansion and its own evidence gates; V5 does not pretend that this is already finished.
