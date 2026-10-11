# Expert AI Agent Pro Trader Brain — V5.3 Adaptive Selection Architecture

```text
                         EVENT BUS
              MARKET / NEWS / ACCOUNT EVENTS
                              │
                              ▼
                       ORCHESTRATOR
                              │
              ┌───────────────┴───────────────┐
              │                               │
              ▼                               ▼
      MARKET UNIVERSE RADAR             ACCOUNT STATE
              │                               │
              └───────────────┬───────────────┘
                              ▼
                    ACCOUNT-AWARE FUNNEL
                              │
             1. MARKET UNIVERSE RANKING
                              │
             2. ACCOUNT AFFORDABILITY
                              │
             3. REGIME SUITABILITY
                              │
             4. CONFLUENCE / SETUP QUALITY
                              │
                              ▼
                    BEST ELIGIBLE MARKET
                              │
                    safe switch while flat
                              │
                              ▼
                  FRESH SYMBOL-LOCAL STATE
                              │
             ┌────────────────┼────────────────┐
             ▼                ▼                ▼
         TECHNICAL          NEWS          INTERMARKET
             └────────────────┼────────────────┘
                              ▼
                     OPPORTUNITY BRAIN
                              ▼
                       TRADE CRITIC
                              ▼
                      CHIEF TRADER
                              ▼
                       RISK BRAIN
                              │
                    deterministic RiskEngine
                              │
                     REJECT / APPROVE
                              │
                              ▼
                         EXECUTION
                              ▼
                     POSITION MANAGER
                              ▼
                      JOURNAL / MEMORY
                              │
                              ▼
                           TELEGRAM
```

## Source-aligned design
- The Professional Trading Blueprint calls for a limited 2-5 asset watchlist, HTF structural preparation, economic-calendar audit, DXY/US10Y intermarket context and exact pre-execution position sizing.
- The Ultimate Pro Trader Strategy emphasizes structure/trend, supply-demand, liquidity sweeps, Fibonacci confluence, divergence, FVG/imbalance, Asian-range/London liquidity behavior, Breaker/Mitigation and AMD.
- News is a downstream decision input, not a reason to select an asset merely because it has a headline. High-impact event restrictions remain deterministic. Medium-impact news is monitored rather than automatically vetoing every setup.
- The account-affordability gate is deliberately earlier than expensive full analysis: there is no value in spending the full decision pipeline on a market that cannot be traded within the account's minimum-volume risk budget.

## Small-account rule
The selector may prefer a lower-priced / lower-contract-risk market over a technically more attractive market if the technically attractive market cannot be traded within the account risk ceiling. This is **account-aware opportunity selection**, not a reduction of the risk ceiling.

## Hard boundaries
- No forced minimum lot.
- No order creation by the selector.
- No symbol switching with open agent positions/pending orders.
- No AI/LLM permission to increase deterministic risk.
- Final execution remains broker-validated and deterministic.
