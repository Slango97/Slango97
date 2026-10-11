"""Scheduled-event lifecycle instead of a fixed blackout:
PRE (blackout) -> RELEASE -> STABILIZING (until volatility and spread normalize, bounded by max_after)
-> RE_EVALUATE (one cycle) -> CLEAR.
Also tracks forecast / previous / actual, the surprise direction for gold, and each event class's typical move."""
import json
import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger("events")
BLACKOUT = ("PRE", "RELEASE", "STABILIZING")


@dataclass
class EventState:
    phase: str = "CLEAR"            # CLEAR | PRE | RELEASE | STABILIZING | RE_EVALUATE
    title: str = ""
    cls: str = ""
    minutes_to: float = None
    minutes_since: float = None
    expected_move: float = None
    surprise_bias: float = None
    next_event: str = ""
    reason: str = ""
    relevance: float = 0.0
    uncertainty: float = 0.0
    time_to_event_min: float = None

    @property
    def blackout(self):
        return self.phase in BLACKOUT

    @property
    def label(self):
        """Phase names used in the V5 architecture document and the Telegram reports."""
        return PHASE_LABELS.get(self.phase, self.phase)


def classify_event(title, classes):
    t = title.lower()
    for name, row in classes.items():
        if row[0] and any(p in t for p in row[0]):
            return name, row
    return "OTHER", classes["OTHER"]


def parse_number(s):
    if s is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(s).replace(",", ""))
    if not m:
        return None
    suffix = str(s)[m.end():m.end() + 1].upper()
    return float(m.group(0)) * {"K": 1e3, "M": 1e6, "B": 1e9}.get(suffix, 1.0)


def uncertainty(ev, sens):
    """Direction uncertainty 0..1: unknowable for decisions/speeches, high without a forecast."""
    if sens == 0:
        return 1.0
    fc, prev = parse_number(ev.get("forecast")), parse_number(ev.get("previous"))
    if fc is None:
        return 0.8
    if prev is None:
        return 0.6
    return round(0.4 + 0.6 * min(1.0, abs(fc - prev) / max(abs(prev), 1e-9) / 0.2), 2)


PHASE_LABELS = {"CLEAR": "CLEAR", "CAUTION": "UPCOMING", "PRE": "PRE_EVENT", "RELEASE": "INITIAL_REACTION",
                "STABILIZING": "ABSORPTION", "RE_EVALUATE": "POST_EVENT"}
# "Actual < Forecast = Good for currency" releases (strategy document: unemployment-type data)
LOWER_IS_BETTER = ("unemployment rate", "jobless", "claims", "claimant count", "unemployment change")
CATEGORY = {"CPI": "inflation", "PPI": "inflation", "PCE": "inflation", "FOMC": "policy", "FED_SPEECH": "policy",
            "AUCTION": "policy", "NFP": "growth", "GDP": "growth", "RETAIL": "growth", "ISM_PMI": "growth",
            "JOBLESS": "growth", "OTHER": "growth"}


def currency_surprise(ev, classes):
    """How GOOD the release was for its currency, in [-1, 1] (Actual vs Forecast, inverted for unemployment-type data)."""
    actual, fc = parse_number(ev.get("actual")), parse_number(ev.get("forecast"))
    if actual is None or fc is None:
        return None
    prev = parse_number(ev.get("previous"))
    scale = max(abs(fc), abs(prev) if prev is not None else 0.0, 1e-9) * 0.1
    s = actual - fc
    g = (1 if s > 0 else -1 if s < 0 else 0) * min(1.0, abs(s) / scale)
    return -g if any(k in ev.get("title", "").lower() for k in LOWER_IS_BETTER) else g


def event_bias(ev, classes, profile=None):
    """Bias for the traded instrument from an actual-vs-forecast surprise (None when it cannot be computed).
    profile None = the V3/V4 gold-only behaviour, kept unchanged for the frozen tests."""
    if profile is not None:
        from instruments import instrument_effect
        g = currency_surprise(ev, classes)
        if g is None:
            return None
        cls, _ = classify_event(ev["title"], classes)
        eff = instrument_effect(profile, ev.get("currency", "USD"), CATEGORY.get(cls, "growth"))
        return round(g * eff, 4) if eff else None
    _, row = classify_event(ev["title"], classes)
    sens = row[1]
    actual, fc = parse_number(ev.get("actual")), parse_number(ev.get("forecast"))
    if sens == 0 or actual is None or fc is None:
        return None
    prev = parse_number(ev.get("previous"))
    scale = max(abs(fc), abs(prev) if prev is not None else 0.0, 1e-9) * 0.1
    s = actual - fc
    return sens * (1 if s > 0 else -1 if s < 0 else 0) * min(1.0, abs(s) / scale)


