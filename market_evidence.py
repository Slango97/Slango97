"""Out-of-sample evidence per market, produced by `market_research.py --publish` and read by the live system.

With BRAIN__REQUIRE_MARKET_EVIDENCE=true a market may be selected and traded only when its latest walk-forward
verdict is PASS and the evidence is younger than LEARNING__MARKET_EVIDENCE_MAX_AGE_DAYS. Missing, unreadable,
synthetic, failed or stale evidence all fail closed. The file is advisory-only data: it can never ENABLE a trade
that another gate rejects.
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from market_specs import canonical

_CACHE = {}


def load(path):
    """Parsed evidence file (cached by mtime), or None."""
    p = Path(path)
    try:
        mt = p.stat().st_mtime
    except OSError:
        return None
    hit = _CACHE.get(str(p))
    if hit and hit[0] == mt:
        return hit[1]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        data = None
    _CACHE[str(p)] = (mt, data)
    return data


def status_for(symbol, path, max_age_days=120.0, now=None):
    """(ok, status, reason) for a broker symbol. ok is True only for fresh, real-data PASS evidence."""
    data = load(path)
    market = canonical(symbol) or str(symbol).upper()
    if not isinstance(data, dict):
        return False, "MISSING", f"no market evidence file at {path}"
    if data.get("synthetic"):
        return False, "SYNTHETIC", "evidence was produced from synthetic data"
    row = (data.get("markets") or {}).get(market)
    if not isinstance(row, dict):
        return False, "MISSING", f"{market}: no walk-forward evidence"
    st = str(row.get("status", "MISSING"))
    try:
        gen = datetime.fromisoformat(str(data.get("generated_at")).replace("Z", "+00:00"))
        if gen.tzinfo is None:
            gen = gen.replace(tzinfo=timezone.utc)
        age_d = ((now or time.time()) - gen.timestamp()) / 86400
    except Exception:
        return False, "STALE", f"{market}: evidence has no valid generation time"
    if age_d > max_age_days:
        return False, "STALE", f"{market}: evidence is {age_d:.0f} days old (max {max_age_days:.0f})"
    if st != "PASS":
        why = "; ".join(row.get("reasons", [])[:2])
        return False, st, f"{market}: walk-forward {st}" + (f" ({why})" if why else "")
    return True, "PASS", f"{market}: walk-forward PASS"


def gate(cfg, symbol):
    """(ok, reason) honoring BRAIN__REQUIRE_MARKET_EVIDENCE; always ok when the requirement is off."""
    if not getattr(cfg.brain, "require_market_evidence", False):
        return True, "market evidence not required"
    ok, _, why = status_for(symbol, cfg.brain.market_evidence_file, cfg.learning.market_evidence_max_age_days)
    return ok, why
