# Slango Pro Trader — V5.8 (Expert AI Agent Pro Trader)

## V5.8 live cross-market correlation matrix (see V5_8_CHANGELOG.md, CORRELATION_MATRIX_AR.md)

Portfolio risk now uses correlations measured every hour from the broker's H1 bars for the whole watchlist (long and
short window, shrunk toward the factor prior), maps broker names (XAUUSDs, US30Cash) to markets, sizes each pair at
the more dangerous end of its range and only half-trusts hedges. Stale matrix -> factor prior (now covering all 12
markets). Daily report lists the correlated clusters. Suite: 283 passed.

## V5.7 drawdown- and volatility-aware exposure scaling (see V5_7_CHANGELOG.md, EXPOSURE_SCALING_AR.md)

Per-trade risk now shrinks automatically - and only ever shrinks - when the account is more than 2% below its
equity peak (linear to x0.35 at 6%), when market ATR exceeds 1.3x its recent average, or when the account's own
daily volatility exceeds a 12%/yr target. Live trading and the backtester share one function, the market research
judges the sizing actually used and reports with-vs-without scaling, and Telegram reports only band changes.
Suite: 276 passed.

## V5.6 per-market walk-forward evidence (see V5_6_CHANGELOG.md, MARKET_RESEARCH_AR.md)

`RESEARCH_MARKETS_WINDOWS.bat` exports your broker's history for all 12 watchlist markets and runs walk-forward,
cost-stress and Monte Carlo tests per market against PF >= 1.3, Sharpe >= 0.8, max DD < 15%. With
`BRAIN__REQUIRE_MARKET_EVIDENCE=true` only markets with a fresh PASS can be selected or traded. Suite: 268 passed.

## V5.5 audit & intelligence upgrade (see V5_5_CHANGELOG.md, VALIDATION_REPORT_V5_5.md, SYSTEM_AUDIT_V5_5_AR.md)

A full-system audit fixed defects that mattered once the adaptive selector started trading markets other than gold:
2-decimal price rounding (EURUSD TP1 could trigger right after entry), a gold-only news-intelligence prompt applied
to every market, HALTED/DEGRADED alert flapping, hard-coded "XAUUSD" message identity and duplicate trade
messages. It also implements the Enhancement Specification: a Telegram notification state engine (no scan / no-trade
spam, once-per-stage news and trade events), Asia/Beirut display time, stable news event IDs with announce/release
reports and affected markets, instrument-aware LLM prompts (Anthropic or OpenAI), and three chart views whose
execution levels are copied from the exact execution candidate. Full suite: 257 passed.

# Expert AI Agent Pro Trader — V5.2 Ultimate Brain Architecture

## V5.2 (see V5_2_CHANGELOG.md)

## V5.3 adaptive market-selection upgrade

V5.3 turns the watchlist from a passive radar into an **account-aware market-selection funnel** when `SYSTEM__SYMBOL=AUTO` (the default). While flat, the engine ranks up to the configured 2-5 liquid markets by market health, broker minimum-lot affordability, regime suitability, confluence and session quality. It then switches the active symbol to the best eligible candidate and rebuilds all symbol-local state before running the complete downstream chain: **News → Intermarket → Trade Critic → Chief Trader → Risk → Execution**.

For a $5-$500 account, the selector uses the actual broker minimum volume and broker-side loss-per-lot calculation. A market is not eligible when its minimum tradable volume would exceed the account's risk ceiling. It never forces a minimum lot just to make a trade happen. Fixed-symbol multi-market launcher instances remain pinned by default, so `START_MULTI_V5_WINDOWS.bat` still provides deliberate parallel market coverage.

V5.2 moderate vetoes and account-aware risk ceilings remain active: genuine safety failures are hard blockers, while borderline confirmation, contradiction, quality and news disagreement generally reduce risk instead of killing a technically valid setup. See `ARCHITECTURE_V5_3_ADAPTIVE_SELECTION.md` and `V5_3_CHANGELOG.md`.

## V5.1 (see V5_1_CHANGELOG.md)

