# Expert AI Agent Pro Trader — V5.2 Ultimate Brain Architecture

## Objective

V5.2 is the adaptive, multi-market evolution of V5.1. It combines the deterministic MT5 trading kernel with the two supplied trading documents while keeping the execution/risk layer authoritative.

The design target is a **10/10 engineering architecture**, not a promise of 10/10 trading returns. Profitability must be established by broker-specific out-of-sample testing, walk-forward validation, Monte-Carlo analysis, shadow trading and DEMO validation.

### Source principles incorporated

- Professional workflow: pre-market calendar audit, intermarket context, HTF levels, limited watchlist, If-Then scenarios, strict execution, daily drawdown stop, journaling and review.
- Intermarket anchors: DXY, US10Y and risk-on/risk-off context.
- Structural trading: HTF trend, supply/demand, liquidity pools, sweeps/fakeouts, Fibonacci confluence, RSI/MACD divergence, FVG and AMD.
- Execution: structural invalidation stop, risk-based sizing, TP1/TP2/optional TP3, partial close, break-even after TP1 and structural trailing.
- News: impact filtering, currency relevance, Forecast-vs-Actual reasoning, high-impact blackout/absorption, medium-impact monitoring.
- Psychology: loss cooldown, two-loss daily lock and system-over-ego behavior.

The supplied Blueprint explicitly calls for HTF chart refinement and 2–5 liquid assets, DXY/US10Y context and pre-written scenarios.  
The longer strategy document adds the detailed news workflow, confluence methods, FVG/Asian-range/London Judas/Block Breaker/AMD playbooks and tiered trade management.

## 1. Master architecture

```text
                         EVENT BUS
       ┌────────────────────┼────────────────────┐
       │                    │                    │
   MARKET EVENTS        NEWS EVENTS         ACCOUNT EVENTS
       │                    │                    │
       └────────────────────┼────────────────────┘
                            ▼
                     ORCHESTRATOR
                            │
                  REGIME / MARKET MAP
                            │
                    PARALLEL BRAIN RUN
        ┌──────────┬────────┼─────────┬───────────┐
        ▼          ▼        ▼         ▼           ▼
   Technical     Quant     News    Intermarket  Structure
        │          │        │         │           │
        └──────────┴────────┼─────────┴───────────┘
                            ▼
                  OPPORTUNITY / CONFLUENCE
                            ▼
                    TRADE CRITIC / DEBATE
                            ▼
                      CHIEF TRADER
                            ▼
                     RISK GUARDIAN
                            ▼
                 DETERMINISTIC RISK GATE
                            ▼
                 BROKER / EXECUTION CHECK
                            ▼
                       EXECUTION
                            ▼
                    POSITION MANAGER
                 ┌──────────┼──────────┐
                 ▼          ▼          ▼
                TP1        BE        TP2/TP3
                 └──────────┼──────────┘
                            ▼
                  JOURNAL / MEMORY / QA
                            ▼
                         TELEGRAM
```

The architecture preserves the original V5.1 brain sequence and strengthens the boundary between probabilistic reasoning and deterministic execution.

## 2. Decision philosophy: moderate vetoes

### Hard vetoes — only genuine safety failures

A trade is blocked immediately for:

1. Stale/invalid required market data.
2. Broker/MT5 connection or reconciliation failure.
3. Existing unprotected position.
4. Daily/weekly/max-drawdown risk limit.
5. Two-loss account-wide lock.
6. High-impact event blackout/initial-reaction window.
7. Extreme market-quality failure.
8. Invalid structural stop, target geometry or impossible R:R.
9. Minimum broker volume would exceed the permitted monetary risk.
10. Insufficient margin / dangerous projected margin level.
11. Execution-time price drift, spread or slippage violation.

### Soft vetoes — become evidence penalties and/or smaller risk

The following do **not** automatically kill a candidate:

- one missing lower-timeframe confirmation when a valid trigger exists;
- moderate intermarket disagreement;
- borderline pro-quality score;
- medium-impact news;
- moderate news disagreement;
- modest session disadvantage;
- cautious/high-volatility regimes.

A candidate may survive these conditions only if the remaining confluence and risk geometry are strong enough.

### Hard/soft boundary

- Confirmation + trigger both missing = hard no-trade.
- One missing = risk multiplier 0.75 and evidence penalty.
- Conflict >= 0.65 = hard block.
- Conflict 0.30–0.65 = risk multiplier 0.70.
- Pro-quality below 48 or fewer than 2 strong evidence domains = hard block.
- Borderline pro-quality = risk multiplier 0.75.
- Strong news opposition (confidence >= 0.75 and opposition >= 0.75) = hard block.
- Weaker news opposition = risk multiplier 0.65.