class EventEngine:
    def __init__(self, cfg):
        self.ec = cfg.events
        self.caution = cfg.features.event_caution
        self.doc = getattr(cfg.features, "doc_rules", False)
        self.profile = None
        self.medium = []
        self.events = []
        self.cleared = set()
        self.impacts = self._load_impacts()

    def _load_impacts(self):
        p = Path(self.ec.impacts_file)
        try:
            return json.loads(p.read_text()) if p.exists() else {}
        except Exception:
            return {}

    def set_events(self, events):
        out, medium = [], []
        for e in events:
            name, row = classify_event(e["title"], self.ec.classes)
            sc = self.ec.blackout_scale
            impact = str(e.get("impact", "High")).capitalize()
            if self.doc and impact == "Medium" and not self.ec.medium_impact_caution:
                medium.append({**e, "cls": name})            # orange folder: monitored, trading continues
                continue
            before, release = row[2] * sc, row[3] * sc
            if self.doc and impact == "High":                 # red folder: the documents' windows (never shorter)
                before = max(before, self.ec.high_impact_pre_min * sc)
                release = max(release, self.ec.high_impact_initial_reaction_min * sc)
            out.append({**e, "cls": name, "before": before, "release": release, "max_after": max(row[4] * sc, release + 15),
                        "move": self.impacts.get(name, row[5]), "relevance": self.ec.relevance.get(name, 0.3),
                        "uncertainty": uncertainty(e, row[1])})
        self.events = sorted(out, key=lambda e: e["time"])
        self.medium = sorted(medium, key=lambda e: e["time"])

    def upcoming_medium(self, now, hours=24):
        return [e for e in self.medium if now <= e["time"] <= now + timedelta(hours=hours)]

    def state(self, now, vol_probe=None):
        upcoming = [e for e in self.events if e["time"] > now]
        nxt = f"{upcoming[0]['title']} {upcoming[0]['time']:%a %H:%M} UTC" if upcoming else ""
        surprise = None
        for e in reversed(self.events):
            mins = (now - e["time"]).total_seconds() / 60
            if 0 <= mins <= 120:
                surprise = event_bias(e, self.ec.classes, self.profile)
                if surprise is not None:
                    break
        for e in self.events:
            t = e["time"]
            key = (e["title"], t.isoformat())
            base = dict(title=e["title"], cls=e["cls"], expected_move=e["move"], surprise_bias=surprise, next_event=nxt,
                        relevance=e["relevance"], uncertainty=e["uncertainty"],
                        time_to_event_min=round((t - now).total_seconds() / 60, 1))
            if self.caution and e["relevance"] >= 0.5 and \
                    t - timedelta(minutes=self.ec.caution_before_min * self.ec.blackout_scale) <= now < t - timedelta(minutes=e["before"]):
                return EventState("CAUTION", minutes_to=(t - now).total_seconds() / 60,
                                  reason=f"{e['title']} in {(t - now).total_seconds() / 60:.0f} min (caution window)", **base)
            if t - timedelta(minutes=e["before"]) <= now < t:
                return EventState("PRE", minutes_to=(t - now).total_seconds() / 60,
                                  reason=f"{e['title']} ({e['cls']}) at {t:%H:%M} UTC, typical move ${e['move']:.0f}", **base)
            if t <= now < t + timedelta(minutes=e["release"]):
                return EventState("RELEASE", minutes_since=(now - t).total_seconds() / 60,
                                  reason=f"{e['title']} just released", **base)
            if t + timedelta(minutes=e["release"]) <= now < t + timedelta(minutes=e["max_after"]) and key not in self.cleared:
                stable = False
                if vol_probe is not None:
                    rr, sr = vol_probe()
                    stable = rr <= self.ec.stab_range_ratio and sr <= self.ec.stab_spread_ratio
                if stable:
                    self.cleared.add(key)
                    return EventState("RE_EVALUATE", minutes_since=(now - t).total_seconds() / 60,
                                      reason=f"volatility normalized after {e['title']}; re-evaluating", **base)
                return EventState("STABILIZING", minutes_since=(now - t).total_seconds() / 60,
                                  reason=f"waiting for volatility to settle after {e['title']}", **base)
        return EventState("CLEAR", surprise_bias=surprise, next_event=nxt)


def learn_impacts(m5, events, classes, horizon_min=30, path=None):
    """Median absolute price excursion in the `horizon_min` after each event, per class (from your own data)."""
    per = {}
    idx = m5.index
    for e in events:
        t = pd.Timestamp(e["time"]).tz_localize(None) if pd.Timestamp(e["time"]).tzinfo else pd.Timestamp(e["time"])
        i = idx.searchsorted(t)
        j = idx.searchsorted(t + pd.Timedelta(minutes=horizon_min))
        if i >= len(idx) or j <= i:
            continue
        p0 = m5["open"].iloc[i]
        move = max(abs(m5["high"].iloc[i:j].max() - p0), abs(p0 - m5["low"].iloc[i:j].min()))
        per.setdefault(classify_event(e["title"], classes)[0], []).append(float(move))
    out = {k: round(float(np.median(v)), 2) for k, v in per.items() if len(v) >= 3}
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(out, indent=2))
    return out