Official, key-free news (Federal Reserve, BLS, ECB + ForexFactory calendar cross-checked with the BLS release
schedule), the trading documents' impact filter (red = no entries 60 min before to 15 min after, then absorption;
orange = monitored; yellow = off), every market via instrument profiles and START_MULTI_V5_WINDOWS.bat, and the
documents' rules: two-loss lock, 15-minute cooldown, TP1 50% + stop to entry + spread, RSI/Stochastic triggers,
pre-flight checklist, Trade Critic, pre-market If-Then plan and weekly review.


V5.0.0 is the Python-native Ultimate Brain layer built on the V4.4.1 deterministic trading kernel. It replaces the need for a separate agent-orchestration product or n8n workflow for the trading path. The V5 brains coordinate market, technical, quantitative, news/macro, intermarket, opportunity, critic/chief-trader and risk reasoning, while deterministic risk, broker validation, execution, reconciliation and emergency controls remain authoritative.

## V5 operating model

```
MT5 / account / feeds
        ↓
V5 Python workflow orchestrator
        ↓
┌────────────── specialist brains (parallel) ──────────────┐
│ Connection │ Technical │ Quant │ News/Macro │ Intermarket │
└──────────────────────────┬──────────────────────────────┘
                           ↓
                    Opportunity Brain
                           ↓
                     Trade Critic
                           ↓
                     Chief Trader
                           ↓
                   Risk Guardian Brain
                           ↓
                 Deterministic Risk Gate
                           ↓
                    Execution Kernel
                           ↓
                   Position Management
                           ↓
                Journal / Evidence / Learning
                           ↓
                  Telegram Intelligence
```

### V5 safety invariants

- AI cannot create or send raw broker orders.
- AI cannot increase deterministic risk.
- AI cannot turn a deterministic WAIT/HALT into a trade.
- Stale required market/calendar data, reconciliation failures and state-recovery conditions remain fail-closed.
- Telegram delivery is asynchronous and cannot block the trading loop.
- The V5 LLM path is optional and disabled by default.

### V5 market radar

### Adaptive account-level market selection

With `SYSTEM__SYMBOL=AUTO`, the watchlist is no longer just a radar. On the configured refresh interval, the selector builds a broker-specific snapshot, checks account affordability, classifies regime and confluence, ranks the eligible markets, and can safely switch the active execution symbol while the account is flat. The selected market is then rebuilt from fresh symbol-local data and must pass the normal deterministic decision, news, intermarket, Trade Critic, Chief Trader, Risk Engine and Execution gates.

The selector is **not** allowed to switch while agent positions or pending orders exist. Fixed-symbol instances launched by `START_MULTI_V5_WINDOWS.bat` remain pinned unless `BRAIN__ADAPTIVE_SELECTION_ALLOW_FIXED_SYMBOL=true` is explicitly enabled. This prevents multiple intentional market instances from collapsing onto the same automatically selected symbol.

### Telegram intelligence

V5 can publish market-health reports, macro/news updates, opportunities, no-trade reasons, entry reports, trade closures and multi-market radar snapshots. Opportunity reports can include an automatically rendered technical chart with Entry/SL/TP overlays.

### Current release posture

Start in `DRY_RUN` with `BRAIN__ENFORCE=false`. The V5 layer is production-oriented engineering, not a profitability claim. Validate broker data, news/calendar availability, multi-session behavior, slippage and out-of-sample strategy evidence before considering DEMO and then LIVE.

---

## V4.3.1 (see V4_3_1_CHANGELOG.md)

Fixes for the V4.3 review (true clock-drift measurement, one bid/ask price basis matching MT5's bid-based bars, correct
limit-fill side, break-even regret statistic, hard-only fill re-authorisation, rollover-aware limit expiry, backtest/live
parity for limit + M1, DRY_RUN limit simulation, release hygiene) plus deterministic caution-only headline reasoning
(`news_rules.py`) and the gate funnel report. Run guide: RUN_V4_3_1_WINDOWS.md.

# XAUUSD MT5 agent v4.4 Ultimate

