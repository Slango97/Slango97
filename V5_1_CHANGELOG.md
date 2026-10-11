# V5.1 - the trading documents implemented, key-free official news, every market

Built on V5.0 (brain layer, auto-detection, reliability fixes kept). Validated in the build environment:
`tests/run_tests.py` 233 passed / 0 failed; frozen V3 baseline 109 passed / 0 failed. Synthetic data only;
real-terminal behaviour must be confirmed in DRY_RUN.

## 1. Restored V4.4.1 fixes that V5.0 lost (V5.0 was built on V4.4)
Backtest crash after a rejected candidate (evidence.py would have died on real data); the LLM switch
(`FEATURES__USE_LLM` + `INTEL__PROVIDER`); the explicit single-vendor calendar policy; unconnected settings
removed or wired (guard test now scans every package, including brain/); documented V3 supersession register.

## 2. News: official sources, no API keys
Calendar: ForexFactory this-week (impact-rated, every currency); **BLS official release schedule**
(bls.gov, Eastern Time converted to UTC with daylight saving) as an independent cross-check for CPI, the jobs
report, PPI and JOLTS; manual file. Trading Economics removed from the defaults (needs a key; still supported).
ForexFactory's next-week file returning 404 is "not published yet", not a failure; a missing key is
"not configured". An empty BLS parse counts as a failure (page-format change), never as coverage.
Headlines: Federal Reserve press releases, Federal Reserve speeches & testimony, BLS latest numbers, ECB press
releases, CNBC, BBC Business - fetched in parallel. FXStreet removed (SSL failures in the V5 live run).

## 3. Impact filter exactly as the documents describe
Red (High): no new entries from 60 minutes before (strategy checklist "no red news in the next hour";
Blueprint minimum 30) until 15 minutes after the release (initial reaction / manipulation), then ABSORPTION
until volatility settles, then POST_EVENT re-evaluation. Orange (Medium): kept, reported, trading continues
("continue normally with technical discipline" - it includes central-bank speeches). Yellow (Low) and holidays:
dropped. Phase labels: UPCOMING / PRE_EVENT / INITIAL_REACTION / ABSORPTION / POST_EVENT.
Actual vs Forecast is "good for the currency" (inverted for unemployment-type data, fixing a V3-V5 gold-sign
error on the unemployment rate) and is converted to the traded instrument.

## 4. Every market
`instruments.py` profiles gold, silver, FX pairs, indices, oil and crypto: which currencies' calendar matters,
and the instrument's reaction to the dollar, US yields and risk-on/off. The calendar filter, event direction,
headline reasoning (now including ECB/BoE/BoJ/RBA/BoC/SNB/RBNZ policy headlines) and DXY/US10Y feeds all use it.
Index/oil/crypto spread caps scale with ATR; crypto may trade weekends. `START_MULTI_V5_WINDOWS.bat` runs one
engine per market (max 5, Blueprint watchlist), sharing exposure and the account-wide loss rules.

## 5. The documents' trading rules (FEATURES__DOC_RULES, on by default; off reproduces V5.0)
- Two consecutive full stop-outs: trading locked for the rest of the UTC day; every loss: 15-minute cooldown;
  account-wide across market instances; identical in the backtester.
- TP1 at the nearest structural level (at least 1:1, else 1.5R): close 50%, stop to entry + spread; TP2 / trail.
- RSI divergence, RSI reclaiming 30 / losing 70 and slow-Stochastic exhaustion as confirmation evidence.
- The Blueprint pre-flight checklist (9 items, PASS/FAIL/INFO) in every entry's decision record.
- Trade Critic brain attacks each candidate before the Chief (fatal -> block, 2 major -> block, 1 major ->
  reduce); the Chief uses your configured minimum score instead of a hard-coded 65.
- Phase A daily pre-market If-Then plan (calendar windows, HTF structure, levels, scenarios) and Phase C
  Saturday weekly review (win rate, expectancy, best/worst setups, statistical vs execution losses).

## 6. From the V5 live log
EXTREME volatility now needs a genuine shock (ATR at least 2x its recent mean, or a new long-history extreme);
the old rule kept a 60% volatility rise "EXTREME" for about 11 hours, standing the agent down all afternoon.
Heartbeat CPU is the agent's own process. VERIFY_V5_WINDOWS.bat now checks release integrity, validates your
.env (the old check ignored validation errors) and runs the full suite.

## Changed test expectations (documented)
Event-lifecycle test pinned to V4 class windows (doc windows tested in test_v51); full-stack break-even test
moves price past TP1; V4 loss-streak test runs with doc rules off; universe test sets 9 symbols (default 5);
two-vendor tests add Trading Economics explicitly (opt-in now).
