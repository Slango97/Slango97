"""Central configuration. Every tunable lives here so it can be optimized and audited.

Override any field with an environment variable named SECTION__FIELD, e.g.
RISK__RISK_PER_TRADE_PCT=0.25  or  STRATEGY__MIN_SCORE=70  (values in .env are loaded first).
"""
import os
import re
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Optional

VERSION = "5.3.0"
LADDER = ("M5", "M15", "H1", "H4", "D1", "W1")
MODES = ("DRY_RUN", "DEMO", "LIVE")
_V4 = os.getenv("V4_FEATURES", "on").lower() != "off"     # V4_FEATURES=off reproduces V3 behaviour (regression mode)



def load_env(path=".env"):
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if v[:1] in ('"', "'") and v[1:].find(v[0]) >= 0:
            v = v[1:1 + v[1:].find(v[0])]           # quoted value: keep everything inside the quotes
        else:
            v = re.split(r"\s+#", v, maxsplit=1)[0].strip()   # strip inline comments
        os.environ.setdefault(k.strip(), v)


def _cast(raw, default):
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    if isinstance(default, tuple):
        kind = type(default[0]) if default else str
        return tuple(kind(x.strip()) for x in raw.split(",") if x.strip())
    return raw


def env_override(obj, prefix):
    changes = {}
    for f in fields(obj):
        key = f"{prefix}__{f.name}".upper()
        cur = getattr(obj, f.name)
        if key in os.environ and not isinstance(cur, (dict, type(None))) and not hasattr(cur, "__dataclass_fields__"):
            changes[f.name] = _cast(os.environ[key], cur)
    return replace(obj, **changes) if changes else obj


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade_pct: float = 0.5
    # Account-aware risk ceilings: small balances get a slightly larger percentage ceiling,
    # but never outside the blueprint's 1-2% per-trade risk discipline.
    micro_account_max_equity: float = 25.0
    micro_account_risk_pct: float = 1.0
    small_account_max_equity: float = 500.0
    small_account_risk_pct: float = 0.75
    max_open_risk_pct: float = 1.5
    max_daily_loss_pct: float = 2.0
    max_weekly_loss_pct: float = 4.0
    max_drawdown_pct: float = 8.0
    max_open_trades: int = 3
    max_trades_per_day: int = 6
    min_free_margin_pct: float = 50.0      # free margin kept after entry, % of equity
    min_margin_level: float = 300.0        # projected margin level floor, %
    loss_streak_reduce_at: int = 3         # consecutive losses before risk is reduced
    loss_streak_reduce_factor: float = 0.5
    loss_streak_pause_at: int = 5          # consecutive losses before pausing entries
    # V5.1 documents (features.doc_rules): two consecutive full stop-outs lock trading for the rest of the UTC day;
    # every losing trade starts a 15-minute cooldown ("15-minute forced dissociation"). The 2% daily cap is above.
    two_loss_lock_after: int = 2
    loss_cooldown_min: float = 15.0
    full_loss_r: float = 0.8               # a close at -0.8R or worse counts as a full stop-out
    shared_state_file: str = "data/shared_risk.json"   # account-wide two-loss lock / cooldown across market instances
    pause_hours: float = 12.0
    throttle_dominance: float = 0.6        # share of streak losses in one regime that keeps the pause going
    throttle_extend_h: float = 1.0
    adaptive_quality_risk: bool = True      # reduce size in cautious/high-vol/event conditions; never increases base risk
    cautious_risk_mult: float = 0.70
    high_vol_risk_mult: float = 0.70
    expansion_risk_mult: float = 0.80
    news_caution_risk_mult: float = 0.60
    # V5.7 exposure scaling (Strategies Summary: drawdown-aware leverage + volatility targeting). Every factor is
    # <= 1: scaling can only REDUCE the per-trade risk, never raise it above the configured value.
    dd_scaling: bool = True
    dd_scale_start_pct: float = 2.0        # drawdown from the equity peak where risk starts to shrink
    dd_scale_full_pct: float = 6.0         # drawdown where the floor is reached (the hard halt stays max_drawdown_pct)
    dd_scale_floor: float = 0.35
    vol_scaling: bool = True
    vol_scale_atr_ratio: float = 1.3       # ATR above this multiple of its recent average shrinks risk ~1/ratio
    vol_scale_floor: float = 0.5
    equity_vol_target_pct: float = 12.0    # annualised volatility target of the ACCOUNT's own daily returns
    equity_vol_min_days: int = 10
    equity_vol_floor: float = 0.5
    exposure_scale_floor: float = 0.25     # combined multiplier never below this (smaller is a pause, not sizing)


@dataclass(frozen=True)
class ExecutionConfig:
    max_spread_points: int = 40
    max_slippage_points: int = 30
    max_spread_risk_frac: float = 0.15     # spread may not exceed this share of the stop distance
    max_entry_drift_atr: float = 0.3       # abort if price moved this far since the decision
    min_stop_atr: float = 0.5
    max_stop_atr: float = 3.0
    spread_stop_mult: float = 3.0
    max_retries: int = 2
    retry_delay_s: float = 0.4
    max_tick_age_s: float = 45.0
    limit_expiry_min: float = 30.0
    limit_min_improve_atr: float = 0.15    # use a limit only if it beats the market price by this much
    limit_fill_through_points: float = 5.0 # backtest: price must trade through the limit by this many points
    require_order_check: bool = True       # broker-side preflight before every live order modification/send
    server_time_max_drift_s: float = 10.0  # corrected server clock drift tolerated before trading is blocked
    pending_revalidate_on_fill: bool = True # re-run risk/news/price validity when a pending order actually fills
    cost_model_file: str = "data/cost_model.json"
    max_spread_atr_frac: float = 0.15      # V5.1: index / energy / crypto spread cap = this x M5 ATR (points differ per CFD)   # written by evidence.py --calibrate-costs from your own fills
    session_spread_caps: dict = field(default_factory=dict)   # e.g. {"ASIA": 50}; empty = one global cap (validate first)


@dataclass(frozen=True)
class StyleConfig:
    code: str
    tf: str
    htf: tuple
    min_rr: float
    max_rr: float
    risk_mult: float
    be_r: float
    partial: Optional[tuple]
    max_hold_h: float
    trail_tf: str
    trail_mode: str = "hybrid"             # structure | atr | ema | hybrid
    trail_atr_mult: float = 2.0


