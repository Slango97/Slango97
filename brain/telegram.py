"""Asynchronous Telegram Brain: rich operational feed, never on the trading critical path."""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from pathlib import Path

import requests

log = logging.getLogger("v5.telegram")


class TelegramBrain:
    def __init__(self, cfg, alerts=None):
        self.cfg = cfg
        self.alerts = alerts
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.enabled = bool(self.token and self.chat_id)
        self._last = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue(maxsize=200)
        self._stop = threading.Event()
        self._worker = None
        if self.enabled:
            self._worker = threading.Thread(target=self._run, daemon=True, name="v5-telegram")
            self._worker.start()

    def _enqueue(self, item):
        try:
            self._queue.put_nowait(item)
            return True
        except queue.Full:
            log.warning("telegram V5 queue full; dropping report")
            return False

    def text(self, message: str, *, key: str = "", cooldown_s: float = 0.0):
        if not self.enabled:
            return False
        now = time.time()
        with self._lock:
            if key and cooldown_s > 0 and now - self._last.get(key, 0.0) < cooldown_s:
                return False
            if key:
                self._last[key] = now
        return self._enqueue(("text", str(message), None))

    def photo(self, path: str | Path, caption: str = ""):
        if not self.enabled or not path or not Path(path).exists():
            return False
        return self._enqueue(("photo", str(path), str(caption)))

    def _send_text(self, message: str):
        r = requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                          json={"chat_id": self.chat_id, "text": message}, timeout=8)
        r.raise_for_status()

    def _send_photo(self, path: str, caption: str):
        with open(path, "rb") as f:
            r = requests.post(f"https://api.telegram.org/bot{self.token}/sendPhoto",
                              data={"chat_id": self.chat_id, "caption": caption[:1000]},
                              files={"photo": f}, timeout=15)
        r.raise_for_status()

    def _run(self):
        while not self._stop.is_set() or not self._queue.empty():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                self._queue.task_done()
                continue
            kind, payload, caption = item
            try:
                if kind == "text":
                    self._send_text(payload)
                elif kind == "photo":
                    self._send_photo(payload, caption or "")
            except Exception as ex:
                log.warning("telegram V5 %s delivery failed: %s", kind, ex)
            finally:
                self._queue.task_done()

    def close(self, timeout=2.5):
        if not self.enabled or self._worker is None:
            return
        self._stop.set()
        try:
            self._queue.join()
        except Exception:
            pass
        self._worker.join(timeout=timeout)
