# V5.3 — Adaptive Account-Aware Market Selection

## Objective
Turn the multi-market watchlist into a genuine self-selection layer for an account, especially micro/small balances, without bypassing the deterministic risk and execution kernel.

## Pipeline
`Market Universe Ranking → Account Affordability → Regime → Confluence → News → Intermarket → Trade Critic → Chief Trader → Risk → Execution`

## Implemented
- Added broker-aware market ranking to `brain/universe.py`.
- Added account-equity-aware affordability using the broker minimum volume and broker-side loss-per-lot calculation.
- Added estimated structural-stop preflight using ATR; final sizing remains authoritative in `RiskEngine`.
- Added regime and confluence ranking plus session suitability.
- Added safe `MT5Client.switch_symbol()` and broker-aware margin calculation helper.
- Added account-level adaptive selection in `V5BrainSystem`.
- Added engine integration before entry analysis; symbol-local views, raw frames, caches, regime, profile and news context are rebuilt after a switch.
- Adaptive switching is flat-only and disabled for fixed-symbol multi-market launcher instances by default.
- Added Telegram selector reporting.
- Added selector and micro-account tests.
- Preserved moderate-veto behavior: soft disagreement reduces risk; genuine safety/risk failures still veto.

## Account behavior
- Equity <= $25: 1.0% ceiling.
- Equity <= $500: 0.75% ceiling.
- Above $500: configured base risk (0.5% default).
- A market whose broker minimum lot would exceed the applicable ceiling is not eligible.

## Operational safety
- No symbol switching with agent positions or pending orders.
- The selector never places orders.
- The selected market still passes all downstream deterministic and AI-brain gates.
- Fixed symbol remains supported.

## Validation
Targeted V5/V5.2/adaptive and regression suites pass after updating superseded tests for the V5.2 moderate-confirmation policy. The long aggregate dependency-free runner can exceed the execution environment timeout because several historical/news tests are intentionally slow; individual suites were run successfully.