A multi-timeframe gold trading agent for MetaTrader 5. **What changed in V4, the module map, the design diff and the evidence
are in [DESIGN_V4.md](DESIGN_V4.md).** V3 remains a separate runnable package; every V4 feature has a flag
(`FEATURES__NAME=false`) and `V4_FEATURES=off` reproduces V3 behaviour. The system is organised around three goals:
**safety** (a software, broker or API failure must not damage the account), **intelligence** (regimes,
liquidity, SMC quality, news) and **evidence** (prove out-of-sample which components actually earn money).

**Read this first.** Nothing here is a proven edge. Scoring weights and thresholds are reasoned defaults that
have not been validated on real XAUUSD history. Everything is tested offline against a simulated MT5 and simulated
news APIs, not against a live terminal, Anthropic or OpenAI. Follow the promotion path below and never skip a stage.
Leveraged trading can lose more than you can afford. This is software documentation, not financial advice.

## V4.4 Ultimate upgrades

- Calendar state machine: FRESH / CACHED / STALE / UNAVAILABLE with correct cache timestamps.
- Free MT5-runtime calendar fallback is available through an isolated adapter when supported by the connected terminal; Strategy Tester historical research must use exported point-in-time/calendar data.
- Single-calendar-provider operation is explicitly marked and can reduce risk rather than silently masquerading as cross-checked coverage.
- News Brain rules remain deterministic and caution-only: causal-family deduplication, surprise-aware event logic, negation/reversal handling, price-echo filtering and headline confidence cap.
- Heartbeat separates calendar age/state from headline age and exposes the last trading block reason.
- LLMs are optional and disabled by default; when enabled, they remain bounded intelligence and cannot override deterministic risk/execution controls.

## Architecture

```
                                   MT5
                                    |
                              MT5Client  (connection monitor, broker validation, verified execution)
                     +--------------+---------------+
                     |                              |
              MARKET ENGINE                    NEWS ENGINE  (background thread)
   indicators, smc (structure, displacement,   events.py   calendar lifecycle + surprise + learned impact
   FVG, OB), sessions, liquidity map,          sources.py  reliability + confirmation of big claims
   microstructure (market quality)             intel.py    Anthropic / OpenAI, optional web search, expiry
                     |                              |
                     +--------------+---------------+
                                    |
                           regime.py        MARKET REGIME ENGINE     which setups are allowed now?
                                    |
                           decision.generate  (expensive, cacheable)
                             mtf.py        timeframe hierarchy: macro > structure > zone > confirm > entry
                             setups.py     SETUP CLASSIFIER (8 explicit setup types)
                             levels.py     setup-aware stop and target
                             confluence.py CONTRADICTION DETECTION
                                    |
                           decision.apply_policy  (cheap, switchable)  -> BUY | SELL | WAIT | HALT
                             calibration.py  P(win), expected R   setups.EdgeBook  out-of-sample setup evidence
                                    |
                           risk.py + portfolio.py   RISK GATE (limits, projected + correlated risk, margin, throttle)
                          /                     \
                     REJECT                     ALLOW
                                                  |
                                          execution.py   EXECUTION ENGINE
                                                  |
                                          trade_manager.py + management.py   TRADE MANAGER (+ live MFE/MAE)
                                                  |
                                          reconciliation.py   RECONCILIATION (restart recovery, closed trades)
                                                  |
                                          journal.py   TRADE JOURNAL
                                                  |
                                metrics.py + analytics.py   PERFORMANCE ENGINE
                                                  |
                                          optimize.py   WALK-FORWARD / ABLATION / MONTE CARLO
```

`engine.py` runs the lifecycle (STARTING, CONNECTING, VALIDATING, SYNCING, RUNNING, DEGRADED, HALTED, SHUTDOWN)
with a fast loop every 2 s (connection, ticks, market quality, risk, position management, kill switches), a
candle step on every closed M5 candle (regime, decision, execution) and a news thread.

The LLM never opens, sizes or closes trades. It can veto (only with reliable evidence) or add at most +5 points.

