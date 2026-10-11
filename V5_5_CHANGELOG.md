# Slango Pro Trader V5.5 — Full-System Audit, Multi-Market Correctness & Telegram Intelligence

Date: 11 October 2026. Based on the V5.4 Safety & 12-Market Scan candidate.

## Critical fixes found by the audit
1. **Price precision on non-gold markets (P&L-affecting).** TP1, zone-midpoint limit prices, invalidation zones and
   the DRY_RUN fill were rounded to 2 decimals — correct only for XAUUSD. On EURUSD a TP1 could land *behind*
   the entry, so the manager closed 50% and moved the stop to break-even right after every entry; a limit could
   land beyond the stop. New `pricefmt.py` derives precision from the stop distance / ATR (gold keeps 2 decimals,
   FX gets 5, JPY pairs 3). Applied in `decision.tp1_level`, `levels.limit_price`, `levels.compute`,
   `execution` and all human-facing price text.
2. **News intelligence was gold-only on every market.** The LLM system prompt was "macro analyst for gold" and its
   `gold_bias` was applied to whatever symbol the adaptive selector chose (e.g. USDJPY, where gold-bullish usually
   means USDJPY-bearish). The prompt is now built per instrument (`intel.system_prompt(profile)` with that market's
   own drivers: central banks, yields, inventories, ETF flows ...), the schema accepts `instrument_bias`
   (legacy `gold_bias` still valid), and cached intelligence is discarded when the active market switches.
3. **Lifecycle flapping between HALTED and DEGRADED.** A recoverable DEGRADED condition (stale ticks) could
   overwrite a latched HALT; the durable latch then re-halted the next loop — a CRITICAL alert every loop. HALTED
   now only leaves through a validated resume or shutdown.
4. **Broker-data inconsistency vs the V5.4 latch.** It HALTED (writing the durable latch) and then auto-resumed to
   RUNNING, which the latch immediately reverted. It is now a recoverable DEGRADED state (entries blocked, positions
   still managed) that clears when the diagnosis is clean.
5. **Hard-coded "XAUUSD" identity.** Every alert said "XAUUSD agent" and OPEN/CLOSE reports said XAUUSD even when the
   selector traded EURUSD. Alerts now read `SLANGO PRO TRADER | Active market: <current symbol>`.
6. **Duplicate trade messages.** A fill produced up to three OPEN messages (execution alert, record notifier, V5
   reporter) and two CLOSE messages. One owner per message now.
7. **Anthropic provider currency.** Web-search tool updated to `web_search_20260209` (the 2025 variant is outdated
   for current models), `max_tokens` raised for reasoning models, and a `refusal` stop reason is treated as
   "news UNKNOWN" (never as permission).
8. Stale test (`universe_max_symbols <= 5`) updated to the intentional V5.4 12-market universe — the suite was
   red at baseline (241 passed / 1 failed).

## Enhancement Specification implemented
- **Notification state engine** (`brain/notify.py`): daily-once channels roll over at Lebanon midnight; state
  channels speak only on change (WAIT -> WAIT silent, WAIT -> SIGNAL speaks); news events advance
  DISCOVERED -> ANNOUNCED -> RELEASED (re-sent only if the numbers change); trade stages OPEN / TP1 / BE / FINISHED
  once per ticket. Persisted atomically so restarts do not replay the day.
- **Lebanon time** (`timefmt.py`): every user-facing time uses IANA `Asia/Beirut` (DST automatic, configurable via
  `NOTIFY__DISPLAY_TIMEZONE`); UTC stays canonical internally; explicit UTC fallback, never a fixed +3.
  `tzdata` added to requirements for Windows.
- **News event IDs & lifecycle**: `events.event_id` (e.g. `USD_CPI_2026-10-08_12:30`, UTC-stable),
  structured `NewsContext.events`, daily news briefing ranked by impact, upcoming-event announcement with local
  time, mechanism and affected markets (`instruments.affected_markets`), release report with actual vs forecast,
  surprise and implication for the active market, and entering/leaving the event window.
- **Instrument-aware LLM**: both the news-intelligence prompt and the V5 Chief-Trader advisory prompt name the
  active instrument and its drivers. The V5 advisory gateway now also supports `INTEL__PROVIDER=anthropic`
  (bounded timeout, no retries on the decision path, reduce-only merge unchanged).
- **Professional charts** (`brain/chart.py`): A — market structure (swings, S/R, FVG/OB zones, sweep);
  B — only the indicators the kernel computes (EMA20/50/200, RSI, Stochastic, MACD histogram, ATR, via the kernel's
  own `add_indicators`); C — execution plan (entry zone, SL, TP1, TP2, optional TP3, side, RR, timeframe, validity,
  Beirut timestamp). C is built only by `plan_from_decision` from the exact decision passed to the execution
  engine, or from the broker's executed entry/SL/TP — the chart cannot disagree with the order.
- **Daily consolidated universe health** replaces the hourly radar and the 60-minute per-market health message;
  the selector report is sent only when the selected market changes.

## Configuration
Added `NOTIFY__DISPLAY_TIMEZONE`, `NOTIFY__NEWS_ANNOUNCE_LEAD_MIN`, `NOTIFY__NEWS_ANNOUNCE_MEDIUM`,
`NOTIFY__CHART_VIEWS`. Removed `BRAIN__HEALTH_REPORT_INTERVAL_MIN` (health is daily now; the key is ignored if
still present). `BRAIN__LLM_MAX_TOKENS` default 900 -> 2000.

## Not claimed
No profitability claim. Not validated against a live broker in this environment. The trading rules, scores and
risk limits were not loosened anywhere; every change either fixes a defect, adds reporting, or reduces risk.
