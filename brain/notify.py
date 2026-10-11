"""Telegram Intelligence / Notification Manager (Enhancement Spec sections 2-3, 5, 10).

The engine keeps scanning every few seconds; Telegram only hears about CHANGES. Every outgoing report is
decided here by comparing the current state with the last reported one:

* daily-once channels (startup, market health, news briefing) roll over at LOCAL midnight (Asia/Beirut);
* state channels (best opportunity, no-trade, selector, regime ...) speak only when their fingerprint changes:
  WAIT -> WAIT is silent, WAIT -> SIGNAL is a message;
* news events advance through DISCOVERED -> ANNOUNCED -> RELEASED -> ANALYZED -> PROCESSED and each stage is
  reported at most once per stable event id;
* trade lifecycle (SIGNAL, OPEN, TP1, BE, FINISHED) is reported once per ticket and stage.

State is persisted (atomic JSON) so a restart does not replay the day's briefings. The manager only filters
reporting: it has no access to, and no influence on, any trading gate.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("v5.notify")

NEWS_STAGES = ("DISCOVERED", "ANNOUNCED", "RELEASED", "ANALYZED", "PROCESSED")
TRADE_STAGES = ("TRADE_SIGNAL", "TRADE_OPEN", "TP1_REACHED", "BE_PROTECTED", "TRADE_FINISHED")


class NotificationManager:
    def __init__(self, path=None, *, clock=time.time, max_events=500):
        self.path = Path(path) if path else None
        self.clock = clock
        self.max_events = max_events
        self._lock = threading.Lock()
        self.data = {"daily": {}, "states": {}, "events": {}, "trades": {}}
        self.suppressed = 0
        self._load()

    # ---- persistence ----------------------------------------------------
    def _load(self):
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for k in self.data:
                if isinstance(raw.get(k), dict):
                    self.data[k] = raw[k]
        except Exception as ex:                   # a damaged file only costs a possible duplicate message
            log.warning("notification state unreadable, starting fresh: %s", ex)

    def _save(self):
        if self.path is None:
            return
        try:
            ev = self.data["events"]
            if len(ev) > self.max_events:          # keep the most recent ids only
                for k in sorted(ev, key=lambda k: ev[k].get("at", 0))[:len(ev) - self.max_events]:
                    ev.pop(k, None)
            tr = self.data["trades"]
            if len(tr) > self.max_events:
                for k in sorted(tr, key=lambda k: tr[k].get("at", 0))[:len(tr) - self.max_events]:
                    tr.pop(k, None)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, default=str), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception as ex:
            log.warning("notification state not saved: %s", ex)

    def _decide(self, ok):
        if not ok:
            self.suppressed += 1
        return ok

    # ---- policies ---------------------------------------------------------
    def daily_due(self, channel, local_day):
        """True once per local calendar day per channel (startup, health, news briefing ...)."""
        with self._lock:
            if self.data["daily"].get(channel) == local_day:
                return self._decide(False)
            self.data["daily"][channel] = local_day
            self._save()
            return True

    def state_changed(self, channel, fingerprint, *, min_interval_s=0.0):
        """True when `channel` moves to a materially different state. `min_interval_s` additionally damps a
        state that oscillates (A -> B -> A) faster than a human can use."""
        fp = json.dumps(fingerprint, sort_keys=True, default=str)
        now = self.clock()
        with self._lock:
            last = self.data["states"].get(channel)
            if last and last.get("fp") == fp:
                return self._decide(False)
            if last and min_interval_s > 0 and now - float(last.get("at", 0)) < min_interval_s:
                return self._decide(False)
            self.data["states"][channel] = {"fp": fp, "at": now}
            self._save()
            return True

    def peek_state(self, channel):
        st = self.data["states"].get(channel)
        return json.loads(st["fp"]) if st else None

    def event_stage(self, event_id, stage, detail=None):
        """True when a news event reaches a NEW, later lifecycle stage (or its released numbers change)."""
        if stage not in NEWS_STAGES:
            raise ValueError(f"unknown news stage {stage}")
        sig = json.dumps(detail, sort_keys=True, default=str) if detail is not None else None
        with self._lock:
            cur = self.data["events"].get(event_id)
            if cur is not None:
                ci, ni = NEWS_STAGES.index(cur["stage"]), NEWS_STAGES.index(stage)
                material_update = ni == ci and stage == "RELEASED" and sig is not None and sig != cur.get("sig")
                if ni < ci or (ni == ci and not material_update):
                    return self._decide(False)
            self.data["events"][event_id] = {"stage": stage, "sig": sig, "at": self.clock()}
            self._save()
            return True

    def event_status(self, event_id):
        cur = self.data["events"].get(event_id)
        return cur["stage"] if cur else None

    def trade_stage(self, trade_key, stage):
        """True the first time a trade (ticket or candidate id) reaches `stage`."""
        if stage not in TRADE_STAGES:
            raise ValueError(f"unknown trade stage {stage}")
        key = str(trade_key)
        with self._lock:
            cur = self.data["trades"].setdefault(key, {"stages": [], "at": self.clock()})
            if stage in cur["stages"]:
                return self._decide(False)
            cur["stages"].append(stage)
            cur["at"] = self.clock()
            self._save()
            return True
