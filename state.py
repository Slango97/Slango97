import json
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("state")
SCHEMA_VERSION = 3


def _fresh():
    return {"schema_version": SCHEMA_VERSION, "risk": {}, "trades": {},
            "meta": {"created": datetime.now(timezone.utc).isoformat(), "saves": 0}}


def migrate(data):
    """Upgrade older state layouts to the current schema. Raises ValueError if unusable."""
    if not isinstance(data, dict) or "trades" not in data or "risk" not in data:
        raise ValueError("not a state document")
    v = int(data.get("schema_version", 1))
    if v > SCHEMA_VERSION:
        raise ValueError(f"state written by a newer version ({v})")
    if v < 2:
        for t in data["trades"].values():
            t.setdefault("partial", False)
            t.setdefault("be", False)
        v = 2
    if v < 3:
        for t in data["trades"].values():
            t.setdefault("lifecycle", "OPEN")
            t.setdefault("last_sl", None)
            t.setdefault("reconstructed", False)
        data.setdefault("meta", {"created": datetime.now(timezone.utc).isoformat(), "saves": 0})
        data["risk"].setdefault("loss_streak", 0)
    data["schema_version"] = SCHEMA_VERSION
    return data


class State:
    """Current-state store. History belongs in the journal, not here.

    On a corrupt file the bad file is quarantined, the newest valid backup is tried, and if nothing
    can be recovered `needs_recovery` is set so the engine halts trading until it has reconciled
    against MT5 and called `acknowledge()`.
    """

    def __init__(self, path, backups=5):
        self.path = Path(path)
        self.backups = backups
        self.needs_recovery = False
        self.notes = []
        self.data = _fresh()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _read(self, p):
        with open(p) as f:
            return migrate(json.load(f))

    def _load(self):
        if not self.path.exists():
            return
        try:
            self.data = self._read(self.path)
            return
        except Exception as ex:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            bad = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
            shutil.move(str(self.path), str(bad))
            self.notes.append(f"state file unreadable ({ex}); quarantined as {bad.name}")
            log.error(self.notes[-1])
        for b in sorted(self.path.parent.glob(self.path.name + ".bak*"), reverse=True):
            try:
                self.data = self._read(b)
                self.notes.append(f"recovered from backup {b.name}")
                log.warning(self.notes[-1])
                self.needs_recovery = True
                return
            except Exception:
                continue
        self.data = _fresh()
        self.needs_recovery = True
        self.notes.append("no valid backup; starting empty, MT5 reconciliation required")
        log.error(self.notes[-1])

    @property
    def trades(self):
        return self.data["trades"]

    @property
    def risk(self):
        return self.data["risk"]

    def acknowledge(self):
        self.needs_recovery = False

    def save(self):
        self.data["meta"]["saves"] = self.data["meta"].get("saves", 0) + 1
        self.data["meta"]["last_saved"] = datetime.now(timezone.utc).isoformat()
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=1, default=str)
            f.flush()
            os.fsync(f.fileno())
        if self.path.exists() and self.data["meta"]["saves"] % 10 == 1:
            shutil.copy2(self.path, self.path.with_name(f"{self.path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}"))
            for old in sorted(self.path.parent.glob(self.path.name + ".bak*"))[:-self.backups]:
                old.unlink(missing_ok=True)
        os.replace(tmp, self.path)
