"""In-process event bus. Publishers never know who listens; a failing subscriber can never break trading."""
import logging
from collections import defaultdict

log = logging.getLogger("bus")


class EventBus:
    def __init__(self):
        self.subs = defaultdict(list)
        self.counts = defaultdict(int)

    def subscribe(self, topic, fn):
        self.subs[topic].append(fn)

    def publish(self, topic, payload):
        self.counts[topic] += 1
        for fn in self.subs.get(topic, []) + self.subs.get("*", []):
            try:
                fn(topic, payload)
            except Exception:
                log.exception("subscriber failed on %s", topic)
