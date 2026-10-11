# Slango Pro Trader V5.9 — Evidence-Gated AI Influence

Date: 11 October 2026. Roadmap step 4: measure whether the AI layers help before giving them influence.

## Problems found
- AI news opinions were never stored: nothing could ever prove or disprove them.
- An unproven LLM view could ADD up to `STRATEGY__NEWS_MAX_BONUS` score to a setup (raising its tier/size) - the
  only place where AI could increase, not just reduce, exposure.
- Decision records rounded candidate entry/SL/TP to 2 decimals (useless for FX evaluation).

## Added
- **Opinion log** (`INTEL__LOG_FILE`, data/intel_log.jsonl): id, time, instrument, provider, direction, confidence,
  risk level, veto, validity of every LLM view. Decision records carry `intel_id`, `llm_bias`, `llm_influence`.
- **`intel_evaluation.py`** scores, once each and only after the outcome is known:
  - every view: price move over 60/240 min (capped at its expiry) in ATR units; moves < 0.25 ATR = no call;
    hit rate with an exact one-sided binomial test vs 50%; high-confidence subset; volatility in veto windows;
  - every candidate blocked by an INTEL veto: counterfactual R (first touch, SL wins ties, 48 h mark-to-market);
  - every V5-brain BLOCK/NO_TRADE of a deterministic trade: counterfactual R -> VALUABLE / COSTLY / NEUTRAL /
    UNPROVEN and an explicit `BRAIN__ENFORCE` recommendation.
  Outcomes accumulate in data/intel_outcomes.jsonl; verdicts in data/intel_evidence.json; markdown report offline.
- **Evidence gate** (`INTEL__EVIDENCE_GATE`, default on): until the news-AI verdict is a fresh PROVEN (>= 50
  decided calls, p < 0.05, positive mean move, vetoes not shown to block winners), AI views keep every
  risk-reducing power (strong-opposition veto, soft reduction, trade_veto) but cannot add score. HARMFUL verdicts
  (worse than a coin, or vetoes that significantly block winners) are flagged with a recommendation to disable.
- Engine runs the evaluation daily on live MT5 prices; Telegram "AI EVIDENCE UPDATE" only when a verdict changes;
  `RESEARCH_MARKETS_WINDOWS.bat` runs it on exported history.

## Behaviour change
With the LLM enabled (`FEATURES__USE_LLM=true`), news support no longer adds score until proven. Event-surprise
(actual vs forecast) support is unaffected. `INTEL__EVIDENCE_GATE=false` restores V5.8 behaviour.
Tests: 293 passed (10 new).