def _styles():
    # tf = zone timeframe; htf = (structural, macro); roles in HierarchyConfig define what each TF may do
    return {
        "scalp": StyleConfig("sc", "M15", ("H1", "H4"), 1.5, 2.5, 0.5, 0.8, None, 3, "M5", "atr", 1.5),
        "day": StyleConfig("da", "H1", ("H4", "D1"), 1.8, 3.5, 1.0, 1.0, (1.5, 0.5), 20, "M15"),
        "swing": StyleConfig("sw", "H4", ("D1", "W1"), 2.5, 5.0, 1.0, 1.5, (2.0, 0.33), 240, "H4"),
        "position": StyleConfig("po", "D1", ("W1", "W1"), 3.0, 8.0, 0.5, 2.0, (3.0, 0.25), 1440, "D1", "structure"),
    }


@dataclass(frozen=True)
class StrategyConfig:
    # Moderate decision policy: safety blockers remain hard, evidence disagreements are mostly soft.
    min_score: float = 65.0
    tp1_min_r: float = 1.0                 # V5.1 documents: TP1 at the nearest structural level, at least 1:1
    tp1_default_r: float = 1.5             # no structural level before the target: TP1 at 1.5R
    tp1_close_frac: float = 0.5            # close 50% at TP1, then stop to entry + spread
    min_score_gap: float = 8.0
    zone_pad_atr: float = 0.25
    zone_match_pad_atr: float = 0.25
    sweep_sl_pad_atr: float = 0.1
    # score weights (technical part sums to 100)
    w_mtf: float = 25.0                    # hierarchical multi-timeframe confluence
    w_structure: float = 10.0
    w_zone: float = 20.0
    w_location: float = 10.0
    w_liquidity: float = 10.0
    w_momentum: float = 10.0
    w_room: float = 5.0
    w_volatility: float = 5.0
    w_session: float = 5.0
    news_max_bonus: float = 5.0
    min_rr_mean_reversion: float = 1.2
    high_vol_pad_mult: float = 1.5
    low_vol_pad_mult: float = 0.8
    # volatility / regime thresholds
    block_extreme_vol: bool = True
    adx_bands: tuple = (15.0, 20.0, 25.0, 35.0)
    scalp_sessions: tuple = ("LONDON_OPEN", "LONDON_CONT", "OVERLAP", "NY_OPEN", "NY_CONT")
    min_bos_quality: float = 40.0
    structure_lookback_bars: int = 30
    styles: dict = field(default_factory=_styles)


@dataclass(frozen=True)
class NewsConfig:
    enabled: bool = True
    refresh_min: int = 15
    max_headline_age_h: float = 6.0
    min_confidence: float = 0.35
    veto_against: float = 0.75             # strong, reliable directional news can still hard-veto
    soft_opposition_threshold: float = 0.45 # weaker news disagreement reduces risk instead of blocking
    support_from: float = 0.3
    block_if_calendar_unavailable: bool = True
    # V5.1: official, key-free sources first (central banks and statistics agencies), then two market wires.
    # FXStreet removed (SSL failures in the V5 live run); Trading Economics removed from defaults (needs a key).
    rss_feeds: tuple = (
        "https://www.federalreserve.gov/feeds/press_all.xml",
        "https://www.federalreserve.gov/feeds/speeches_and_testimony.xml",
        "https://www.bls.gov/feed/bls_latest.rss",
        "https://www.ecb.europa.eu/rss/press.html",
        "https://www.cnbc.com/id/20910258/device/rss/rss.html",
        "https://feeds.bbci.co.uk/news/business/rss.xml",
    )
    http_contact: str = ""                 # optional e-mail added to the User-Agent (BLS asks automated clients for one)
    calendar_url: str = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    http_timeout_s: float = 10.0
    # V4 redundancy: several calendar providers, merged and de-duplicated; "file" = manual fallback you maintain
    # Provider tuple: (name, kind, source, family). The first two are the same vendor/family; the
    # Trading Economics adapter is genuinely independent and activates only when its API key exists.
    # The local file is an emergency manual/last-known-good source, not an independent vendor.
    # V5.1: no API keys. ForexFactory = impact-rated calendar for every currency (the documents' reference tool);
    # BLS = the official U.S. statistics agency's own release schedule (CPI, jobs report, PPI, JOLTS) as an
    # independent cross-check. ForexFactory publishes next week's file only late in the week: a 404 there is
    # "not published yet", not a failure.
    calendar_providers: tuple = (("forexfactory", "ff_json", "https://nfs.faireconomy.media/ff_calendar_thisweek.json", "forexfactory"),
                                 ("forexfactory_next", "ff_json", "https://nfs.faireconomy.media/ff_calendar_nextweek.json", "forexfactory"),
                                 ("bls_official", "bls_html", "https://www.bls.gov/opub/update.htm", "official_us"),
                                 ("local", "file", "data/calendar_override.json", "manual"))
    calendar_optional_providers: tuple = ("forexfactory_next",)
    calendar_cache_file: str = "data/calendar_cache.json"
    calendar_cache_max_age_h: float = 24.0
    calendar_trade_max_age_h: float = 4.0   # cached calendar older than this is never sufficient for new entries
    calendar_require_fresh_for_entries: bool = True
    calendar_min_remote_families: int = 1
    calendar_require_independent_remote_when_available: bool = True   # V4.2: cross-check & report, never discard fresh data
    calendar_strict_all_families: bool = False     # True = every configured vendor must succeed (lowers availability)
    calendar_refresh_s: float = 3600.0             # ForexFactory updates hourly and rate-limits frequent polling
    calendar_backoff_max_s: float = 3600.0
    calendar_stale_h: float = 12.0
    headline_dedupe_jaccard: float = 0.7
    calendar_state_fresh_h: float = 4.0
    calendar_allow_single_provider: bool = True
    calendar_single_provider_risk_mult: float = 0.70
    # degraded_only = cut only when 2+ vendor families are configured but at most one works (redundancy lost);
    # always = whenever only one family works (a permanent cut on single-vendor setups); off = never.
    calendar_single_provider_mode: str = "degraded_only"


