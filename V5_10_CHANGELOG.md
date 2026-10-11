# Slango Pro Trader V5.10 — Enforced Deployment Ladder

Date: 11 October 2026. Roadmap step 5: DRY_RUN -> DEMO -> LIVE, now enforced instead of documented.

## Problem
The promotion path existed only as a README table. A real account could trade at full risk on day one with a
single environment variable (`CONFIRM_LIVE=YES_I_UNDERSTAND`); nothing compared live results with the demo, and
nothing reduced exposure when live results deteriorated.

## Added — `rollout.py`
Stages `DRY_RUN -> DEMO -> LIVE_PILOT -> LIVE_FULL`, account-wide state in `data/rollout.json` (shared by all
market instances), judged from every instance's journals since the stage began:

| Promotion | Criteria (defaults, `ROLLOUT__...`) |
|---|---|
| DRY_RUN -> DEMO | >= 10 days, >= 20 simulated entries, 0 critical incidents, valid config |
| DEMO -> LIVE_PILOT | >= 20 days, >= 40 closed trades, PF >= 1.2, expectancy > 0, max DD < 8%, mean slippage <= 0.10R, 0 incidents, >= 1 market with walk-forward PASS (when evidence exists/required), AI news not HARMFUL (when enabled), `--login/--server` of the REAL account |
| LIVE_PILOT -> LIVE_FULL | >= 20 days, >= 30 trades, PF >= 1.2, expectancy > 0, max DD < 6%, pilot expectancy >= DEMO expectancy - 0.15R |

Critical incidents: non-operator HALTs, orders with unknown outcome, positions found without a stop, trades lost
from broker history, failed reconnect reconciliation. Operator STOP/flatten/resume are not incidents.

- **Promotion** is manual (`rollout.py promote --to X --by NAME`), only one rung at a time, only when every
  criterion passes; it records who, when, the checklist and an evidence snapshot in a hash-chained history.
- **Enforcement:** `AGENT_MODE=LIVE` HALTs at startup unless the stage is LIVE_PILOT/LIVE_FULL AND the connected
  login/server is the promoted one (`ROLLOUT__ENFORCE_LIVE`, default on). DEMO before passing DRY_RUN is allowed
  with an advisory warning (no money at risk).
- **LIVE_PILOT limits:** per-trade risk capped at 0.25%, at most 1 open trade (approval details show the cap).
- **Automatic demotion** (daily review): critical incidents, last-20-trades PF < 0.8, or stage drawdown >= 5% ->
  LIVE_FULL drops to LIVE_PILOT (CRITICAL alert); a failing LIVE_PILOT HALTs with the durable safety latch.
  Promotions are manual, demotions automatic: exposure only ever grows by a human decision.
- `ROLLOUT_STATUS_WINDOWS.bat`; Telegram `/status` shows the stage.

Tests: 303 passed (10 new).
