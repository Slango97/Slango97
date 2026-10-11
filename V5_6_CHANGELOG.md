# Slango Pro Trader V5.6 — Per-Market Walk-Forward Evidence

Date: 11 October 2026. Roadmap step 1 of the V5.5 audit: prove each market out-of-sample before trading it.

## Added
- **`market_research.py`** — for every watchlist market: the live signal/policy/risk/management code through
  anchored walk-forward folds, then the DEFAULT configuration's out-of-sample trades are judged against the
  Strategies Summary criteria: OOS trades >= `LEARNING__MIN_OOS_TRADES` (100), profit factor >= 1.3, annualised
  Sharpe >= 0.8 (daily, 365-day year for weekend markets), max drawdown < 15% at the configured risk per trade,
  stress PF >= 1.0 with spread x1.5 and slippage x2, block-bootstrap Monte Carlo P(loss) <= 0.2. Verdict per market:
  PASS / FAIL / INSUFFICIENT / NO_DATA / ERROR, with every failed criterion listed. Writes
  `research/markets/MARKET_RESEARCH_REPORT.md`, `market_research.json`, OOS trade CSVs and cached signals.
  `--publish` writes `data/market_evidence.json`; synthetic runs are labelled and can never be published.
- **`market_specs.py`** — point, contract size, typical spread/slippage (price units), commission and weekend rule
  per market, overridden by the broker's export metadata. Commission is converted into the profit currency for
  USD-base pairs (USDJPY/USDCAD/USDCHF).
- **`export_history.py --markets WATCHLIST [--tf M5]`** — one folder per market with the broker's CSV and
  `export_meta.json` (point, digits, contract size, profit currency, median spread, server offset).
- **`market_evidence.py` + `BRAIN__REQUIRE_MARKET_EVIDENCE`** (default off) — when on, the adaptive selector never
  selects, and the engine never enters, a market without a fresh real-data PASS (missing, synthetic, failed or
  older than `LEARNING__MARKET_EVIDENCE_MAX_AGE_DAYS` all fail closed). The selector Telegram report shows the
  evidence status per market.
- **`RESEARCH_MARKETS_WINDOWS.bat`** — export + research + publish in one step on the MT5 workstation.

## Fixed (research kernel was gold-only)
- The default cost model charged $0.02 slippage per fill: 200 pips on EURUSD. Each market now uses its own
  price-unit slippage.
- The backtester's fixed 40-point spread cap rejected every BTC/index bar and depressed their market-quality score;
  it now mirrors the live ATR-based cap for index/energy/crypto (`BTConfig.spread_cap_points`) and the weekend rule
  of the instrument profile (`BTConfig.trades_weekends`). Gold behaviour is unchanged (defaults None/False).

## Honest status
No market has been judged on real data yet: this build environment blocks every market-data host (Dukascopy,
Yahoo, Stooq, HistData) and has no MT5 terminal. The pipeline was validated end-to-end on synthetic data
(plumbing only). Real verdicts come from running `RESEARCH_MARKETS_WINDOWS.bat` against your broker.
