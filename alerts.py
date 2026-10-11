import logging
import os
import queue
import smtplib
import threading
import time
from email.message import EmailMessage

import requests

from redact import redact

log = logging.getLogger("alerts")
LEVELS = {"INFO": "ℹ️", "WARN": "⚠️", "CRITICAL": "🚨"}


class Alerts:
    """Fire-and-forget notifications. Never raises and never blocks the trading loop.
    Channels are enabled by environment variables (see .env.example); with none set it only logs."""

    def __init__(self, cooldown_s=300, sender=None, http_retries=3, http_retry_delay_s=0.75):
        self.cooldown_s = cooldown_s
        self.http_retries = max(1, int(http_retries))
        self.http_retry_delay_s = max(0.0, float(http_retry_delay_s))
        self._last = {}
        self._q = queue.Queue(maxsize=200)
        self._sender = sender or self._deliver
        self.sent = []
        self._t = threading.Thread(target=self._worker, daemon=True)
        self._t.start()

    def send(self, level, text, key=None):
        log.log({"INFO": 20, "WARN": 30, "CRITICAL": 40}.get(level, 20), "ALERT %s: %s", level, text)
        k = key or text[:60]
        now = time.time()
        if level != "CRITICAL" and now - self._last.get(k, 0) < self.cooldown_s:
            return
        self._last[k] = now
        try:
            self._q.put_nowait(f"{LEVELS.get(level, '')} XAUUSD agent [{level}] {text}")
        except queue.Full:
            pass

    def flush(self, timeout=5.0):
        end = time.time() + timeout
        while not self._q.empty() and time.time() < end:
            time.sleep(0.05)

    def _worker(self):
        while True:
            msg = self._q.get()
            try:
                self._sender(msg)
                self.sent.append(msg)
            except Exception as ex:
                log.warning("alert delivery failed: %s", redact(ex))

    def _deliver(self, msg):
        def post(url, **kwargs):
            last = None
            for attempt in range(self.http_retries):
                wait = self.http_retry_delay_s * (2 ** attempt)
                try:
                    r = requests.post(url, timeout=5, **kwargs)
                    if r.status_code == 429:                       # rate limited: honour the server's retry-after
                        try:
                            wait = float(r.json().get("parameters", {}).get("retry_after") or r.headers.get("Retry-After") or wait)
                        except Exception:
                            wait = float(r.headers.get("Retry-After", wait) or wait)
                        last = RuntimeError("HTTP 429 rate limited")
                    elif 400 <= r.status_code < 500:                # bad token / chat id: retrying cannot help
                        raise RuntimeError(f"HTTP {r.status_code}: not retried")
                    else:
                        r.raise_for_status()
                        return True
                except RuntimeError:
                    raise
                except Exception as ex:
                    last = ex
                if attempt + 1 < self.http_retries:
                    time.sleep(min(wait, 60.0))
            raise RuntimeError(redact(f"notification HTTP delivery failed after {self.http_retries} attempts: {last}"))

        delivered = 0
        tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
        if tok and chat:
            post(f"https://api.telegram.org/bot{tok}/sendMessage", json={"chat_id": chat, "text": msg})
            delivered += 1
        hook = os.getenv("DISCORD_WEBHOOK_URL")
        if hook:
            post(hook, json={"content": msg})
            delivered += 1
        host, to = os.getenv("SMTP_HOST"), os.getenv("ALERT_EMAIL_TO")
        if host and to:
            m = EmailMessage()
            m["Subject"], m["From"], m["To"] = msg[:80], os.getenv("SMTP_USER", to), to
            m.set_content(msg)
            with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=10) as s:
                s.starttls()
                if os.getenv("SMTP_USER"):
                    s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD", ""))
                s.send_message(m)
            delivered += 1
        if not delivered:
            log.info("no external notification channel configured")
