"""Authenticated Telegram commands. Commands only act through the engine's existing kill-switch files, so every
MT5 call stays in the trading loop. Only your chat (and optionally listed user ids) is obeyed.

    /status            lifecycle, equity, positions, regime, last decision
    /report            today's statistics
    /stop              block new entries (safe, no PIN)
    /resume <PIN>      allow entries again
    /flatten <PIN>     returns a one-time code; then /confirm <CODE> within 60 s closes everything and halts
Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_COMMAND_PIN (required), TELEGRAM_ALLOWED_USER_IDS (optional).
"""
import hmac
import logging
import os
import secrets
import threading
import time

import requests

from redact import redact

log = logging.getLogger("telegram_cmd")


class CommandHandler:
    def __init__(self, cfg, status_fn, report_fn, clock=time.time):
        self.cfg, self.status_fn, self.report_fn, self.clock = cfg, status_fn, report_fn, clock
        self.chat = str(os.getenv("TELEGRAM_CHAT_ID", ""))
        self.users = {u.strip() for u in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",") if u.strip()}
        self.pin = os.getenv("TELEGRAM_COMMAND_PIN", "")
        self.pending_code, self.code_expires = None, 0.0
        self.fails, self.locked_until = 0, 0.0

    def authorised(self, chat_id, user_id):
        return bool(self.chat) and str(chat_id) == self.chat and (not self.users or str(user_id) in self.users)

    def _pin_ok(self, given):
        if self.clock() < self.locked_until:
            return False
        ok = bool(self.pin) and hmac.compare_digest(given.encode(), self.pin.encode())
        if ok:
            self.fails = 0
        else:
            self.fails += 1
            if self.fails >= 5:
                self.locked_until, self.fails = self.clock() + 900, 0
        return ok

    def handle(self, text, chat_id, user_id):
        if not self.authorised(chat_id, user_id):
            log.warning("ignored Telegram command from unauthorised chat/user")
            return None
        parts = (text or "").strip().split()
        if not parts:
            return None
        cmd, arg = parts[0].lower().split("@")[0], (parts[1] if len(parts) > 1 else "")
        em = self.cfg.emergency
        if cmd == "/status":
            return self.status_fn()
        if cmd == "/report":
            return self.report_fn()
        if cmd == "/stop":
            open(em.stop_file, "w").close()
            return "STOP set: no new entries. Open positions keep being managed."
        if cmd in ("/resume", "/flatten", "/confirm") and self.clock() < self.locked_until:
            return "Locked after repeated wrong PINs. Try again later."
        if cmd == "/resume":
            if not self._pin_ok(arg):
                return "Wrong PIN."
            # Resume is a request, not direct permission to trade. The engine consumes
            # this one-shot marker and runs its full safety/reconciliation checks.
            if os.path.exists(em.stop_file):
                os.remove(em.stop_file)
            data_dir = getattr(self.cfg.system, "data_dir", "data")
            from pathlib import Path
            import tempfile
            target = Path(data_dir) / "resume.request"
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix="resume-", suffix=".tmp", dir=str(target.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write("requested\n")
                os.replace(tmp_name, target)
            finally:
                try:
                    if os.path.exists(tmp_name): os.remove(tmp_name)
                except OSError:
                    pass
            return "Resume requested. The engine must validate safety before allowing entries."
        if cmd == "/flatten":
            if not self._pin_ok(arg):
                return "Wrong PIN."
            self.pending_code, self.code_expires = secrets.token_hex(3).upper(), self.clock() + 60
            return f"Confirm closing ALL agent positions: send /confirm {self.pending_code} within 60 s."
        if cmd == "/confirm":
            if self.pending_code and self.clock() <= self.code_expires and hmac.compare_digest(arg.upper(), self.pending_code):
                self.pending_code = None
                open(em.flatten_file, "w").close()
                return "FLATTEN requested: the engine will close every agent position and halt."
            self.pending_code = None
            return "Confirmation failed or expired."
        if cmd in ("/help", "/start"):
            return __doc__.split("Env:")[0].strip()
        return "Unknown command. /help"


class TelegramCommander(threading.Thread):
    def __init__(self, handler, http_get=None, http_post=None):
        super().__init__(daemon=True, name="telegram-commands")
        self.h, self.offset, self.stop_evt = handler, 0, threading.Event()
        self.tok = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._get = http_get or (lambda url, params: requests.get(url, params=params, timeout=35).json())
        self._post = http_post or (lambda url, data: requests.post(url, json=data, timeout=10))

    def poll_once(self):
        base = f"https://api.telegram.org/bot{self.tok}"
        data = self._get(f"{base}/getUpdates", {"offset": self.offset, "timeout": 25})
        for u in data.get("result", []):
            self.offset = max(self.offset, u["update_id"] + 1)
            m = u.get("message") or {}
            reply = self.h.handle(m.get("text", ""), (m.get("chat") or {}).get("id"), (m.get("from") or {}).get("id"))
            if reply:
                self._post(f"{base}/sendMessage", {"chat_id": self.h.chat, "text": reply[:3500]})

    def run(self):
        while not self.stop_evt.is_set():
            try:
                self.poll_once()
            except Exception as ex:
                log.warning("telegram polling error: %s", redact(ex))
                self.stop_evt.wait(10)