## 3. Confluence score

The deterministic technical score remains 100 points:

| Domain | Weight |
|---|---:|
| MTF hierarchy | 25 |
| Structure | 10 |
| Supply/demand + FVG zone | 20 |
| Location / Fibonacci | 10 |
| Liquidity / sweep | 10 |
| Momentum / RSI/Stochastic/MACD | 10 |
| Room to target | 5 |
| Volatility | 5 |
| Session | 5 |
| **Total** | **100** |

News can add only a bounded bonus. It cannot manufacture a technical setup.

Default final score remains **65/100**. The opposing-direction gap has been reduced from 15 to **8 points**, allowing a strong candidate to survive normal market ambiguity without permitting near-ties to trade freely.

## 4. Regime-aware playbooks

The agent identifies the market regime before selecting a setup:

- STRONG_TREND
- WEAK_TREND
- RANGE
- COMPRESSION
- BREAKOUT
- REVERSAL
- EXPANSION
- NEWS_DRIVEN
- ABNORMAL
- EXTREME

Each regime enables only compatible setup families. The agent never forces a trend strategy into a range or a mean-reversion setup into a strong expansion.

## 5. Strategy fusion

The candidate generator combines:

1. HTF market structure / trend.
2. Supply and demand / order blocks.
3. Liquidity pools and sweeps/fakeouts.
4. Fibonacci location/confluence.
5. RSI divergence and momentum exhaustion.
6. FVG / imbalance retest.
7. Asian range / London Judas behavior where the instrument/session supports it.
8. Breaker/mitigation logic where structural evidence supports it.
9. AMD: accumulation → manipulation → distribution.
10. Price action and displacement.

These are evidence sources, not independent permission switches. Confluence increases quality; lack of one optional component does not automatically veto a trade.

## 6. News brain

### Impact levels

- **High / Red:** hard entry blackout from the configured pre-event window through the initial reaction; then absorption/re-evaluation.
- **Medium / Orange:** monitored and reported; does not automatically block trading.
- **Low / Yellow:** excluded from trading decisions unless another system explicitly requires it.
- **Holiday / non-economic:** retained for liquidity/session awareness.

The implementation follows the documents' key behavior: do not chase the first post-news impulse; wait for stabilization, absorption and technical confirmation.

### News reasoning

For each relevant event:

1. identify affected currency/economy;
2. compare Actual vs Forecast when available;
3. account for indicator-specific polarity (e.g. unemployment behaves differently from CPI);
4. translate the event into the traded instrument's expected direction;
5. compare against DXY/US10Y/risk-on-off context;
6. classify reaction as continuation or liquidity sweep/reversal;
7. require price-action confirmation before execution.

### Source reliability

Official sources are preferred for primary facts. Secondary sources may corroborate but cannot independently create a high-impact emergency veto unless reliability rules are satisfied.

## 7. Intermarket brain

The system watches the relationships relevant to the current instrument rather than blindly applying a gold-only model.

Examples:

- Gold: DXY, US10Y, risk sentiment and gold-specific drivers.
- FX: base/quote currency events plus USD/yield context where relevant.
- Indices: USD/yields/risk-on-off and domestic currency.
- Oil: USD, risk sentiment and energy-sensitive event context.
- Crypto: USD/yields/risk sentiment with weekend awareness.

The intermarket brain is confirmation/contradiction evidence, not a universal hard veto.

## 8. Trade Critic

Before Chief Trader approval, the critic attempts to disprove the candidate:

- HTF contradiction;
- insufficient trigger;
- TP1 too close;
- high-impact event phase;
- intermarket opposition;
- news/technical disagreement;
- weak structural confirmation.

Severity policy:

- fatal → block;
- two major objections → block;
- one major objection → reduce risk;
- minor objections → retain as warnings.

## 9. Chief Trader

Chief Trader synthesizes deterministic evidence. It cannot create a trade from a deterministic WAIT/HALT.

The Chief can:

- APPROVE;
- REDUCE;
- NO_TRADE;
- BLOCK.

The LLM, if enabled, remains advisory and can only reduce permission/risk. It cannot increase risk, override deterministic safety, or send an MT5 order.

## 10. Risk architecture: $5 to $100,000+

### Account-aware percentage ceiling

V5.2 introduces an account-aware ceiling:

