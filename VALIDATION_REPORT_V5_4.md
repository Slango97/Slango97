# V5.4 Validation Report

Date: 2026-10-11

## Environment and evidence
- Source: supplied V5.3 adaptive account selector ZIP, extracted into a clean working copy.
- `python -m compileall -q .`: PASS.
- Targeted tests: `python -m pytest -q tests/test_mt5_engine.py tests/test_v4_ops.py tests/test_adaptive_selector.py tests/test_v42.py tests/test_v3_core.py`: **85 passed**, one existing pandas warning.
- Test collection: **351 tests collected**.
- Full-suite attempt: `python -m pytest -q --durations=15` exceeded the 300-second execution limit after partial progress (~20%). This is INCOMPLETE, not a pass and not a demonstrated test failure. It must be rerun locally and the slow tests investigated individually.
- Broker/live execution: NOT TESTED. No live orders were sent.

## Existing warning
`mt5_client.py` emits a pandas warning about discarding nonzero nanoseconds while converting a timestamp. It did not fail the targeted suite but should be cleaned up in a later focused change.

## Release recommendation
Use DRY_RUN first, then broker-specific Demo validation. Do not enable LIVE based only on the targeted suite. Before release, finish the full suite, validate the configured symbols on the actual broker, confirm correct account mode, test halt/resume/restart and order reconciliation, and verify screenshots/Telegram payloads against the exact trade candidate.
