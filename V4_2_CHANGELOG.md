# V4.2 - fixes for the V4.1 review + evidence, learning, execution and operations upgrades

V4.1 is preserved as the comparison baseline; the frozen V3 suite still runs with `--v3-baseline`.
Every new trading behaviour is OFF by default (opt-in flags) until evidence on your data supports it.

## Fixes for defects confirmed in the V4.1 review (each has a regression test in tests/test_v42.py)
1. Trading Economics timestamps without an offset are treated as UTC (V4.1 used the PC's local timezone: on a
   Beirut-time PC CPI blackouts were shifted 3 hours early).
2. Secrets are redacted from every log line, health record and alert error (API keys in query strings, Telegram bot
   tokens in URL paths, bearer tokens, and the value of any *_KEY / *_TOKEN / *_SECRET / *_PASSWORD variable).
3. Calendar redundancy works as redundancy: fresh data from ANY vendor is used; a second vendor is a cross-check
   (coverage CROSS_CHECKED vs SINGLE_VENDOR, disagreements reported); the cache is used only when no vendor works and is
   merged with any partial fresh data. Strict all-vendors mode is opt-in. Failing providers back off (60 s .. 1 h) and
   successful ones refresh hourly (ForexFactory rate-limits frequent polling).
4. With FEATURES__NEWS_REDUNDANCY=false the coverage rule no longer blocks trading permanently.
5. Vendor event titles map to the right classes (Inflation Rate -> CPI, Non Farm Payrolls -> NFP, FOMC member speeches ->
   speech, not rate decision); events from different vendors de-duplicate by class + time.
6. Registry metadata (including gates_passed) is hash-verified before any use or promotion; optional HMAC signing with
   REGISTRY_HMAC_KEY. A tampered manifest can no longer promote a failed candidate.
Minor: alerts no longer retry 4xx errors and honour Telegram retry_after; the plain test runner supports pytest's
tmp_path; RELEASE_MANIFEST.sha256 (+ release_manifest.py verify) covers every shipped file and replaces
V4_1_FILE_HASHES.txt (which covered only 3 documents).

## New capabilities
- **Evidence**: export_history.py (years of M1 + spread + auxiliary symbols from your MT5), evidence.py (one command:
  broker diagnosis, backtest, walk-forward, ablation, block Monte Carlo, cost stress, meta-label CV -> EVIDENCE_REPORT.md
  with a pass/fail readiness checklist), cost-model calibration from your own fills.
- **Meta-labelling** (metalabel.py): triple-barrier labels for every candidate, dependency-free regularised logistic
  regression, purged + embargoed time-series CV, gates (OOS AUC, Brier vs base rate, filtered expectancy gain, sample
  size), registry kind meta_model, policy gate POLICY__USE_META_MODEL (off by default), meta_p in decision records.
- **Gold drivers** (macro_feeds.py): dollar index / US10Y / EURUSD from broker symbols when offered, CSV feeds you maintain
  (GLD holdings, GVZ); used as contradiction evidence, never as votes. FEATURES__MACRO_FEEDS (off by default),
  backtest/evidence --aux to measure them first.
- **Execution**: limit entries at the zone midpoint when they beat the market price (FEATURES__LIMIT_ENTRIES, off by
  default) with expiry, stop-level invalidation, blackout/halt cancellation, fill registration, conservative backtest
  fill model; reconnect reconciliation accepts the agent's own tracked pending orders only.
- **Operations**: shadow mode (FEATURES__SHADOW_MODE + SHADOW__PARAMS_VERSION / SHADOW__META_VERSION; candidates decide on
  the same market state, are recorded, never trade; python shadow.py report), authenticated Telegram commands
  (FEATURES__TELEGRAM_COMMANDS; chat + user allow-list, PIN, two-step flatten, lockout).
- **Sessions**: SESSIONS__DST_AWARE=true makes London/New York phases follow UK/US daylight saving (rule-based, no tzdata).
- **Research speed**: vectorised swing detection 1.765 -> 0.119 ms (14.8x); backtest stage 1 10.48 ms per decision point (V4.0: 14.3, V3: 32.0); parallel optimizer trials (--workers).

## Validation in the build environment (synthetic data, 1 CPU)
- tests/run_tests.py: **162 passed, 0 failed** in 74 s (140 V4.1 + 22 V4.2)
- tests/run_tests.py --v3-baseline: **109 passed, 0 failed**
- Not possible here: real XAUUSD history, live MT5, Telegram, Trading Economics, OpenAI/compatible providers.
