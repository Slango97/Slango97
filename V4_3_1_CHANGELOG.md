# V4.3.1 - fixes for the V4.3 review + News Brain ideas ported

Built on V4.3. Facts were checked against MetaTrader 5 documentation before changing behaviour ("Bars in the platform
are formed based on Bid prices", MT5 Chart Settings help).

## Defects fixed (each with a regression test in tests/test_v431.py)
1. **Clock-drift false blocks.** V4.3 measured the age of the last tick as "server time drift" (a 20 s quiet market read
   as 20 s drift and blocked entries / post-reconnect checks). Drift is now sampled only from ticks seen ARRIVING between
   polls (median of the last 31); unknown drift is 0 and tick staleness keeps its own 45 s gate.
2. **Price basis.** V4.3 treated MT5 bars as mid prices while trade management still used bid prices. One explicit basis
   now drives every backtest price (`BTConfig.price_basis`, default "bid" per MT5 docs; "mid" selectable).
3. **Limit fills one spread too easy.** V4.3 filled BUY limits on the bid and SELL limits on the ask. Now the executable
   side (ask for buys, bid for sells) must trade through.
4. **Break-even regret statistic.** V4.3 compared stops with the wrong bar extremes (long: high, short: low) and overstated
   "break-even was too early". Fixed.
5. **Unnecessary flattening of filled limits.** Fills are re-authorised against HARD conditions only (news blackout, price
   beyond stop/target, daily/weekly/drawdown/loss-streak/unprotected/open-risk limits, size > 125% of authorised).
   Soft drift (0.01-lot rounding after a small equity move, the trade counter, clock windows) keeps the protected
   position and sends a warning. *Intentional change*: tests/test_v43.py now expects soft blocks to keep the fill and a
   news blackout to close it.
6. **Limits resting into rollover.** Limit expiry is cut at the next rollover hour / Friday cutoff; no limit is placed
   with under 2 minutes of valid window.
7. **Backtest/live parity for limit + M1** (open since V4.2). The backtester now arms, waits for M1 confirmation and only
   then places the limit, exactly like the live engine.
8. **DRY_RUN limit simulation** (open since V4.2). DRY_RUN records the limit it would place and simulates fill / expiry /
   invalidation from live ticks without sending anything.
9. **Release hygiene.** V4.3 shipped a test-generated `data/calendar_cache.json` (a fake CPI event). The test runner now
   removes any runtime files tests create, and `release_manifest.py` refuses to build or verify a release with data files.
10. `order_check` success accepts 0 ("Done", the documented check result) or DONE; anything else fails closed.

## Ported from the News Brain hotfix (and improved)
`news_rules.py`: deterministic headline reasoning with one vote per causal family (inflation, labor, jobless, Fed policy,
yields, USD, geopolitics, central-bank gold), negation handling ("not expected to hike"), reversals ("slowed less than
expected"), clause splitting for mixed headlines (conflicts reported), and price-echo filtering ("gold surged" is not a
driver). Headline-only confidence is capped at 0.45 and it is **caution-only**: it can raise the contradiction index
against a trade, never add confidence or a bonus, and can never veto alone. The four headline probes the News Brain
misread are now read correctly (tested).

## Research
Gate funnel in backtest and evidence reports: for every filter, how often it fires, how often it is the SOLE blocker,
and what those blocked trades would have made - the direct measurement of over-filtering.

## Validation in the build environment (synthetic data, 1 CPU)
- `python tests/run_tests.py`: 186 passed, 0 failed (173 V4.3 + 13 V4.3.1)
- `python tests/run_tests.py --v3-baseline`: 109 passed, 0 failed
- Not possible here: real XAUUSD history, a live MT5 terminal, Telegram, Trading Economics, LLM providers.