@dataclass(frozen=True)
class SessionConfig:
    bounds: dict = field(default_factory=lambda: {"ASIA": (0, 7), "LONDON": (7, 12), "OVERLAP": (12, 16), "NY": (16, 21)})
    points: dict = field(default_factory=lambda: {"LONDON": 5, "OVERLAP": 5, "NY": 4, "ASIA": 2, "OFF": 0})
    avoid_hours_utc: tuple = (21, 22, 23)
    no_new_trades_friday_after_utc: int = 19
    split_asia: bool = False               # V4: evaluate Sydney/Tokyo split; enable only if the session report justifies it
    # fine phases in UTC minutes-of-day (fixed approximation; shift by an hour around DST changes if needed)
    phases: tuple = (("ASIA", 0, 420), ("LONDON_OPEN", 420, 540), ("LONDON_CONT", 540, 720), ("OVERLAP", 720, 810),
                     ("NY_OPEN", 810, 930), ("NY_CONT", 930, 1140), ("NY_CLOSE", 1140, 1260), ("OFF", 1260, 1440))
    dst_aware: bool = False                # V4.2: London/New York phases follow their own daylight-saving clocks
    phase_points: dict = field(default_factory=lambda: {"LONDON_OPEN": 5, "LONDON_CONT": 4, "OVERLAP": 5, "NY_OPEN": 5,
                                                        "NY_CONT": 3, "NY_CLOSE": 1, "ASIA": 2, "OFF": 0,
                                                        "TOKYO": 2, "SYDNEY": 0})


@dataclass(frozen=True)
class EmergencyConfig:
    max_consecutive_errors: int = 10
    flatten_on_max_drawdown: bool = False
    stop_file: str = "STOP"                # create this file to halt new entries
    flatten_file: str = "FLATTEN"          # create this file to close everything and halt
    heartbeat_s: int = 300
    daily_report_hour_utc: int = 21


@dataclass(frozen=True)
class HierarchyConfig:
    """What each timeframe is allowed to do. Lower timeframes can never override higher ones."""
    roles: dict = field(default_factory=lambda: {
        "scalp": {"macro": "H4", "structural": "H1", "zone": "M15", "confirm": "M5", "entry": "M5"},
        "day": {"macro": "D1", "structural": "H4", "zone": "H1", "confirm": "M15", "entry": "M5"},
        "swing": {"macro": "W1", "structural": "D1", "zone": "H4", "confirm": "H1", "entry": "M15"},
        "position": {"macro": "W1", "structural": "W1", "zone": "D1", "confirm": "H4", "entry": "H1"},
    })
    structural_veto: float = 0.5           # structural bias against the trade at/above this vetoes it
    confirm_lookback: int = 15
    trigger_lookback: int = 6
    zone_fallback_to_confirm: bool = True


def _allowed_setups():
    trend = ("TREND_PULLBACK", "TREND_CONTINUATION", "BOS_PULLBACK", "FVG_RETEST", "ORDER_BLOCK_RETEST")
    return {
        "STRONG_TREND": trend,
        "WEAK_TREND": trend + ("LIQUIDITY_SWEEP_REVERSAL",),
        "RANGE": ("MEAN_REVERSION", "LIQUIDITY_SWEEP_REVERSAL"),
        "COMPRESSION": ("LIQUIDITY_SWEEP_REVERSAL", "MEAN_REVERSION"),
        "BREAKOUT": ("BREAKOUT_RETEST", "BOS_PULLBACK", "FVG_RETEST"),
        "REVERSAL": ("LIQUIDITY_SWEEP_REVERSAL", "ORDER_BLOCK_RETEST", "FVG_RETEST"),
        "EXPANSION": ("TREND_CONTINUATION", "BOS_PULLBACK", "BREAKOUT_RETEST"),
        "NEWS_DRIVEN": (), "ABNORMAL": (), "EXTREME": (),
    }


@dataclass(frozen=True)
class RegimeConfig:
    regime_tf: str = "H1"
    context_tf: str = "H4"
    strong_trend_strength: float = 70.0
    strong_trend_adx: float = 25.0
    weak_trend_strength: float = 40.0
    compression_bbw_pct: float = 0.2
    breakout_lookback: int = 6
    reversal_lookback: int = 10
    reversal_min_quality: float = 60.0
    allowed: dict = field(default_factory=_allowed_setups)
    no_trade: tuple = ("NEWS_DRIVEN", "ABNORMAL", "EXTREME")
    size_down: tuple = ("EXPANSION", "WEAK_TREND")


@dataclass(frozen=True)
class MicroConfig:
    normal_min: float = 70.0               # >= normal execution
    no_trade_below: float = 40.0           # < no new trades
    abnormal_below: float = 20.0           # < ABNORMAL regime
    baseline_bars: int = 60
    spread_ratio_warn: float = 1.5
    spread_ratio_bad: float = 2.5
    spread_ratio_extreme: float = 4.0
    velocity_warn_atr: float = 2.0
    velocity_bad_atr: float = 3.0
    tick_rate_low: float = 0.2
    tick_rate_high: float = 4.0
    volume_z_bad: float = 4.0
    gap_atr: float = 1.0
    slippage_ratio_bad: float = 2.0


@dataclass(frozen=True)
class SizingConfig:
    mode: str = "fixed"                    # fixed | tiered (enable tiered only after testing it)
    tiers: dict = field(default_factory=lambda: {"exceptional": 1.0, "normal": 0.7, "weak": 0.4})
    score_exceptional: float = 85.0
    score_normal: float = 72.0
    exp_r_exceptional: float = 0.40
    exp_r_normal: float = 0.15


@dataclass(frozen=True)
class PolicyConfig:
    """Cheap, switchable gates applied to candidates. Each switch can be ablated in optimize.py."""
    use_regime_filter: bool = True
    use_micro_gate: bool = True
    use_hierarchy_confirmation: bool = True
    use_contradiction: bool = True
    use_edge_book: bool = False            # only after walk-forward produced an OOS edge book
    use_calibration: bool = False          # only after a calibration was fitted on training data
    use_meta_model: bool = False           # V4.2: learned filter; only after metalabel.py gates + approval
    use_pro_quality_gate: bool = _V4        # independent-domain quality gate; blocks weak/concentrated confluence
    min_pro_quality_score: float = 58.0     # independent evidence quality floor; separate from the composite score
    high_vol_quality_floor: float = 64.0    # require stronger evidence when zone volatility is elevated
    meta_min_prob: float = 0.5
    conflict_reject: float = 0.65
    conflict_reduce: float = 0.30
    soft_confirmation_risk_mult: float = 0.75
    soft_quality_risk_mult: float = 0.75
    soft_conflict_risk_mult: float = 0.70
    soft_news_risk_mult: float = 0.65
    min_hard_quality_score: float = 48.0
    min_expected_r: float = 0.05
    edge_min_trades: int = 50
    calibration_file: str = "data/calibration.json"
    edge_book_file: str = "data/edge_book.json"
    setup_priority: tuple = ("LIQUIDITY_SWEEP_REVERSAL", "BREAKOUT_RETEST", "BOS_PULLBACK", "ORDER_BLOCK_RETEST",
                             "FVG_RETEST", "TREND_CONTINUATION", "TREND_PULLBACK", "MEAN_REVERSION")