- Equity <= $25: max 1.00% requested risk.
- Equity <= $500: max 0.75% requested risk.
- Equity > $500: base 0.50% requested risk.

These are ceilings, not forced risk levels. Strategy/style/regime/news/loss-streak multipliers can reduce them further.

### Critical small-account rule

A $5 account is **supported by architecture**, but the system must never fake affordability.

If a broker's minimum volume (for example 0.01 lot) would lose more than the account's allowed risk at the structural stop, the trade is rejected. The agent may then scan another compatible market. A $5 standard account cannot mathematically trade every instrument safely.

This is intentional: **small-account compatibility means risk-aware eligibility, not forced gambling.**

### Universal position sizing

For each symbol the broker's actual contract/tick economics are authoritative:

`risk_money = equity × permitted_risk_pct / 100`

`loss_per_lot = MT5 profit/loss calculation for entry → structural SL`

`lots = floor(risk_money / loss_per_lot to broker volume step)`

The result must satisfy volume_min, volume_step, volume_max, margin and portfolio exposure.

## 11. Portfolio heat

Multiple symbols can be one macro bet. The portfolio layer therefore controls correlated exposure using instrument factor loadings.

Example: simultaneous long EURUSD + GBPUSD + XAUUSD can represent a large USD-short exposure. The agent measures portfolio heat before approving another position.

## 12. Trade management

### TP1

Nearest structural liquidity/target with at least the configured minimum R:R; otherwise the default structural target is used.

### After TP1

- close 50% where broker volume rules permit;
- move remaining stop to entry + transaction costs/spread;
- record realized R and remaining risk.

### TP2

Higher-timeframe structural target, typically around 2R–3R where market geometry permits.

### TP3 / runner

Optional trailing portion for strong trend/news-driven expansion. Trailing uses structure/ATR/EMA rules according to style.

## 13. Loss protection

- every loss → 15-minute cooldown;
- two consecutive full stop-outs → account-wide lock for the rest of the UTC day;
- daily loss cap → stop new entries;
- weekly loss cap → stop new entries;
- maximum drawdown → halt and require review;
- loss streak → reduce risk before eventual pause.

These protections are shared across market instances so five running market engines cannot bypass the account-wide controls.

## 14. Multi-market operation

The market universe remains a radar, not an uncontrolled order farm.

Default watchlist contains liquid candidates such as:

XAUUSD, EURUSD, GBPUSD, USDJPY, XAGUSD, US30, NAS100, USOIL, BTCUSD.

The Blueprint recommends concentrating on 2–5 assets. V5.2 therefore ranks the available universe and uses account/risk compatibility, data quality, technical quality and freshness to determine which markets deserve attention.

A single account-wide risk ledger controls all instances.

## 15. Execution safety

Immediately before sending an order the kernel rechecks:

- fresh tick;
- server time drift;
- market quality;
- regime;
- news blackout;
- price drift;
- stop geometry;
- spread/stop cost;
- current R:R;
- broker order_check;
- risk budget;
- correlated exposure;
- margin.

After execution it verifies the position and stop. A position that cannot be protected is closed/escalated.

## 16. Journal / learning

Every candidate records:

- symbol, direction, setup, style;
- score and component scores;
- news state and impact;
- intermarket state;
- regime;
- pro-quality score;
- critic objections;
- requested/actual risk;
- lot size and broker economics;
- entry/SL/TP1/TP2;
- execution quality;
- outcome in money and R;
- whether the loss was statistical or a rule violation.

Learning remains evidence-gated. No parameter is promoted into live trading merely because it looked good in-sample.

## 17. Promotion ladder

1. Static/syntax checks.
2. Unit/integration tests.
3. Synthetic MT5 tests.
4. Historical backtest.
5. Walk-forward / purged validation.
6. Monte-Carlo / block bootstrap.
7. Shadow mode.
8. DRY_RUN with real terminal data.
9. DEMO.
10. LIVE only after explicit confirmation and evidence review.

## 18. Engineering scorecard

| Area | Target |
|---|---:|
| Architecture / separation of concerns | 10/10 |
| Risk protection | 10/10 |
| Multi-market abstraction | 10/10 |
| News lifecycle / impact filtering | 10/10 |
| Moderate veto logic | 10/10 |
| Execution hardening | 10/10 |
| Trade management | 10/10 |
| Auditability / journaling | 10/10 |
| Testing discipline | 10/10 target |
| Proven live profitability | **Not claimed** |

The last distinction is deliberate: software quality can be engineered and tested; future market returns cannot be guaranteed.
