"""Secret redaction for every log line, health record and alert. API keys in query strings, Telegram bot tokens in
URL paths, bearer tokens and the value of any secret-looking environment variable are replaced by ***."""
import logging
import os
import re

_PATTERNS = [
    (re.compile(r"([?&](?:c|key|apikey|api_key|token|access_token|client|secret)=)[^&\s'\"]+", re.I), r"\1***"),
    (re.compile(r"/bot\d+:[A-Za-z0-9_-]+"), "/bot***"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._-]+", re.I), r"\1***"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{10,}"), "sk-***"),
]
_SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASS)", re.I)


def _env_secrets():
    return sorted({v for k, v in os.environ.items() if _SECRET_ENV.search(k) and v and len(v) >= 6}, key=len, reverse=True)


def redact(text):
    s = str(text)
    for sec in _env_secrets():
        if sec in s:
            s = s.replace(sec, "***")
    for pat, rep in _PATTERNS:
        s = pat.sub(rep, s)
    return s


class RedactingFilter(logging.Filter):
    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        clean = redact(msg)
        if clean != msg or record.args:
            record.msg, record.args = clean, ()
        return True


def install(logger=None):
    """Attach the filter to every handler of the given (default: root) logger."""
    lg = logger or logging.getLogger()
    f = RedactingFilter()
    for h in lg.handlers:
        h.addFilter(f)
    lg.addFilter(f)
    return f