def _loadings():
    # factor loadings used to derive correlations between symbols (cosine similarity)
    return {"XAUUSD": {"USD": -0.6, "GOLD": 1.0}, "XAGUSD": {"USD": -0.6, "GOLD": 0.85, "SILVER": 0.5},
            "EURUSD": {"USD": -1.0, "EUR": 0.6}, "GBPUSD": {"USD": -1.0, "GBP": 0.6},
            "AUDUSD": {"USD": -1.0, "AUD": 0.6, "GOLD": 0.2, "EQ": 0.2}, "USDJPY": {"USD": 1.0, "JPY": -0.6},
            "USDCHF": {"USD": 1.0, "CHF": -0.6},
            # V5.8: the rest of the watchlist (prior for the live matrix; used alone when no fresh matrix exists)
            "USDCAD": {"USD": 1.0, "CAD": -0.6, "OIL": -0.2},
            "US30": {"EQ": 1.0, "USD": -0.1}, "NAS100": {"EQ": 1.0, "TECH": 0.4, "USD": -0.1},
            "USOIL": {"OIL": 1.0, "EQ": 0.2, "USD": -0.2}, "BTCUSD": {"CRYPTO": 1.0, "EQ": 0.4, "USD": -0.2}}


@dataclass(frozen=True)
class PortfolioConfig:
    max_correlated_exposure_pct: float = 1.5
    loadings: dict = field(default_factory=_loadings)
    unknown_correlation: float = 0.5
    # V5.8 live correlation matrix (correlation_matrix.py)
    live_correlation: bool = True
    corr_timeframe: str = "H1"
    corr_lookback_bars: int = 720          # ~6 weeks of H1: the slow estimate
    corr_short_bars: int = 120             # ~1 week: catches correlation spikes in stress
    corr_min_overlap: int = 60             # fewer common bars than this -> the window is ignored (prior used)
    corr_shrink_k: float = 100.0           # shrinkage toward the factor prior: weight n / (n + k)
    corr_refresh_min: float = 60.0
    corr_max_age_h: float = 12.0           # older matrix -> factor prior
    corr_hedge_credit: float = 0.5         # share of a hedge's theoretical risk reduction that is believed
    corr_report_threshold: float = 0.7     # |rho| shown as a cluster in the daily report
    corr_file: str = "data/correlation_matrix.json"


def _event_classes():
    # name: (title patterns, gold sensitivity to a hotter print, before, release, max_after (minutes), typical 30m move USD)
    # Order matters: speeches are matched before FOMC so "FOMC Member X Speaks" is a speech, not a rate decision.
    # Patterns cover ForexFactory and Trading Economics naming ("Inflation Rate MoM", "Non Farm Payrolls", ...).
    return {
        "FED_SPEECH": (("speaks", "speech", "powell", "testif", "fed chair", "press conference"), 0, 15, 10, 60, 6.0),
        "JOBLESS": (("unemployment claims", "jobless claims", "continuing claims"), +1, 10, 5, 30, 5.0),
        "FOMC": (("fomc", "federal funds rate", "interest rate decision", "fed interest rate"), 0, 30, 15, 120, 25.0),
        "NFP": (("non-farm", "nonfarm", "non farm", "nfp", "payrolls", "unemployment rate", "average hourly earnings"),
                -1, 30, 10, 90, 20.0),
        "CPI": (("cpi", "consumer price", "inflation rate", "core inflation"), -1, 25, 10, 90, 15.0),
        "PPI": (("ppi", "producer price"), -1, 15, 5, 45, 8.0),
        "PCE": (("pce", "personal consumption expenditure"), -1, 20, 5, 60, 8.0),
        "GDP": (("gdp",), -1, 15, 5, 45, 8.0),
        "RETAIL": (("retail sales",), -1, 10, 5, 30, 6.0),
        "ISM_PMI": (("ism ", "pmi"), -1, 10, 5, 30, 6.0),
        "AUCTION": (("auction", "bond auction", "note auction"), 0, 10, 5, 30, 4.0),
        "OTHER": ((), 0, 15, 5, 45, 6.0),
    }


@dataclass(frozen=True)
class EventConfig:
    classes: dict = field(default_factory=_event_classes)
    stab_range_ratio: float = 1.6          # recent 1-minute range vs baseline must fall below this
    stab_spread_ratio: float = 1.3
    impacts_file: str = "data/event_impacts.json"
    caution_before_min: float = 60.0       # V4: CAUTION phase before the blackout (no scalps, smaller size in tiered mode)
    # V5.1 - the trading documents' impact filter (features.doc_rules):
    # High (red): no new entries from high_impact_pre_min before (strategy checklist: "no red news in the next hour";
    # Blueprint: 30 min) until high_impact_initial_reaction_min after release (initial reaction / manipulation), then
    # ABSORPTION until volatility settles. Medium (orange): monitored and reported, trading continues ("continue
    # normally with technical discipline"). Low (yellow): dropped.
    impact_levels: tuple = ("High", "Medium")
    high_impact_pre_min: float = 60.0
    high_impact_initial_reaction_min: float = 15.0
    medium_impact_caution: bool = False
    blackout_scale: float = 1.0            # multiply every class's pre/post windows
    relevance: dict = field(default_factory=lambda: {"FOMC": 1.0, "CPI": 0.95, "NFP": 0.95, "PCE": 0.8, "FED_SPEECH": 0.7,
                                                     "PPI": 0.6, "GDP": 0.6, "RETAIL": 0.5, "ISM_PMI": 0.5, "JOBLESS": 0.5,
                                                     "AUCTION": 0.4, "OTHER": 0.3})


