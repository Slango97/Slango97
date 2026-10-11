"""State-aware reconnect policy: exponential backoff with jitter, a retry ceiling per time window followed by a
cooldown, so an outage never turns into hammering MT5. Trading stays blocked until post-reconnect
reconciliation passes (enforced by the engine)."""
import random
import time
from collections import deque


class ReconnectPolicy:
    def __init__(self, rc, clock=time.time, rng=None):
        self.rc, self.clock = rc, clock
        self.rng = rng or random.Random()
        self.state = "CONNECTED"            # CONNECTED | RECONNECTING | COOLDOWN
        self.failures = 0
        self.next_at = 0.0
        self.attempts = deque()
        self.outages = 0
        self.ceiling_hits = 0
        self.lost_at = None

    def on_lost(self):
        if self.state == "CONNECTED":
            self.state, self.failures, self.next_at = "RECONNECTING", 0, self.clock()
            self.outages += 1
            self.lost_at = self.clock()

    def should_attempt(self):
        return self.state != "CONNECTED" and self.clock() >= self.next_at

    def delay_for(self, failures):
        d = min(self.rc.backoff_max_s, self.rc.backoff_base_s * 2 ** max(0, failures - 1))
        return max(0.0, d * (1 + self.rc.jitter_frac * (2 * self.rng.random() - 1)))

    def record(self, ok):
        now = self.clock()
        self.attempts.append(now)
        while self.attempts and now - self.attempts[0] > self.rc.ceiling_window_s:
            self.attempts.popleft()
        if ok:
            self.state, self.failures, self.next_at = "CONNECTED", 0, 0.0
            return
        self.failures += 1
        if len(self.attempts) >= self.rc.ceiling_attempts:
            self.state, self.next_at = "COOLDOWN", now + self.rc.cooldown_s
            self.ceiling_hits += 1
            self.attempts.clear()
        else:
            self.state, self.next_at = "RECONNECTING", now + self.delay_for(self.failures)

    def snapshot(self):
        return {"state": self.state, "failures": self.failures, "outages": self.outages, "ceiling_hits": self.ceiling_hits,
                "next_attempt_in_s": round(max(0.0, self.next_at - self.clock()), 1) if self.state != "CONNECTED" else 0}
