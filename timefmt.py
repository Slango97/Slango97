"""User-facing time (Enhancement Spec section 7).

UTC stays the internal canonical clock. Everything a human reads (Telegram, reports, chart stamps) is converted
with the IANA zone NOTIFY__DISPLAY_TIMEZONE (default Asia/Beirut) so daylight-saving changes are automatic.
A fixed "+3" offset is never used: if the zone database is missing (Windows without the `tzdata` package) the
text falls back to explicit UTC instead of guessing a wrong local time.
"""
from datetime import datetime, timezone
from functools import lru_cache

DEFAULT_TZ = "Asia/Beirut"
_LABELS = {"Asia/Beirut": "Beirut"}


@lru_cache(maxsize=8)
def _zone(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return None


def zone_name(cfg=None):
    return str(getattr(getattr(cfg, "notify", None), "display_timezone", "") or DEFAULT_TZ)


def _aware(dt):
    if dt is None:
        dt = datetime.now(timezone.utc)
    if hasattr(dt, "to_pydatetime"):                 # pandas Timestamp
        dt = dt.to_pydatetime()
    if isinstance(dt, (int, float)):
        dt = datetime.fromtimestamp(float(dt), timezone.utc)
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)   # naive = UTC (kernel convention)


def to_local(dt=None, tz=DEFAULT_TZ):
    """Aware local datetime, or the aware UTC datetime when the zone is unavailable."""
    z = _zone(tz)
    d = _aware(dt)
    return d.astimezone(z) if z is not None else d.astimezone(timezone.utc)


def fmt_local(dt=None, tz=DEFAULT_TZ, fmt="%a %d %b %H:%M"):
    """'Thu 08 Oct 15:30 Beirut' (or '... 12:30 UTC' when the zone database is missing)."""
    z = _zone(tz)
    label = _LABELS.get(tz, tz.split("/")[-1].replace("_", " ")) if z is not None else "UTC"
    return f"{to_local(dt, tz):{fmt}} {label}"


def local_date(dt=None, tz=DEFAULT_TZ):
    """Calendar day as the user experiences it: daily-once reports roll over at local midnight."""
    return to_local(dt, tz).date().isoformat()