@dataclass(frozen=True)
class IntelConfig:
    provider: str = "none" if _V4 else "anthropic"                # anthropic | openai | none
    anthropic_model: str = "claude-sonnet-5-5"
    openai_model: str = ""                 # required when provider=openai (set INTEL__OPENAI_MODEL)
    web_search: bool = False               # let the model search the web (costs more, slower)
    refresh_min: int = 20
    min_expiry_min: int = 5
    max_expiry_min: int = 240
    fallback_providers: tuple = ()          # e.g. ("openai_compatible",): tried in order when the primary fails
    compat_base_url: str = ""              # OpenAI-compatible endpoint (e.g. a Grok/xAI or self-hosted gateway)
    compat_model: str = ""
    compat_key_env: str = "COMPAT_API_KEY"
    veto_requires_reliable_source: bool = True
    min_veto_reliability: float = 0.6
    # V5.9 evidence gate: an LLM view may always REDUCE risk (opposition, veto); it may ADD score only after
    # intel_evaluation.py has shown, out of sample, that its direction calls beat chance.
    evidence_gate: bool = True
    log_file: str = "data/intel_log.jsonl"
    outcomes_file: str = "data/intel_outcomes.jsonl"
    evidence_file: str = "data/intel_evidence.json"
    evidence_min_samples: int = 50        # directional opinions needed before any verdict
    evidence_min_vetoes: int = 20         # blocked candidates needed to judge the veto
    evidence_alpha: float = 0.05          # one-sided binomial significance for "better than a coin"
    evidence_max_age_days: float = 30.0
    evidence_horizons_min: tuple = (60, 240)


@dataclass(frozen=True)
class FeatureFlags:
    """Every V4 feature can be switched off independently for validation (FEATURES__NAME=false)."""
    m1_refinement: bool = _V4
    macro_context: bool = _V4
    target_feasibility_v4: bool = _V4
    decision_records: bool = _V4
    telegram_explain: bool = _V4
    rejected_alerts: bool = _V4
    news_redundancy: bool = _V4
    event_caution: bool = _V4
    use_llm: bool = False
    reconnect_backoff: bool = _V4
    broker_fail_closed: bool = _V4
    registry_models: bool = _V4
    apply_production_params: bool = _V4
    drift_monitor: bool = _V4
    drift_auto_safe: bool = _V4
    htf_fetch_schedule: bool = _V4
    realistic_costs: bool = _V4
    precomputed_indicators: bool = _V4
    rejected_dataset: bool = _V4
    doc_rules: bool = _V4                  # V5.1: rules from the Professional Trading Blueprint / Ultimate strategy
    news_rules: bool = _V4                 # V4.3.1 deterministic headline reasoning (can only ADD caution)
    macro_feeds: bool = False              # V4.2 opt-in: enable after evidence.py --aux shows the feeds help
    limit_entries: bool = False            # V4.2 opt-in: limit orders at the zone instead of market entries
    shadow_mode: bool = False              # V4.2 opt-in: evaluate a registry candidate alongside production
    telegram_commands: bool = False        # V4.2 opt-in: authenticated /status /stop /resume /flatten
    require_fresh_calendar_entries: bool = _V4
    strict_pending_revalidation: bool = _V4


@dataclass(frozen=True)
class M1Config:
    window_min: int = 10                   # an armed M5 decision waits at most this long for M1 confirmation
    bars: int = 240
    confirm_lookback: int = 5
    against_lookback: int = 3
    max_chase_r: float = 0.35              # do not chase: price already moved this far toward the target
    max_adverse_r: float = 0.6             # signal invalidating: price moved this far toward the stop
    momentum_ok: bool = True


@dataclass(frozen=True)
class ReliabilityConfig:
    backoff_base_s: float = 2.0
    backoff_max_s: float = 120.0
    jitter_frac: float = 0.25
    ceiling_attempts: int = 8              # reconnect attempts per window before a cooldown
    ceiling_window_s: float = 600.0
    cooldown_s: float = 300.0
    broker_diag_interval_min: float = 60.0
    profit_tolerance: float = 0.02         # broker-calc vs independent derivation


@dataclass(frozen=True)
class NotifyConfig:
    premarket_plan: bool = True            # V5.1 Phase A: daily If-Then plan (Telegram + data/premarket/)
    premarket_hour_utc: int = 6
    weekly_review: bool = True             # V5.1 Phase C: Saturday review (documents)
    weekly_review_weekday: int = 5
    rejected_min_score: float = 55.0
    rejected_cooldown_min: float = 30.0
    daily_report: bool = True
    http_retries: int = 3
    http_retry_delay_s: float = 0.75
    # V5.5 Telegram intelligence (Enhancement Specification)
    display_timezone: str = "Asia/Beirut"  # every user-facing time; IANA zone, DST handled automatically
    news_announce_lead_min: float = 120.0  # announce a relevant event once when it is this close
    news_announce_medium: bool = True      # also announce medium-impact events that move the ACTIVE market
    chart_views: tuple = ("structure", "indicators", "execution")


@dataclass(frozen=True)
class LearningConfig:
    registry_dir: str = "data/registry"
    min_oos_trades: int = 100
    min_oos_exp_r: float = 0.05
    min_oos_pf: float = 1.1
    stress_spread_mult: float = 1.5
    stress_slippage_mult: float = 2.0
    sensitivity_pct: float = 0.2
    sensitivity_pass_share: float = 0.8
    mc_max_p_negative: float = 0.2
    mc_max_dd_r: float = 15.0
    mc_block_len: int = 5
    mc_sims: int = 5000
    mc_block_max_len: int = 20
    min_param_stability: float = 0.5
    meta_inner_folds: int = 3
    meta_min_filtered_gain: float = 0.05
    meta_min_filtered_trades: int = 30
    drift_z: float = 2.0
    drift_psi: float = 0.25
    drift_min_trades: int = 30
    # V5.6 per-market walk-forward acceptance (Strategies Summary: PF > 1.3, Sharpe > 0.8, max DD < 15%)
    market_min_pf: float = 1.3
    market_min_sharpe: float = 0.8
    market_max_dd_pct: float = 15.0
    market_stress_min_pf: float = 1.0      # still profitable with spread x stress_spread_mult, slippage x stress_slippage_mult
    market_evidence_max_age_days: float = 120.0


