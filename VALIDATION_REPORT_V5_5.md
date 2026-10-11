# V5.5 Validation Report

Date: 2026-10-11 · Environment: Linux, Python 3.13, simulated MT5 (`tests/fake_mt5.py`), no broker, no live LLM.

| Check | Baseline V5.4 | V5.5 |
|---|---|---|
| `python -m compileall -q .` | pass | pass |
| Full suite `pytest tests --ignore=tests/v3_baseline` | 241 passed, **1 failed**, ~145 s | **257 passed, 0 failed**, ~145 s |
| New regression tests (`tests/test_v55.py`) | — | 16 passed |
| `ruff --select F` on changed modules | 3 pre-existing findings in `brain/technical.py` | unchanged (none introduced) |

The V5.4 report said the full suite "exceeded 300 s"; it completes in about 2.5 minutes here (slowest test 26 s).

## What the new tests prove
- EURUSD TP1 is never rounded behind the entry; an EURUSD position at entry does not trigger TP1/BE; gold values
  unchanged; limit prices keep FX precision.
- Beirut display time is DST-aware (UTC+3 in October, UTC+2 in January), rolls the day at local midnight, and falls
  back to explicit UTC.
- Notification engine: daily-once, state-change, event-stage ordering, material-revision, trade-stage-once and
  persistence across restart.
- Reporter: five identical NO_TRADE scans -> one message; opportunity only on material change; the active market in
  every message follows the selector; announce/release once each; trade lifecycle once per ticket.
- Execution chart levels equal the execution candidate / executed values exactly; the three chart views render.
- Intelligence prompt is instrument-specific; legacy and new JSON schema both validate; switching market discards the
  previous market's intelligence.
- HALTED is never downgraded; broker-data inconsistency is a recoverable DEGRADED state without a durable latch.

## Not tested here
Real MT5 terminal and broker symbol specifications, real Telegram delivery, real Anthropic/OpenAI calls, Windows
`tzdata`. Run DRY_RUN, then broker-specific DEMO with `BRAIN__ENFORCE=false`, before any LIVE use.
