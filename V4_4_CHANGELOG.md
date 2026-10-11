# V4.4 Ultimate Changelog

## Objective
Strengthen V4.3.1 without feature creep: make calendar/news handling trustworthy, preserve deterministic risk/execution authority, and improve research integrity and observability.

## Implemented
- Fixed fresh remote calendar data not updating `calendar_cached_at`.
- Added explicit calendar state: `FRESH`, `CACHED`, `STALE`, `UNAVAILABLE`.
- Added bounded single-provider risk reduction (`70%` by default) rather than hidden confidence inflation.
- Added separate calendar/news-age heartbeat telemetry.
- LLM provider chain is now explicitly opt-in through `FEATURES__USE_LLM`; default provider is `none`.
- Preserved the News Brain deterministic rules and confidence cap from V4.3.1.
- Preserved broker-calibrated sizing, `order_check`, pending-fill reauthorization, reconnect/reconciliation and fail-closed safety.
- Documented runtime-vs-Strategy-Tester calendar limitations.

## Validation target
Run all existing V4.3.1 regression tests plus V4.4 calendar/telemetry/LLM-gating tests before Demo. Do not promote to LIVE from software tests alone.


## V4.4.1 — Broker Auto-Discovery Hardening

- Added semantic gold-symbol discovery from the connected MT5 terminal inventory; broker suffixes such as `XAUUSDs` no longer require a hard-coded alias.
- Added account identity capture and reconnect verification; account changes fail closed.
- Changed `.env.example` default to `SYMBOL=AUTO`. Exact symbol pinning remains supported.
- Added `symbol_resolver.py` with deterministic scoring and reasons for auditability.

- Windows test portability: the Trading Economics UTC regression now avoids the POSIX-only `time.tzset()` API on Windows while preserving timezone-invariance coverage.