@dataclass(frozen=True)
class MacroConfig:
    refresh_min: float = 30.0
    lookback_bars: int = 24
    max_age_h: float = 72.0
    feeds: tuple = (("usd_index", ("USDX", "DXY", "DX", "USDIDX", "DOLLARINDEX", "USDOLLAR"), -1.0),
                    ("us10y", ("US10Y", "UST10Y", "US10YR", "TNOTE10Y", "UST10YR"), -1.0),
                    ("eurusd_usd_proxy", ("EURUSD",), 1.0))
    csv_feeds: tuple = (("gld_holdings", "data/aux/gld_holdings.csv", 1.0, 120.0),
                        ("gvz", "data/aux/gvz.csv", 0.0, 120.0))      # sign 0: recorded, not voted


@dataclass(frozen=True)
class ShadowConfig:
    params_version: str = ""               # registry id of a params candidate (never applied to live trading)
    meta_version: str = ""                 # registry id of a meta_model candidate


@dataclass(frozen=True)
class RolloutConfig:
    """V5.10 enforced deployment ladder: DRY_RUN -> DEMO -> LIVE_PILOT -> LIVE_FULL (rollout.py)."""
    enforce_live: bool = True               # LIVE mode needs a recorded LIVE_PILOT/LIVE_FULL promotion for THIS account
    state_file: str = "data/rollout.json"   # account-wide (shared by multi-market instances)
    max_incidents: int = 0                  # critical incidents tolerated inside the stage being judged
    dry_run_min_days: float = 10.0
    dry_run_min_signals: int = 20           # simulated entries in DRY_RUN
    demo_min_days: float = 20.0
    demo_min_trades: int = 40
    demo_min_pf: float = 1.2
    demo_max_dd_pct: float = 8.0
    demo_max_slippage_r: float = 0.10       # mean |slippage| in R must stay this small
    pilot_risk_pct: float = 0.25            # LIVE_PILOT: per-trade risk ceiling
    pilot_max_open_trades: int = 1
    pilot_min_days: float = 20.0
    pilot_min_trades: int = 30
    pilot_min_pf: float = 1.2
    pilot_max_dd_pct: float = 6.0
    pilot_max_expectancy_gap_r: float = 0.15  # live pilot expectancy may trail DEMO by at most this
    demote_window_trades: int = 20          # automatic review of the last N trades in a live stage
    demote_pf_below: float = 0.8
    demote_dd_pct: float = 5.0              # drawdown inside the live stage that triggers demotion