## What decides a trade

1. **Regime** (`regime.py`, from H1 with H4 context, market quality and the event engine):
   ABNORMAL, NEWS_DRIVEN, EXTREME mean stand down (HALT). The others enable only fitting setups:

   | Regime | Allowed setups |
   |---|---|
   | STRONG_TREND | trend pullback, trend continuation, BOS pullback, FVG retest, OB retest |
   | WEAK_TREND | the same plus liquidity-sweep reversal (smaller size in tiered mode) |
   | RANGE / COMPRESSION | mean reversion, liquidity-sweep reversal |
   | BREAKOUT | breakout retest, BOS pullback, FVG retest |
   | REVERSAL | liquidity-sweep reversal, OB retest, FVG retest |
   | EXPANSION | trend continuation, BOS pullback, breakout retest (smaller size in tiered mode) |

2. **Timeframe hierarchy** (`mtf.py`, roles in `HierarchyConfig`). For the day style: D1 macro direction,
   H4 structural direction (an opposing H4 vetoes the trade), H1 setup zone (order block or FVG), M15 confirmation
   (BOS/CHoCH, liquidity sweep or displacement), M5 entry trigger. Scalp shifts everything one level down; swing and
   position shift it up.
3. **Setup type** (`setups.py`): liquidity sweep reversal, breakout retest, BOS pullback, order block retest, FVG
   retest, trend continuation, trend pullback, mean reversion. Every trade is tagged so performance is measured per setup.
4. **Levels** (`levels.py`): trend setups target the next structural swing, sweep reversals the opposing liquidity
   pool, breakouts a measured move, mean reversion the middle of the range. Stops sit beyond the zone and sweep, are
   moved off obvious stop pools, and widen in high volatility; position size shrinks so money at risk stays constant.
5. **Score** (technical, 100): hierarchy 25, structure 10, zone 20, location 10, liquidity 10, momentum 10, room 5,
   volatility 5, session phase 5. News: veto or 0 to +5.
6. **Policy gates** (`PolicyConfig`, each can be switched off and ablated): regime filter, market-quality gate
   (below 40 = no trade), confirmation + trigger required, edge book, contradiction check (index of 0.5 or more = WAIT),
   minimum score and lead over the opposite direction, optional calibrated expected-R minimum.
7. **Sizing** (`SizingConfig`): `fixed` by default (base risk x style multiplier, no confidence boosts). `tiered`
   (1.0 / 0.7 / 0.4 of base, never above) only after you have tested it; cautious market quality, mixed signals and
   size-down regimes each step a tier down.

WAIT and HALT are normal outcomes. Every decision is logged with its reason in `data/decisions.csv`, and the
heartbeat reports how often each no-trade reason fired.

## News and events

- **Events** (`events.py`): PRE (blackout) -> RELEASE -> STABILIZING (until 1-minute ranges and spread return to normal,
  bounded per event class) -> RE_EVALUATE -> CLEAR. Classes: CPI, PPI, PCE, NFP, FOMC, Fed speeches, GDP, retail sales,
  ISM/PMI, jobless claims, Treasury auctions. Forecast/previous/actual give a surprise direction for gold. Typical moves
  per class can be learned from your own data with `events.learn_impacts`.
- **Sources** (`sources.py`): every headline gets a reliability score (official sources and Reuters/Bloomberg high,
  social media low). High-impact claims count as confirmed only from a top-tier source or two independent reliable ones.
- **Intelligence** (`intel.py`): returns `gold_bias, confidence, risk_level, key_drivers (with source), contradictions,
  trade_veto, expiry_minutes, summary`, strictly validated. Its weight fades over its life and it is ignored after expiry.
  A veto or "extreme" risk without reliable backing is downgraded. Anything malformed means news = UNKNOWN (neutral).
