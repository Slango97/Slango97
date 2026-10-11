# Slango Pro Trader V5.7 — Drawdown & Volatility Exposure Scaling

Date: 11 October 2026. Roadmap step 2 (Strategies Summary: "drawdown-aware leverage", Bridgewater/AQR
volatility targeting: "volatility changes... allocations must change too").

## Added
- `risk.exposure_scale()` — one pure function used by BOTH `RiskEngine.approve` (live) and `Backtester.simulate`:
  - **Drawdown:** x1.0 up to 2% below the equity peak, linear to x0.35 at 6% (the 8% hard halt is unchanged);
    recovers automatically as equity recovers.
  - **Market volatility:** M15/style-timeframe ATR above 1.3x its recent average -> x(1.3 / ratio), floor x0.5.
  - **Account volatility targeting:** realised annualised volatility of the account's daily equity (one point per
    day, persisted in the state file as `day_equities`) above 12%/yr -> x(12 / realised), floor x0.5,
    after 10 days of history.
  - Factors multiply, the product is floored at x0.25 and capped at x1.0 — scaling can never raise risk.
- Approval details record `exposure_scale` and each factor; heartbeat journals them.
- Telegram: "RISK SCALED DOWN" / "RISK BACK TO FULL SIZE" only when the multiplier changes 10% band.
- Backtester records the equity fraction actually risked per trade (`risk_frac`); `market_research.py` judges the
  drawdown/Sharpe of the sizing actually used and reports the same OOS trades with vs without scaling.

## Configuration (all `RISK__...`)
`DD_SCALING`, `DD_SCALE_START_PCT` (2), `DD_SCALE_FULL_PCT` (6), `DD_SCALE_FLOOR` (0.35), `VOL_SCALING`,
`VOL_SCALE_ATR_RATIO` (1.3), `VOL_SCALE_FLOOR` (0.5), `EQUITY_VOL_TARGET_PCT` (12), `EQUITY_VOL_MIN_DAYS` (10),
`EQUITY_VOL_FLOOR` (0.5), `EXPOSURE_SCALE_FLOOR` (0.25). Set `RISK__DD_SCALING=false` / `RISK__VOL_SCALING=false`
to reproduce V5.6 sizing.

## Notes
- Market-volatility scaling is continuous and stacks with the existing categorical HIGH/EXTREME-volatility
  multiplier (x0.7) in the decision layer; both only reduce risk.
- Signal caches written before V5.7 lack `atr_ratio`; those runs apply drawdown/account scaling only. Delete
  `research/markets/cache` to regenerate.
- Tests: 276 passed (8 new). Effect on real markets is measured by `market_research.py` on broker data.