@dataclass(frozen=True)
class BrainConfig:
    """Python-native V5 brain/workflow settings.  No external agent orchestrator is required."""
    enabled: bool = True
    enforce: bool = False                   # start in advisory/shadow mode; deterministic kernel remains authoritative
    max_workers: int = 4
    journal_file: str = "data/v5_brain.jsonl"
    opportunity_report_threshold: float = 80.0
    no_trade_report_threshold: float = 55.0
    telegram_charts: bool = True
    universe_enabled: bool = True
    universe_refresh_min: float = 60.0
    universe_bars: int = 180
    universe_max_symbols: int = 12          # Scan at least 12 configured markets; rank down to a smaller actionable shortlist
    llm_enabled: bool = False               # optional direct LLM call; never gives order authority
    llm_max_tokens: int = 2000              # current models reason before answering; 900 truncated replies
    llm_timeout_s: float = 8.0
    watchlist: tuple = ("XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "XAGUSD", "US30", "NAS100", "USOIL", "BTCUSD", "AUDUSD", "USDCAD", "USDCHF")
    # Adaptive market selection: the engine can switch the active execution symbol while flat.
    # The selected market still passes the full downstream news/intermarket/critic/chief/risk gates.
    adaptive_market_selection: bool = True
    adaptive_selection_refresh_min: float = 5.0
    adaptive_selection_min_score: float = 65.0
    adaptive_selection_flat_only: bool = True
    adaptive_selection_allow_fixed_symbol: bool = False
    # V5.6: only markets that PASSED the per-market walk-forward (market_research.py --publish) may be traded.
    # Off by default so an install without research data keeps working; turn on after running the research.
    require_market_evidence: bool = False
    market_evidence_file: str = "data/market_evidence.json"


@dataclass(frozen=True)
class SystemConfig:
    # SYMBOL may be an exact broker name or AUTO. AUTO first tries the preferred
    # aliases, then semantically ranks the symbols exposed by the connected MT5 terminal.
    symbol: str = "AUTO" if _V4 else "XAUUSD"
    symbol_aliases: tuple = ("XAUUSD", "XAUUSDm", "XAUUSD.a", "XAUUSD.", "GOLD", "GOLDm")
    symbol_autodetect: bool = True
    mode: str = "DRY_RUN"                  # DRY_RUN | DEMO | LIVE
    magic: int = 560001
    expected_contract_size: float = 100.0
    bars: int = 500
    timeframes: tuple = LADDER
    fast_loop_s: float = 2.0
    news_loop_s: float = 60.0
    reconcile_s: float = 30.0
    server_utc_offset_h: Optional[float] = None
    data_dir: str = "data"
    state_file: str = "data/state.json"
    log_file: str = "data/agent.log"


@dataclass(frozen=True)
class Config:
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    news: NewsConfig = field(default_factory=NewsConfig)
    sessions: SessionConfig = field(default_factory=SessionConfig)
    emergency: EmergencyConfig = field(default_factory=EmergencyConfig)
    system: SystemConfig = field(default_factory=SystemConfig)
    hierarchy: HierarchyConfig = field(default_factory=HierarchyConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    micro: MicroConfig = field(default_factory=MicroConfig)
    sizing: SizingConfig = field(default_factory=SizingConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)
    events: EventConfig = field(default_factory=EventConfig)
    intel: IntelConfig = field(default_factory=IntelConfig)
    features: FeatureFlags = field(default_factory=FeatureFlags)
    m1: M1Config = field(default_factory=M1Config)
    reliability: ReliabilityConfig = field(default_factory=ReliabilityConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    learning: LearningConfig = field(default_factory=LearningConfig)
    macro: MacroConfig = field(default_factory=MacroConfig)
    shadow: ShadowConfig = field(default_factory=ShadowConfig)
    brain: BrainConfig = field(default_factory=BrainConfig)
    rollout: RolloutConfig = field(default_factory=RolloutConfig)

    @property
    def dry_run(self):
        return self.system.mode == "DRY_RUN"

    @classmethod
    def from_env(cls):
        load_env()
        c = cls()
        c = replace(c, risk=env_override(c.risk, "RISK"), execution=env_override(c.execution, "EXECUTION"),
                    strategy=env_override(c.strategy, "STRATEGY"), news=env_override(c.news, "NEWS"),
                    sessions=env_override(c.sessions, "SESSIONS"), emergency=env_override(c.emergency, "EMERGENCY"),
                    system=env_override(c.system, "SYSTEM"), hierarchy=env_override(c.hierarchy, "HIERARCHY"),
                    regime=env_override(c.regime, "REGIME"), micro=env_override(c.micro, "MICRO"),
                    sizing=env_override(c.sizing, "SIZING"), policy=env_override(c.policy, "POLICY"),
                    portfolio=env_override(c.portfolio, "PORTFOLIO"), events=env_override(c.events, "EVENTS"),
                    intel=env_override(c.intel, "INTEL"), features=env_override(c.features, "FEATURES"),
                    m1=env_override(c.m1, "M1"), reliability=env_override(c.reliability, "RELIABILITY"),
                    notify=env_override(c.notify, "NOTIFY"), learning=env_override(c.learning, "LEARNING"),
                    macro=env_override(c.macro, "MACRO"), shadow=env_override(c.shadow, "SHADOW"),
                    brain=env_override(c.brain, "BRAIN"), rollout=env_override(c.rollout, "ROLLOUT"))
        sysc = c.system
        extra = {}
        if os.getenv("SYMBOL"):
            extra["symbol"] = os.environ["SYMBOL"]
        if os.getenv("AGENT_MODE"):
            extra["mode"] = os.environ["AGENT_MODE"].upper()
        if os.getenv("SERVER_UTC_OFFSET"):
            extra["server_utc_offset_h"] = float(os.environ["SERVER_UTC_OFFSET"])
        if extra:
            c = replace(c, system=replace(sysc, **extra))
        return c

    def validate(self):
        """Return a list of configuration errors; an empty list means the config is usable."""
        e, r, x, s, sy = [], self.risk, self.execution, self.strategy, self.system
        if sy.mode not in MODES:
            e.append(f"mode must be one of {MODES}")
        if sy.mode == "LIVE" and os.getenv("CONFIRM_LIVE") != "YES_I_UNDERSTAND":
            e.append("LIVE mode requires CONFIRM_LIVE=YES_I_UNDERSTAND")
        if not 0 < r.risk_per_trade_pct <= 2:
            e.append("risk_per_trade_pct must be in (0, 2]")
        if not 0 < r.micro_account_risk_pct <= 2 or not 0 < r.small_account_risk_pct <= 2:
            e.append("small-account risk ceilings must be in (0, 2%]")
        if r.micro_account_max_equity <= 0 or r.small_account_max_equity <= r.micro_account_max_equity:
            e.append("account tier equity thresholds are invalid")
        if r.risk_per_trade_pct * 1.25 > r.max_open_risk_pct:
            e.append("max_open_risk_pct must allow at least one maximum-size trade")
        if r.max_daily_loss_pct >= r.max_drawdown_pct:
            e.append("max_daily_loss_pct should be below max_drawdown_pct")
        if x.min_stop_atr >= x.max_stop_atr:
            e.append("min_stop_atr must be below max_stop_atr")
        if sy.symbol.upper() != "AUTO" and sy.symbol not in sy.symbol_aliases and not sy.symbol_autodetect:
            e.append("symbol must be AUTO, a configured alias, or symbol_autodetect must be enabled")
        for name, st in s.styles.items():
            if st.tf not in sy.timeframes or any(h not in sy.timeframes for h in st.htf) or st.trail_tf not in sy.timeframes:
                e.append(f"style {name} uses a timeframe that is not in system.timeframes")
            if st.min_rr > st.max_rr:
                e.append(f"style {name}: min_rr above max_rr")
        for name, roles in self.hierarchy.roles.items():
            if name not in s.styles:
                e.append(f"hierarchy role set for unknown style {name}")
            if any(tf not in sy.timeframes for tf in roles.values()):
                e.append(f"hierarchy for {name} uses a timeframe that is not in system.timeframes")
            if s.styles.get(name) and s.styles[name].tf != roles["zone"]:
                e.append(f"style {name}: tf must equal its hierarchy zone timeframe")
        if self.sizing.mode not in ("fixed", "tiered"):
            e.append("sizing.mode must be fixed or tiered")
        if not 0 < self.policy.conflict_reduce < self.policy.conflict_reject <= 1:
            e.append("policy conflict thresholds must satisfy 0 < reduce < reject <= 1")
        for name in ("soft_confirmation_risk_mult", "soft_quality_risk_mult", "soft_conflict_risk_mult", "soft_news_risk_mult"):
            if not 0 < getattr(self.policy, name) <= 1:
                e.append(f"{name} must be in (0, 1]")
        if not 0 < self.policy.min_hard_quality_score <= self.policy.min_pro_quality_score <= 100:
            e.append("quality thresholds must satisfy 0 < hard floor <= pro-quality floor <= 100")
        if any(v > 1.0 for v in self.sizing.tiers.values()):
            e.append("sizing tiers may not exceed 1.0 x base risk")
        if self.intel.provider not in ("anthropic", "openai", "openai_compatible", "none"):
            e.append("intel.provider must be anthropic, openai, openai_compatible or none")
        for p in (self.intel.provider,) + tuple(self.intel.fallback_providers):
            if p not in ("anthropic", "openai", "openai_compatible", "none"):
                e.append(f"unknown intelligence provider {p}")
        if "openai_compatible" in (self.intel.provider,) + tuple(self.intel.fallback_providers) and not (self.intel.compat_base_url and self.intel.compat_model):
            e.append("openai_compatible provider needs INTEL__COMPAT_BASE_URL and INTEL__COMPAT_MODEL")
        if self.features.use_llm and self.intel.provider == "none":
            e.append("FEATURES__USE_LLM=true needs INTEL__PROVIDER (anthropic | openai | openai_compatible)")
        if self.news.calendar_single_provider_mode not in ("degraded_only", "always", "off"):
            e.append("news.calendar_single_provider_mode must be degraded_only, always or off")
        if not 0 < self.news.calendar_single_provider_risk_mult <= 1:
            e.append("news.calendar_single_provider_risk_mult must be in (0, 1]: it may only reduce risk")
        if self.m1.max_chase_r <= 0 or self.m1.window_min <= 0:
            e.append("m1 refinement window and chase limit must be positive")
        if self.intel.provider == "openai" and not self.intel.openai_model:
            e.append("intel.provider=openai requires INTEL__OPENAI_MODEL")
        if not self.micro.abnormal_below < self.micro.no_trade_below < self.micro.normal_min:
            e.append("micro thresholds must satisfy abnormal < no_trade < normal")
        tech = (s.w_mtf + s.w_structure + s.w_zone + s.w_location + s.w_liquidity + s.w_momentum
                + s.w_room + s.w_volatility + s.w_session)
        if abs(tech - 100) > 1e-6:
            e.append(f"technical weights sum to {tech}, expected 100")
        if x.require_order_check and sy.mode == "LIVE":
            try:
                mt5_mod = __import__("MetaTrader5")
                if not hasattr(mt5_mod, "order_check"):
                    e.append("LIVE mode requires MetaTrader5.order_check support")
            except Exception:
                e.append("LIVE mode requires the MetaTrader5 package")
        if self.news.calendar_require_fresh_for_entries and self.news.calendar_trade_max_age_h <= 0:
            e.append("calendar_trade_max_age_h must be positive when fresh calendar is required")
        if self.learning.meta_inner_folds < 2:
            e.append("meta_inner_folds must be >= 2")
        if self.news.calendar_trade_max_age_h > self.news.calendar_cache_max_age_h:
            e.append("calendar_trade_max_age_h cannot exceed calendar_cache_max_age_h")
        if self.learning.mc_block_len < 2 or self.learning.mc_block_max_len < self.learning.mc_block_len:
            e.append("Monte Carlo block lengths are invalid")
        if not 0 < self.policy.min_pro_quality_score <= 100:
            e.append("min_pro_quality_score must be in (0,100]")
        if not 0 < self.policy.high_vol_quality_floor <= 100:
            e.append("high_vol_quality_floor must be in (0,100]")
        if self.brain.max_workers < 1:
            e.append("brain.max_workers must be >= 1")
        if self.brain.opportunity_report_threshold <= 0:
            e.append("brain reporting interval and opportunity threshold must be positive")
        if not 0 < self.brain.no_trade_report_threshold <= 100:
            e.append("brain.no_trade_report_threshold must be in (0,100]")
        if self.brain.universe_refresh_min <= 0 or self.brain.universe_bars < 80 or self.brain.universe_max_symbols < 1:
            e.append("brain universe settings are invalid")
        if self.brain.llm_max_tokens < 100 or self.brain.llm_timeout_s <= 0:
            e.append("brain LLM token budget and timeout must be positive")
        if not self.brain.watchlist:
            e.append("brain.watchlist must not be empty")
        if self.brain.adaptive_selection_refresh_min <= 0:
            e.append("brain.adaptive_selection_refresh_min must be positive")
        if not 0 < self.brain.adaptive_selection_min_score <= 100:
            e.append("brain.adaptive_selection_min_score must be in (0,100]")
        for name, v in (("cautious_risk_mult", r.cautious_risk_mult), ("high_vol_risk_mult", r.high_vol_risk_mult),
                        ("expansion_risk_mult", r.expansion_risk_mult), ("news_caution_risk_mult", r.news_caution_risk_mult)):
            if not 0 < v <= 1:
                e.append(f"{name} must be in (0,1]")
        return e


SPLIT_ASIA = (("SYDNEY", 1260, 1440), ("TOKYO", 0, 420))


DST_MARK = ("__DST__", 0, 0)
LONDON_PHASES = ("LONDON_OPEN", "LONDON_CONT")
NY_PHASES = ("OVERLAP", "NY_OPEN", "NY_CONT", "NY_CLOSE")


def phases_for(sc):
    """Session phases, optionally splitting Asia into Tokyo/Sydney and marking them daylight-saving aware."""
    ph = sc.phases
    if sc.split_asia:
        keep = [p for p in ph if p[0] not in ("ASIA", "OFF")]
        ph = tuple(sorted(keep + list(SPLIT_ASIA), key=lambda p: p[1]))
    return (DST_MARK,) + tuple(ph) if getattr(sc, "dst_aware", False) else ph


def _last_sunday(y, m):
    import calendar
    import datetime as _dt
    d = _dt.date(y, m, calendar.monthrange(y, m)[1])
    return d - _dt.timedelta(days=(d.weekday() + 1) % 7)


def _nth_sunday(y, m, n):
    import datetime as _dt
    d = _dt.date(y, m, 1)
    d += _dt.timedelta(days=(6 - d.weekday()) % 7)
    return d + _dt.timedelta(days=7 * (n - 1))


def london_dst(ts):
    """UK summer time: last Sunday of March 01:00 UTC .. last Sunday of October 01:00 UTC."""
    import datetime as _dt
    y = ts.year
    a = _dt.datetime.combine(_last_sunday(y, 3), _dt.time(1))
    b = _dt.datetime.combine(_last_sunday(y, 10), _dt.time(1))
    t = _dt.datetime(ts.year, ts.month, ts.day, ts.hour, ts.minute)
    return a <= t < b


def us_dst(ts):
    """US daylight time: second Sunday of March 07:00 UTC .. first Sunday of November 06:00 UTC."""
    import datetime as _dt
    y = ts.year
    a = _dt.datetime.combine(_nth_sunday(y, 3, 2), _dt.time(7))
    b = _dt.datetime.combine(_nth_sunday(y, 11, 1), _dt.time(6))
    t = _dt.datetime(ts.year, ts.month, ts.day, ts.hour, ts.minute)
    return a <= t < b


def session_phase(ts, phases):
    m = ts.hour * 60 + ts.minute
    if phases and phases[0] == DST_MARK:              # windows are defined on winter (GMT / EST) clocks
        ml, mn = (m + 60 * london_dst(ts)) % 1440, (m + 60 * us_dst(ts)) % 1440
        for name, a, b in phases[1:]:
            if name in NY_PHASES and a <= mn < b:
                return name
        for name, a, b in phases[1:]:
            if name in LONDON_PHASES and a <= ml < b:
                return name
        phases = [p for p in phases[1:] if p[0] not in NY_PHASES + LONDON_PHASES]
    for name, a, b in phases:
        if a <= m < b:
            return name
    return "OFF"


def session_name(hour, bounds):
    for name, (a, b) in bounds.items():
        if a <= hour < b:
            return name
    return "OFF"