- **Providers**: `INTEL__PROVIDER=anthropic` (default; `INTEL__WEB_SEARCH=true` enables the web search tool) or
  `openai` (set `INTEL__OPENAI_MODEL`; uses the Responses API. That code path could not be tested here, so verify model
  and tool names against OpenAI's current docs) or `none`.

## V4.3 workflow (see V4_3_CHANGELOG.md)
### V4.3 hardening / pro-trader layer

- Independent pro-quality evidence gate with a separate quality floor; no single score factor or LLM output can bypass it.
- Adaptive risk only reduces exposure in cautious/high-volatility/expansion/event conditions; it never increases base risk.
- M1 execution is re-validated against current microstructure and regime immediately before entry.
- Pending LIMIT fills are re-authorized against current global blockers, news, spread, risk and stop/target geometry.
- Broker `order_check` is required for new entries; safety-reducing cancellation/flatten operations remain best-effort if preflight is unavailable.
- Pending orders are confirmed visible after broker acknowledgement and are rejected if the broker does not expose the ticket.
- Research uses side-aware bid/ask execution, nested purged meta-label threshold selection, block-bootstrap Monte Carlo and reproducible provenance.
- Economic-calendar freshness is fail-closed for new entries when the V4 news redundancy layer is enabled.


```
python export_history.py --years 3 --out history --aux USDX,US10Y,EURUSD     # on the MT5 workstation
python evidence.py --csv history/XAUUSD_M1.csv --timeframe M1 --offset 2 --broker-diag --out evidence
python metalabel.py train --csv history/XAUUSD_M1.csv --timeframe M1 --offset 2 --register
python learning.py status / approve <id> --by <name>                          # explicit, recorded promotion
python release_manifest.py verify                                             # release integrity
```
New behaviours are opt-in: `FEATURES__MACRO_FEEDS`, `FEATURES__LIMIT_ENTRIES`, `POLICY__USE_META_MODEL`,
`FEATURES__SHADOW_MODE`, `FEATURES__TELEGRAM_COMMANDS`, `SESSIONS__DST_AWARE`. Enable each only after evidence.py /
shadow mode shows it helps on your data.

## V4 quick reference

- **Execution timing**: an M5 decision is ARMED; M1 confirms (BOS/CHoCH, sweep or momentum), waits (no chasing beyond
  0.35R) or cancels (0.6R toward the stop, news, risk gate, 10-minute window). M1 never creates a trade.
- **Broker check**: `python broker_diag.py` prints why tick-value sizing and `order_calc_profit` differ. Unexplained
  differences halt trading; the diagnosis re-runs hourly and after every reconnect.
- **Reconnect**: exponential backoff with jitter, a retry ceiling and a cooldown; trading resumes only after account,
  positions, pending orders, symbol metadata, server time, candles and state have been reconciled.
- **Telegram**: OPEN (direction, setup, levels, risk, R:R, regime, score / P(win), MTF table, liquidity, momentum, news,
  reasons, risk authorisation), REJECTED (what was considered and every blocker), CLOSE (P/L, R, exit reason, MFE/MAE),
  SAFETY (disconnects, degraded, recovery, halts, feed failures, drift), DAILY report. All built from
  `data/decision_records.jsonl`.
- **Learning**: `python learning.py candidate ...` -> gates (walk-forward OOS trades / expectancy / PF, not worse than
  defaults, parameter stability, stress costs, sensitivity, Monte Carlo, calibration) -> `learning.py approve <id> --by NAME`
  -> applied at the next start; `learning.py rollback --kind params --by NAME`; `learning.py drift`.
- **Research**: `backtest.py --workers N` (parallel), realistic session/event costs, M1 replay of the refinement, and
  `rejected_opportunities.csv`: what every blocked candidate would have made, per blocker.

## Install (Windows)

1. Install the MT5 terminal, log in to a **demo** account, keep it running. Tools > Options > Expert Advisors >
   **Allow algorithmic trading**, and turn the **Algo Trading** button green. Make the gold symbol visible in Market Watch.
2. Python 3.11 or 3.12 (64-bit):
   ```
   python -m venv .venv && .venv\Scripts\activate
   pip install -r requirements.txt
   copy .env.example .env      (edit it)
   python main.py
   ```
3. After the first good install: `pip freeze > requirements.lock` and use that file from then on.

## Promotion path

| Stage | What | Pass criterion (suggested) |
|---|---|---|
| 1. Evidence | `optimize.py` on 2+ years of your broker's M1 data | OOS expectancy > 0 over 100+ trades, Monte Carlo p95 drawdown you can live with |
| 2. DRY_RUN | `AGENT_MODE=DRY_RUN`, 2+ weeks | decisions and WAIT reasons make sense when you read them |
| 3. DEMO | `AGENT_MODE=DEMO` on a demo account, 4+ weeks | live R per trade and slippage close to the backtest |
| 4. LIVE | `AGENT_MODE=LIVE`, `CONFIRM_LIVE=YES_I_UNDERSTAND`, real account, 0.1-0.25% risk | review weekly with the analytics |

## Evidence workflow

```
python backtest.py --csv XAUUSD_M1.csv --timeframe M1 --offset 2 --events events.csv --save-signals sig.pkl --out results
python optimize.py --csv XAUUSD_M1.csv --timeframe M1 --offset 2 --group entry --folds 5 --out opt
python optimize.py ... --group management     # reuses the cached signals: seconds, not minutes
python optimize.py ... --group sizing --save-models
```

- `backtest.py` stage 1 runs the live analysis, regime, liquidity, microstructure and candidate code on completed candles
  only (a test proves higher-timeframe bars never use future data). Stage 2 applies policy, risk and management with
  spread, slippage, commission and swap. Reports: summary, breakdowns by setup / regime / style / session / score bucket /
  risk tier / news condition / volatility, no-trade reasons, MFE/MAE findings, execution cost by session.
- `optimize.py` tunes **one group at a time**: `entry` (min score, score gap, conflict threshold), `management`
  (break-even trigger, trailing mode, partials), `sizing` (fixed vs tiered). It gives you:
  walk-forward results (parameters chosen on past data, judged on the next unseen window, always compared with untouched
  defaults); a 60/20/20 train / validation / out-of-sample split; parameter stability across folds; **ablation** of the
  regime filter, market-quality gate, confirmation requirement and contradiction check (`keep: false` means switching it off
  did not hurt out-of-sample results, so consider removing it); Monte Carlo of the out-of-sample trades; calibration fitted
  on the training share with an out-of-sample reliability table and Brier score.
- `--save-models` writes `data/calibration.json` (training data only) and `data/edge_book.json` (out-of-sample trades only).
  They are used live only if you set `POLICY__USE_CALIBRATION=true` / `POLICY__USE_EDGE_BOOK=true`.
- Rule of thumb: if a component does not improve out-of-sample results, remove it, however sophisticated it looks.

## Risk model

Gate: weekend, rollover hours, late Friday, unprotected positions, loss-streak pause, spread, open-trade count, trades
per day, daily / weekly loss, drawdown from peak, open-risk cap. Sizing uses MT5's `order_calc_profit`. Approval adds the
new trade's own risk to open risk, checks **correlated portfolio risk** across symbols (`portfolio.py`, correlations from
factor loadings) and margin. Throttle: after 3 straight losses risk halves, after 5 entries pause; the pause comes with a
diagnosis (regime, setup, session, volatility, spread, slippage, events) and only lifts once the regime that produced the
losses has changed. Parameters are never changed automatically.

