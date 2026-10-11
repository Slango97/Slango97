"""News engine: three layers.
1. Scheduled events (events.py): pre-event blackout, release, stabilization, surprise.
2. Headlines (sources.py): reliability, recency and relevance weights, confirmation of high-impact claims.
3. Market intelligence (intel.py): structured LLM view with an expiry; vetoes must rest on reliable sources.
The LLM never opens trades. Failures degrade to neutral (UNKNOWN), never to 'trade anyway'."""
import hashlib
import json
import logging
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

import os
import re
from collections import deque

from events import EventEngine, classify_event
from redact import redact
from intel import BIAS, make_chain, parse, system_prompt
from sources import assess, enforce

log = logging.getLogger("news")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ExpertAIAgentProTrader/5.1", "Accept": "*/*"}
KEYWORDS = ("federal reserve", "monetary policy", "consumer price", "employment situation", "producer price", "lagarde",
            "bank of england", "bank of japan", "euro", "sterling", "yen", "oil", "crude", "opec", "stocks", "nasdaq", "s&p",
            "dow", "bitcoin", "crypto", "silver", "recession", "growth", "retail sales", "interest rate", "speech",
            "gold", "xau", "fed", "fomc", "powell", "inflation", "cpi", "pce", "ppi", "nfp", "payroll", "rate cut",
            "rate hike", "yield", "treasury", "dollar", "dxy", "tariff", "war", "geopolit", "safe-haven", "safe haven",
            "central bank", "ecb", "boj", "jobs", "unemployment", "gdp", "ceasefire", "sanction", "iran", "israel",
            "ukraine", "russia", "china", "missile", "attack")


@dataclass
class NewsContext:
    bias: float = 0.0
    confidence: float = 0.0
    impact: str = "low"
    summary: str = "no news analysis"
    status: str = "NO_DATA"            # OK | UNKNOWN | NO_DATA | DISABLED
    blackout: bool = False
    blackout_reason: str = ""
    calendar_ok: bool = True
    next_event: str = ""
    headlines: list = field(default_factory=list)
    event_phase: str = "CLEAR"
    event_bias: float = None
    expected_move: float = None
    risk_level: str = "low"
    trade_veto: bool = False
    drivers: list = field(default_factory=list)
    contradictions: list = field(default_factory=list)
    expires_in_min: float = None
    notes: list = field(default_factory=list)
    calendar_sources: list = field(default_factory=list)
    calendar_remote_families: int = 0
    calendar_cache_age_h: float = None
    calendar_coverage: str = "NONE"
    event_label: str = "CLEAR"           # V5.1: UPCOMING / PRE_EVENT / INITIAL_REACTION / ABSORPTION / POST_EVENT
    medium_upcoming: list = field(default_factory=list)
    instrument: str = ""
    calendar_state: str = "UNAVAILABLE"  # FRESH | CACHED | STALE | UNAVAILABLE
    calendar_configured_families: int = 0
    calendar_source_count: int = 0
    rules_bias: float = 0.0              # V4.3.1 deterministic headline evidence (caution-only)
    rules_confidence: float = 0.0
    rules_families: dict = field(default_factory=dict)
    rules_conflicts: list = field(default_factory=list)
    events: list = field(default_factory=list)   # V5.5: structured recent/upcoming events (id, local-time-ready)


def _default_http(url, timeout, contact=""):
    headers = dict(UA)
    if contact:
        headers["User-Agent"] += f" ({contact})"
    r = requests.get(url, timeout=(min(5.0, timeout), timeout), headers=headers)
    r.raise_for_status()
    return r.content


class NewsEngine:
    def __init__(self, cfg, http_get=None, llm=None, clock=None):
        self.cfg, self.nc, self.ic = cfg, cfg.news, cfg.intel
        self._http = http_get or (lambda u: _default_http(u, self.nc.http_timeout_s, self.nc.http_contact))
        self.profile = None
        self._raw_events = []
        self._llm = llm if llm is not None else make_chain(self.ic, enabled=cfg.features.use_llm)
        self.health = {}                     # provider name -> health record (calendar, feeds, intelligence)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self.ev = EventEngine(cfg)
        self.events_at = 0.0
        self.heads, self.heads_hash, self.heads_at = [], "", 0.0
        self.intel, self.intel_status, self.intel_notes = None, "NO_DATA", []
        self.errors = {}                     # per component, so one recovery never hides another failure
        self.calendar_sources = []
        self.calendar_remote_families = set()
        self.calendar_cached_at = 0.0
        self._calendar_remote_ok_at = 0.0
        self.calendar_coverage = "NONE"
        self.calendar_state = "UNAVAILABLE"
        self.calendar_configured_families = 0
        self.calendar_disagreements = []
        self._next_try, self._fails = {}, {}

    @property
    def last_error(self):
        return "; ".join(v for v in self.errors.values() if v)

    # ---- provider health ------------------------------------------------
    def _mark(self, name, ok, err="", latency_ms=None):
        h = self.health.setdefault(name, {"ok": 0, "fail": 0, "last_ok": 0.0, "last_error": "", "recent": deque(maxlen=10)})
        h["ok" if ok else "fail"] += 1
        h["state"] = "OK" if ok else "FAIL"
        h["recent"].append(bool(ok))
        if ok:
            h["last_ok"] = time.time()
        else:
            h["last_error"] = redact(err)[:160]
        if latency_ms is not None:
            h["latency_ms"] = round(latency_ms, 1)

    def _note(self, name, state, msg=""):
        """A provider that is not configured (no key) or not published yet is not a failure."""
        h = self.health.setdefault(name, {"ok": 0, "fail": 0, "last_ok": 0.0, "last_error": "", "recent": deque(maxlen=10)})
        h["state"], h["last_error"] = state, msg[:160]

    def provider_health(self):
        out = {}
        for name, h in self.health.items():
            if h.get("state") in ("NOT_CONFIGURED", "NOT_PUBLISHED"):
                out[name] = {"state": h["state"], "note": h["last_error"], "score": None, "ok": h["ok"], "fail": h["fail"]}
                continue
            fresh = time.time() - h["last_ok"] < self.nc.calendar_stale_h * 3600 if h["last_ok"] else False
            score = (sum(h["recent"]) / len(h["recent"]) if h["recent"] else 0.0) * (1.0 if fresh else 0.5)
            out[name] = {"state": h.get("state", "OK"), "score": round(score, 2), "fresh": fresh, "ok": h["ok"], "fail": h["fail"],
                         "last_error": h["last_error"], "latency_ms": h.get("latency_ms")}
        return out

    @staticmethod
    def _parse_calendar(data, impacts=("High",), currencies=("USD",)):
        """ForexFactory JSON. V5.1 keeps impact + currency on every event; the instrument filter is applied later."""
        evs = []
        for e in data:
            if e.get("impact") not in impacts or (currencies and e.get("country") not in currencies):
                continue
            try:
                t = datetime.fromisoformat(e["date"]).astimezone(timezone.utc)
            except Exception:
                continue
            evs.append({"title": e.get("title", ""), "time": t, "forecast": e.get("forecast"),
                        "previous": e.get("previous"), "actual": e.get("actual"),
                        "currency": e.get("country", "USD"), "impact": e.get("impact", "High"), "source": "ForexFactory"})
        return evs

    def _impacts(self):
        return tuple(self.cfg.events.impact_levels) if self.cfg.features.doc_rules else ("High",)

    def set_instrument(self, profile):
        """Filter the calendar to the currencies that move this instrument and sign every bias for it."""
        changed = getattr(self.profile, "symbol", None) != getattr(profile, "symbol", None)
        self.profile = profile
        self.ev.profile = profile
        if changed and self.intel is not None:
            # Intelligence is instrument-specific: a view (bias/veto) produced for the previous market must never
            # be applied to the newly selected one. Force a fresh, instrument-aware assessment.
            self.intel, self.intel_status, self.heads_hash = None, "UNKNOWN", None
        self._set_events(self._raw_events)

    def _set_events(self, events):
        self._raw_events = list(events)
        cur = set(self.profile.calendar_currencies) if self.profile is not None else {"USD"}
        self.ev.set_events([e for e in events if e.get("currency", "USD") in cur])

    def _calendar_key(self, e):
        """Vendors name events differently: known classes are matched by class + 10-minute bucket, others by title."""
        cls = classify_event(e.get("title", ""), self.cfg.events.classes)[0]
        name = cls.lower() if cls != "OTHER" else re.sub(r"[^a-z0-9]", "", e.get("title", "").lower())
        return (e.get("currency", "USD"), name if cls != "OTHER" else "other:" + name, int(e["time"].timestamp() // 600))

    def _save_calendar_cache(self, events):
        path = Path(self.nc.calendar_cache_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"saved_at": datetime.now(timezone.utc).isoformat(),
                   "events": [{**e, "time": e["time"].isoformat()} for e in events]}
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, path)

    def _load_calendar_cache(self):
        path = Path(self.nc.calendar_cache_file)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text())
            saved = datetime.fromisoformat(raw["saved_at"]).astimezone(timezone.utc)
            age_h = max(0.0, (self._clock() - saved).total_seconds() / 3600)
            if age_h > self.nc.calendar_cache_max_age_h:
                return None
            evs = []
            for e in raw.get("events", []):
                t = datetime.fromisoformat(e["time"]).astimezone(timezone.utc)
                evs.append({k: v for k, v in e.items() if k != "time"} | {"time": t})
            self.calendar_cached_at = saved.timestamp()
            self.calendar_sources = ["cache"]
            self.calendar_remote_families = set()
            return evs, age_h
        except Exception as ex:
            log.warning("calendar cache unusable: %s", ex)
            return None

    def _load_calendar(self):
        if not self.cfg.features.news_redundancy:
            return self._load_calendar_v3()
        merged, any_ok = {}, False
        remote_families = set()
        configured_remote_families = set()
        sources_ok, per_family, attempted = [], {}, False
        for spec in self.nc.calendar_providers:
            if len(spec) == 3:
                name, kind, src = spec
                family = name
            else:
                name, kind, src, family = spec
            # A provider containing ${ENV_NAME} is optional until that environment variable exists.
            missing_env = [e for e in re.findall(r"\$\{([A-Z0-9_]+)\}", src) if not os.getenv(e)]
            if missing_env:
                self._note("calendar:" + name, "NOT_CONFIGURED", "optional; needs " + ",".join(missing_env))
                continue
            src_resolved = re.sub(r"\$\{([A-Z0-9_]+)\}", lambda m: os.getenv(m.group(1), ""), src)
            if kind != "file":
                configured_remote_families.add(family)
                self.calendar_configured_families = len(configured_remote_families)
                if time.time() < self._next_try.get(name, 0.0):
                    continue                                # provider backoff (failures) / hourly refresh (success)
            attempted = True
            t0 = time.perf_counter()
            try:
                if kind == "file":
                    if not os.path.exists(src_resolved):
                        continue
                    raw = open(src_resolved, "rb").read()
                    evs = self._parse_calendar(json.loads(raw), self._impacts(), None)
                elif kind == "bls_html":
                    raw = self._http(src_resolved)
                    evs = parse_bls_schedule(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw)
                    if not evs:                      # the real page always lists upcoming releases: format changed?
                        raise RuntimeError("no releases parsed from the BLS schedule page")
                elif kind == "te_json":
                    raw = self._http(src_resolved)
                    evs = self._parse_calendar_te(json.loads(raw))
                else:
                    raw = self._http(src_resolved)
                    evs = self._parse_calendar(json.loads(raw), self._impacts(), None)
                self._mark("calendar:" + name, True, latency_ms=(time.perf_counter() - t0) * 1000)
                any_ok = True
                sources_ok.append(name)
                if kind != "file":
                    remote_families.add(family)
                    self._fails[name] = 0
                    self._next_try[name] = time.time() + self.nc.calendar_refresh_s - 60
                    per_family.setdefault(family, set()).update(self._calendar_key(e) for e in evs)
            except Exception as ex:
                if name in self.nc.calendar_optional_providers and "404" in str(ex):
                    self._note("calendar:" + name, "NOT_PUBLISHED", "next week's calendar is not published yet")
                    self._next_try[name] = time.time() + self.nc.calendar_refresh_s
                    continue
                self._mark("calendar:" + name, False, ex)
                log.warning("calendar provider %s failed: %s", name, redact(ex))
                if kind != "file":
                    self._fails[name] = self._fails.get(name, 0) + 1
                    self._next_try[name] = time.time() + min(self.nc.calendar_backoff_max_s, 60 * 2 ** (self._fails[name] - 1))
                continue
            for e in evs:
                key = self._calendar_key(e)
                cur = merged.get(key)
                if cur is None or (e.get("actual") and not cur.get("actual")):
                    merged[key] = e
        self.calendar_coverage = ("CROSS_CHECKED" if len(remote_families) >= 2 else "SINGLE_VENDOR" if remote_families
                                  else "MANUAL_ONLY" if any_ok else "NONE")
        strict_unmet = self.nc.calendar_strict_all_families and len(configured_remote_families) >= 2 \
            and not configured_remote_families <= remote_families
        if len(remote_families) >= 2 and self.nc.calendar_require_independent_remote_when_available:
            self._cross_check(per_family)                   # independent vendors: compare and report disagreements
        if not attempted:
            return                                          # every provider is in backoff: keep the current calendar
        if remote_families and not strict_unmet:            # fresh vendor data is ALWAYS preferred over the cache
            events = list(merged.values())
            with self._lock:
                self._set_events(events)
                self.events_at = time.time()
            self.calendar_sources = sources_ok
            self.calendar_remote_families = remote_families
            self._calendar_remote_ok_at = time.time()
            self.calendar_cached_at = self._calendar_remote_ok_at
            self.calendar_state = "FRESH"
            self.errors.pop("calendar_cache", None)
            try:
                self._save_calendar_cache(events)
            except Exception as ex:
                log.warning("calendar cache save failed: %s", redact(ex))
            return
        cached = self._load_calendar_cache()
        if cached is not None:
            events, age_h = cached
            fresh = list(merged.values())                   # union: partial fresh data is never thrown away
            keys = {self._calendar_key(e) for e in fresh}
            events = fresh + [e for e in events if self._calendar_key(e) not in keys]
            with self._lock:
                self._set_events(events)
                self.events_at = self.calendar_cached_at
            self.calendar_state = "CACHED" if age_h <= self.nc.calendar_trade_max_age_h else "STALE"
            self.errors["calendar_cache"] = f"using last-known-good calendar cache ({age_h:.1f}h old) plus {len(fresh)} fresh events"
            log.warning(self.errors["calendar_cache"])
            return
        if any_ok:                                          # manual file only: known events are used, coverage rule blocks
            with self._lock:
                self._set_events(list(merged.values()))
                self.events_at = time.time()
            self.calendar_sources, self.calendar_remote_families = sources_ok, set()
            self.calendar_state = "CACHED" if self.calendar_cached_at and (time.time() - self.calendar_cached_at) <= self.nc.calendar_trade_max_age_h * 3600 else "STALE"
            return
        self.calendar_state = "UNAVAILABLE"
        raise RuntimeError("all economic-calendar providers failed")

    def _cross_check(self, per_family):
        """High-impact events one vendor lists and another does not (kept in the union; reported for review)."""
        fams = list(per_family)
        diffs = []
        for f in fams:
            others = set().union(*(per_family[o] for o in fams if o != f))
            diffs += [f"{f} only: {k[0]} {k[1]} @ {k[2] * 10} min-bucket" for k in per_family[f] - others
                      if not str(k[1]).startswith("other") and k[0] == "USD"]
        self.calendar_disagreements = diffs[:20]

    @staticmethod
    def _parse_calendar_te(data):
        evs = []
        for e in data if isinstance(data, list) else []:
            if str(e.get("Country", "")).lower() not in ("united states", "united states of america"):
                continue
            if int(e.get("Importance", 0) or 0) < 3:
                continue
            try:
                t = datetime.fromisoformat(str(e["Date"]).replace("Z", "+00:00"))
                # Trading Economics publishes UTC without an offset: never let the PC's local timezone shift it
                t = t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)
            except Exception:
                continue
            evs.append({"title": e.get("Event") or e.get("Category", ""), "time": t,
                        "forecast": e.get("Forecast"), "previous": e.get("Previous"), "actual": e.get("Actual")})
        return evs

    def _load_calendar_v3(self):
        self.calendar_sources, self.calendar_remote_families = ["forexfactory"], {"forexfactory"}
        self.calendar_coverage = "SINGLE_VENDOR"
        data = json.loads(self._http(self.nc.calendar_url))
        evs = []
        for e in data:
            if e.get("country") != "USD" or e.get("impact") != "High":
                continue
            try:
                t = datetime.fromisoformat(e["date"]).astimezone(timezone.utc)
            except Exception:
                continue
            evs.append({"title": e.get("title", ""), "time": t, "forecast": e.get("forecast"),
                        "previous": e.get("previous"), "actual": e.get("actual")})
        with self._lock:
            self._set_events(evs)
            self.events_at = time.time()

    def _load_headlines(self):
        now = self._clock()
        cutoff = now - timedelta(hours=self.nc.max_headline_age_h)
        items, seen, failures = [], set(), 0
        from concurrent.futures import ThreadPoolExecutor

        def fetch(u):                                  # V5.1: feeds in parallel; one slow site no longer delays the rest
            t0 = time.perf_counter()
            try:
                return u, self._http(u), None, (time.perf_counter() - t0) * 1000
            except Exception as ex:
                return u, None, ex, None
        with ThreadPoolExecutor(max_workers=max(1, min(8, len(self.nc.rss_feeds)))) as pool:
            fetched = list(pool.map(fetch, self.nc.rss_feeds))
        for url, body, err, ms in fetched:
            try:
                if err is not None:
                    raise err
                root = ET.fromstring(body)
                self._mark("feed:" + url.split("/")[2], True, latency_ms=ms)
            except Exception as ex:
                failures += 1
                self._mark("feed:" + url.split("/")[2] if "//" in url else url, False, ex)
                log.warning("feed failed %s: %s", url, redact(ex))
                continue
            for it in root.iter("item"):
                title = (it.findtext("title") or "").strip()
                hits = sum(k in title.lower() for k in KEYWORDS)
                if not title or title in seen or not hits:
                    continue
                try:
                    pub = parsedate_to_datetime(it.findtext("pubDate") or "")
                    pub = pub if pub.tzinfo else pub.replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                if pub >= cutoff:
                    seen.add(title)
                    items.append(((now - pub).total_seconds() / 60, url, hits, title))
        if self.nc.rss_feeds and failures == len(self.nc.rss_feeds):
            raise RuntimeError("all news feeds failed")
        hs = assess(items)
        if self.cfg.features.news_redundancy:              # near-duplicate stories across feeds: keep the most reliable
            kept = []
            toks = lambda t: set(re.findall(r"[a-z]{3,}", t.lower()))
            for h in sorted(hs, key=lambda h: -h.reliability):
                th = toks(h.title)
                if any(len(th & toks(k.title)) / max(1, len(th | toks(k.title))) >= self.nc.headline_dedupe_jaccard for k in kept):
                    continue
                kept.append(h)
            hs = sorted(kept, key=lambda h: h.age_min)
        return hs[:25]

    def _prompt(self, now):
        lines = [f"[{h.age_min:.0f}m ago | {h.source} | reliability {h.reliability:.2f}"
                 f"{' | HIGH-IMPACT ' + ('CONFIRMED' if h.confirmed else 'UNCONFIRMED') if h.high_impact else ''}] {h.title}"
                 for h in self.heads]
        evs = [e for e in self.ev.events if now - timedelta(hours=3) <= e["time"] <= now + timedelta(hours=24)]
        ev_txt = "\n".join(f"{e['time']:%a %H:%M} UTC {e['title']} [{e['cls']}] forecast={e.get('forecast')} "
                           f"previous={e.get('previous')} actual={e.get('actual')} typical_move=${e['move']:.0f}" for e in evs)
        sym = getattr(self.profile, "symbol", None) or "XAUUSD"
        cur = "/".join(c for c in getattr(self.profile, "calendar_currencies", ("USD",)) if c != "All") or "USD"
        return (f"Current time: {now:%Y-%m-%d %H:%M} UTC\n\nHeadlines:\n" + ("\n".join(lines) or "none")
                + f"\n\nInstrument: {sym} | high-impact {cur} events (recent / next 24h):\n" + (ev_txt or "none"))

    def refresh(self):
        """Network work (call from a background thread). Never raises."""
        now = self._clock()
        try:
            if time.time() - self.events_at > 3600:
                self._load_calendar()
                self.errors["calendar"] = ""
        except Exception as ex:
            self.errors["calendar"] = f"calendar: {ex}"
            log.warning("calendar fetch failed: %s", ex)
        if not self.nc.enabled:
            return
        if time.time() - self.heads_at >= self.nc.refresh_min * 60:
            self.heads_at = time.time()
            try:
                self.heads = self._load_headlines()
                self.errors["headlines"] = ""
            except Exception as ex:
                self.errors["headlines"] = f"headlines: {ex}"
                self.heads = []
        if self._llm is None:
            self.intel_status = "DISABLED" if self.ic.provider == "none" else self.intel_status
            return
        h = hashlib.md5("|".join(x.title for x in self.heads).encode()).hexdigest()
        fresh = self.intel is not None and time.time() < self.intel["expires_at"] - 60
        recent = self.intel is not None and time.time() - self.intel["fetched_at"] < self.ic.refresh_min * 60
        if fresh and (h == self.heads_hash or recent):
            return
        if not self.heads and not self.ic.web_search:
            self.intel, self.intel_status, self.heads_hash = None, "NO_DATA", h
            return
        try:
            data = parse(self._llm(system_prompt(self.profile), self._prompt(now)), self.ic)
            data, notes = enforce(data, self.heads, self.ic)
            data["fetched_at"] = time.time()
            data["expires_at"] = data["fetched_at"] + 60 * data["expiry_minutes"]
            self.intel, self.intel_status, self.intel_notes, self.heads_hash = data, "OK", notes, h
            self.intel["provider"] = getattr(self._llm, "last_provider", "custom")
            self.errors["intel"] = ""
            log.info("intel: %s conf %.2f risk %s veto %s expires %sm | %s", data["gold_bias"], data["confidence"],
                     data["risk_level"], data["trade_veto"], data["expiry_minutes"], data["summary"])
        except Exception as ex:
            self.intel, self.intel_status = None, "UNKNOWN"
            self.errors["intel"] = f"intel: {ex}"
            log.warning("market intelligence unusable, news treated as UNKNOWN: %s", ex)

    def get(self, vol_probe=None):
        """Cheap, no network: compose the current context (event phase, intel validity, surprise)."""
        if not self.nc.enabled:
            return NewsContext(status="DISABLED", summary="news disabled")
        now = self._clock()
        ctx = NewsContext(headlines=[h.title for h in self.heads[:5]], notes=list(self.intel_notes))
        age_h = (max(0.0, (time.time() - self.calendar_cached_at) / 3600) if self.calendar_cached_at else None)
        ctx.calendar_cache_age_h = age_h
        if not self.cfg.features.news_redundancy:
            # Explicit V3-compatibility mode: one configured calendar stream is sufficient when fresh.
            ctx.calendar_ok = bool(self.events_at) and time.time() - self.events_at < self.nc.calendar_cache_max_age_h * 3600
            ctx.calendar_state = "FRESH" if ctx.calendar_ok else "UNAVAILABLE"
        elif self.calendar_state == "FRESH" and age_h is not None and age_h <= self.nc.calendar_state_fresh_h:
            ctx.calendar_ok = True
        elif self.calendar_state == "CACHED" and age_h is not None and age_h <= self.nc.calendar_trade_max_age_h:
            ctx.calendar_ok = True
        else:
            ctx.calendar_ok = False
        ctx.calendar_sources = list(self.calendar_sources)
        ctx.calendar_remote_families = len(self.calendar_remote_families)
        ctx.calendar_source_count = ctx.calendar_remote_families
        ctx.calendar_configured_families = (self.calendar_configured_families if self.cfg.features.news_redundancy
                                             else int(bool(self.events_at)))
        ctx.calendar_state = self.calendar_state
        ctx.calendar_coverage = self.calendar_coverage
        if self.cfg.features.news_rules and self.heads:
            from news_rules import assess_headlines
            ev = assess_headlines(self.heads, self.profile)
            ctx.rules_bias, ctx.rules_confidence = ev.bias, ev.confidence
            ctx.rules_families, ctx.rules_conflicts = ev.families, ev.conflicts
        with self._lock:
            es = self.ev.state(now, vol_probe)
        ctx.event_phase, ctx.blackout, ctx.next_event = es.phase, es.blackout, es.next_event
        ctx.blackout_reason, ctx.event_bias, ctx.expected_move = es.reason if es.blackout else "", es.surprise_bias, es.expected_move
        ctx.event_label = es.label
        now_ = datetime.now(timezone.utc)
        ctx.medium_upcoming = [f"{e.get('currency', '')} {e['title']} {e['time']:%a %H:%M} UTC" for e in self.ev.upcoming_medium(now_, 12)][:6]
        ctx.instrument = self.profile.symbol if self.profile is not None else ""
        try:
            from events import event_record
            cur = set(self.profile.calendar_currencies) if self.profile is not None else {"USD"}
            window = [e for e in list(self._raw_events) if now_ - timedelta(hours=3) <= e["time"] <= now_ + timedelta(hours=24)]
            ctx.events = [event_record(e, now_, self.cfg.events.classes, e.get("currency", "USD") in cur)
                          for e in sorted(window, key=lambda e: e["time"])][:40]
        except Exception as ex:                       # reporting data only: never break the trading context
            log.debug("event records unavailable: %s", ex)
        b1 = c1 = 0.0
        intel = self.intel
        if intel and self.intel_status == "OK":
            life = intel["expires_at"] - intel["fetched_at"]
            age = time.time() - intel["fetched_at"]
            if age < life:
                b1 = BIAS[intel.get("instrument_bias", intel["gold_bias"])]
                c1 = intel["confidence"] * (1 - 0.5 * age / life)
                ctx.risk_level, ctx.trade_veto = intel["risk_level"], intel["trade_veto"]
                ctx.drivers, ctx.contradictions = intel["key_drivers"], intel["contradictions"]
                ctx.summary, ctx.expires_in_min = intel["summary"], round((life - age) / 60, 1)
        b2 = es.surprise_bias if es.surprise_bias is not None else 0.0
        c2 = 0.6 if es.surprise_bias is not None else 0.0
        if c1 + c2 > 0:
            ctx.bias = (b1 * c1 + b2 * c2) / (c1 + c2)
            ctx.confidence = min(1.0, max(c1, c2) + 0.25 * min(c1, c2))
            ctx.status = "OK"
        else:
            ctx.status = "NO_DATA" if self.intel_status in ("NO_DATA", "DISABLED") else "UNKNOWN"
        ctx.impact = "high" if es.blackout or ctx.risk_level in ("high", "extreme") else "medium" if ctx.risk_level == "medium" else "low"
        # Two separate clocks matter: a cache can be technically usable for diagnostics while already too old
        # for opening a new position. Never call stale market-event data "fresh" after a restart.
        if self.cfg.features.news_redundancy and ctx.calendar_cache_age_h is not None and ctx.calendar_cache_age_h > self.nc.calendar_trade_max_age_h:
            ctx.calendar_ok = False
        if self.cfg.features.news_redundancy and self.nc.calendar_require_fresh_for_entries and self.nc.enabled and ctx.calendar_cache_age_h is None:
            ctx.calendar_ok = False
        if not ctx.blackout and self.nc.block_if_calendar_unavailable:
            if not ctx.calendar_ok:
                ctx.blackout, ctx.blackout_reason = True, "economic calendar unavailable or too stale for new entries"
            elif ctx.calendar_remote_families < self.nc.calendar_min_remote_families and not self.calendar_sources == ["cache"]:
                ctx.blackout, ctx.blackout_reason = True, "insufficient independent economic-calendar coverage"
        return ctx


BLS_RELEASES = {"employment situation": ("Non-Farm Employment Change (BLS Employment Situation)", "High"),
                "consumer price index": ("CPI m/m (BLS)", "High"),
                "producer price index": ("PPI m/m (BLS)", "High"),
                "job openings and labor turnover": ("JOLTS Job Openings (BLS)", "Medium"),
                "employment cost index": ("Employment Cost Index (BLS)", "Medium")}
_MONTHS = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august", "september",
                                       "october", "november", "december"), 1)}


def parse_bls_schedule(html):
    """Official BLS 'Upcoming News Releases' list (times in U.S. Eastern Time) -> UTC calendar events."""
    from config import us_dst
    text = re.sub(r"<[^>]+>", "\n", html)
    text = re.sub(r"&nbsp;|&#160;", " ", text)
    lines = [re.sub(r"\s+", " ", l).strip() for l in text.splitlines()]
    lines = [l for l in lines if l]
    pat = re.compile(r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})\s*\((\d{1,2}):(\d{2})\s*(AM|PM)\)")
    out = []
    for i, l in enumerate(lines):
        m = pat.search(l)
        if not m:
            continue
        name = (l[:m.start()].strip() or (lines[i - 1] if i else "")).lower()
        hit = next((v for k, v in BLS_RELEASES.items() if k in name), None)
        mon = _MONTHS.get(m.group(2).lower())
        if not hit or not mon:
            continue
        hh = int(m.group(5)) % 12 + (12 if m.group(7) == "PM" else 0)
        local = datetime(int(m.group(4)), mon, int(m.group(3)), hh, int(m.group(6)))
        off = 4 if us_dst(local + timedelta(hours=5)) else 5
        out.append({"title": hit[0], "time": (local + timedelta(hours=off)).replace(tzinfo=timezone.utc), "forecast": None,
                    "previous": None, "actual": None, "currency": "USD", "impact": hit[1], "source": "BLS (official)"})
    return out
