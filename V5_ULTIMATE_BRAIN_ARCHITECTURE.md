# Expert AI Agent Pro Trader — Ultimate Brain Architecture V5

## Purpose

V5 adds a Python-native specialist-brain and workflow layer on top of the tested V4 deterministic trading kernel. It is designed to replace an external workflow orchestrator such as n8n and to avoid dependency on the OpenAI Agents SDK.

The V5 layer is advisory by default. The deterministic risk, broker validation, execution, reconciliation and emergency controls remain authoritative.

## Specialist brains

1. **Connection & Account Brain** — verifies account identity, server, symbol binding, terminal connectivity, broker diagnosis and data freshness.
2. **Market + Technical Brain** — consolidates multi-timeframe structure, EMA/RSI/MACD/ADX/ATR, liquidity, FVG, order blocks, sweeps, Fibonacci, candlestick patterns and conservative chart-pattern hints.
3. **Quantitative Brain** — evaluates R:R, available calibrated probability/expected R, risk budget context and evidence quality.
4. **News / Macro Brain** — consumes the deterministic NewsEngine, calendar state, event phase, bias, confidence, contradictions and risk state.
5. **Intermarket Brain** — consumes available DXY/US10Y/auxiliary macro context.
6. **Opportunity Brain** — synthesizes a candidate opportunity from the deterministic decision plus specialist findings and explains why it is or is not executable.
7. **Risk Guardian Brain** — can only reduce permission/risk. It cannot increase a deterministic risk budget.
8. **Trade Critic / Chief Trader Brain** — critiques contradictions and creates a bounded final V5 decision.
9. **Telegram Brain** — produces market-health, opportunity, no-trade, trade-entry and trade-closure feeds. Opportunity reports can include generated chart screenshots.

## Workflow

```text
MT5 / News / Account events
            |
            v
       V5 Orchestrator
            |
     specialist brains (parallel)
            |
            v
     Opportunity Brain
            |
            v
        Trade Critic
            |
            v
       Chief Trader
            |
            v
       Risk Guardian
            |
     +------+------+
     |             |
   BLOCK        APPROVE/REDUCE
                   |
                   v
          deterministic risk gate
                   |
                   v
             execution kernel
                   |
                   v
            position manager
                   |
                   v
             journal / memory
                   |
                   v
              Telegram
```

## Safety invariant

The AI layer cannot:

- bypass the deterministic risk gate;
- increase risk above the deterministic authorization;
- send a raw broker order;
- suppress reconciliation failures;
- convert unavailable/stale required data into a trusted signal;
- turn a deterministic WAIT/HALT into a trade.

When `BRAIN__ENFORCE=false`, V5 cannot alter execution decisions at all. When `BRAIN__ENFORCE=true`, it may only veto a deterministic trade candidate.

## Direct LLM integration

`brain/llm.py` is an optional direct model gateway. It uses a normal model API call rather than an agent SDK. The model is prompted as an advisory reasoning layer and its response is schema-validated and restricted to reducing permission/risk.

No API key is required for the default configuration (`BRAIN__LLM_ENABLED=false`, `INTEL__PROVIDER=none`).

## Current scope

V5 is production-oriented engineering, not proof of profitability. Historical edge must still be established using the existing backtest/evidence/walk-forward workflow. Multi-market analysis interfaces are prepared through the V5 watchlist configuration, while the current execution kernel remains centered on the broker-selected primary symbol.