## Operations

`data/`: `state.json` (+ backups), `trades.jsonl` (signal / requested / filled price, slippage in R, spread, latency,
MFE/MAE, regime, setup, P(win), exit reason, R), `events.jsonl` (lifecycle, regimes, throttle, heartbeats),
`decisions.csv` (every decision with components, regime, market quality, conflict), `agent.log`.
Alerts: Telegram / Discord / email. Kill switches: a `STOP` file blocks entries; a `FLATTEN` file closes everything and halts.

| Situation | What happens | What you do |
|---|---|---|
| Agent crashes | broker-side SL/TP stay in place | restart; positions are rebuilt from MT5 (comment stores style and initial risk) |
| MT5 disconnects | DEGRADED, reconnect attempts | check terminal / network; it recovers by itself |
| Position without stop | restored from records, else HALTED + CRITICAL | set a stop in MT5 at once |
| State file corrupt | quarantined, backup used, entries blocked until reconciled | read the alert and `events.jsonl` |
| "Outcome unknown" order | never blindly resent | check MT5 positions and history first |
| Losing streak | paused with a diagnosis | read it; investigate before changing anything |

## Tests

`python tests/run_tests.py` (V4: 133 tests) and `python tests/run_tests.py --v3-baseline` (frozen V3 suite, 109 tests, run with V4 features off). The tests cover indicators, SMC,
displacement, sessions, liquidity, market quality, regimes, hierarchy, setups, levels, contradictions, policy gates,
calibration, edge book, sizing, portfolio risk, throttle, risk limits, state recovery, reconciliation, events, sources,
intelligence validation and expiry, the MT5 client against a fake MT5, the engine lifecycle and kill switches, the
backtester (no look-ahead, determinism) and the optimizer. Passing tests show the code does what it claims; they do not
show the strategy is profitable.

