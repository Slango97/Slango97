# Slango Pro Trader V5.8 — Live Cross-Market Correlation Matrix

Date: 11 October 2026. Roadmap step 3.

## Problems in the previous portfolio model
- Hand-written factor loadings for only 7 symbols: US30, NAS100, USOIL, BTCUSD and USDCAD all used a guessed 0.5
  correlation (US30/NAS100 really move ~0.9 together, so stacking both was under-counted).
- Correlations were looked up by the broker's raw name: `XAUUSDs`, `GOLD`, `EURUSD.a` matched nothing and also
  fell back to 0.5 — even gold vs silver.
- Hedges (opposite positions in correlated markets) were trusted 100%, although correlations change exactly in
  the crises when the hedge is needed.

## Added
- `correlation_matrix.py`: hourly measurement from closed H1 bars of every watchlist market the broker offers;
  log returns aligned on common timestamps; long window (720 bars) + short stress window (120 bars); shrinkage
  toward the factor prior (`n / (n + 100)`); each pair stored as a (low, high) range; atomic JSON shared by all
  market instances; stale after 12 h. Offline mode: `python correlation_matrix.py --history history`.
- `portfolio.py`: broker names mapped to markets first; per pair the end of the range that makes combined risk
  larger is used; offsetting pairs earn only `PORTFOLIO__CORR_HEDGE_CREDIT` (0.5) of their risk reduction; the
  factor prior now covers all 12 markets (equity, oil, crypto factors).
- Engine heartbeat refreshes the matrix (journaled with the strongest pairs); the daily universe report lists the
  correlated clusters ("one bet, sized together"); `RESEARCH_MARKETS_WINDOWS.bat` also builds the matrix.

## Behaviour changes
- With default settings a gold-long / silver-short pair now counts ~0.52% instead of ~0.23% combined risk (half
  hedge credit). `PORTFOLIO__CORR_HEDGE_CREDIT=1.0` restores full trust; `PORTFOLIO__LIVE_CORRELATION=false` uses
  the factor prior only. The correlated-exposure cap (`PORTFOLIO__MAX_CORRELATED_EXPOSURE_PCT`, 1.5%) is unchanged.
- Tests: 283 passed (7 new; the V3 hedge test now states both the half-trust and the full-trust value).