## Versions

- v1.0 prototype. v2.0 safety rebuild (risk engine, verified execution, reconciliation, crash-safe state, backtester).
- v4.0 see DESIGN_V4.md (M1 refinement, macro context, broker diagnostics, reconnect backoff, news redundancy, decision records + Telegram, learning registry, drift, research speed-ups).
- v3.0 regime engine, setup classifier + edge book, calibrated probabilities, market-quality score, session phases,
  liquidity map, displacement detection, timeframe hierarchy, setup-aware levels, tiered sizing (off by default),
  portfolio risk, event lifecycle, source reliability, expiring LLM intelligence (Anthropic / OpenAI), contradiction
  detection, WAIT/HALT decisions, diagnosing throttle, execution analytics, MFE/MAE, walk-forward optimizer.

## Known limitations

- Not run against a live MT5 terminal or real LLM APIs; the OpenAI path is untested.
- The contradiction check accepts a USD-proxy input (`aux`), but no USD index feed is wired in yet.
- Single traded symbol; portfolio risk counts every agent position with the same magic number on any symbol.
- Session phases are fixed UTC windows; shift them around DST changes if your results show drift.
- The LLM layer cannot be backtested. Tick volume, not real volume, is used for VWAP, displacement and order blocks.

## V4.1 hardening (this build)

- Fail-closed economic-calendar safety: stale/unavailable calendar data blocks new entries by default.
- Calendar resilience: last-known-good cache plus optional independent Trading Economics provider (requires `TRADING_ECONOMICS_API_KEY`). Forex Factory current/next week remain one vendor family and are not counted as independent vendors.
- Strict broker minimum-lot rule: if the minimum tradable volume exceeds the requested risk budget, the candidate is rejected.
- Reproducibility: learning artifacts record code/config/dependency fingerprints in addition to the data fingerprint.
- Block-bootstrap Monte Carlo: preserves short-range clustering rather than assuming every trade is independent.
- Notification delivery: HTTP alert channels verify non-2xx responses and retry with exponential delay.
- Environment baseline: NumPy 1.26.4 and MetaTrader5 5.0.6231 match the validated Windows/MT5 workstation baseline used with V3/V4.

## V4.4.1 Broker Auto-Discovery

The agent no longer assumes that gold is literally named `XAUUSD`. With `SYMBOL=AUTO`, startup inspects the symbols exposed by the connected MT5 terminal, ranks candidates using the actual symbol name plus contract properties (including XAU/USD currencies, description/path and full-trading status), selects the best tradable candidate, and records the resolution. This handles broker suffixes such as `XAUUSDs` without hard-coding every broker.

The account is also detected from the connected MT5 terminal when no `MT5_LOGIN` credentials are supplied. The engine records a non-secret account fingerprint (login/server/company/trade mode/currency/leverage/margin mode/terminal path) and fails closed if the account identity changes during reconnect.

`SYMBOL=<exact name>` remains supported as an intentional pin.

