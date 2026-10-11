import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("journal")

DECISION_FIELDS = ["time", "action", "wait_reason", "dir", "style", "setup", "score", "regime", "session", "vol",
                   "market_quality", "conflict", "p_win", "exp_r", "risk_tier", "htf_bias", "entry", "sl", "tp", "rr",
                   "target", "lots", "components", "reasons"]


class Journal:
    """state.json is the current state; these files are the historical record (never rewritten)."""

    def __init__(self, data_dir):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.dir / "events.jsonl"
        self.trades_path = self.dir / "trades.jsonl"
        self.decisions_path = self.dir / "decisions.csv"

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _append(self, path, obj):
        try:
            with open(path, "a") as f:
                f.write(json.dumps(obj, default=str) + "\n")
        except OSError as ex:
            log.error("journal write failed (%s): %s", path.name, ex)

    def event(self, kind, **data):
        self._append(self.events_path, {"t": self._now(), "kind": kind, **data})

    def record(self, rec):
        self._append(self.dir / "decision_records.jsonl", rec)

    def trade_open(self, **rec):
        self._append(self.trades_path, {"t": self._now(), "type": "open", **rec})

    def trade_close(self, **rec):
        self._append(self.trades_path, {"t": self._now(), "type": "close", **rec})

    def decision(self, dec, lots=0.0, note=""):
        try:
            new = not self.decisions_path.exists()
            with open(self.decisions_path, "a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=DECISION_FIELDS)
                if new:
                    w.writeheader()
                w.writerow({"time": self._now(), "action": dec.action, "wait_reason": dec.wait_reason,
                            "dir": dec.direction, "style": dec.style, "setup": dec.setup, "score": dec.score,
                            "regime": dec.regime, "session": dec.session, "vol": dec.vol_regime,
                            "market_quality": round(dec.market_quality), "conflict": dec.conflict, "p_win": dec.p_win,
                            "exp_r": dec.exp_r, "risk_tier": dec.risk_tier, "htf_bias": dec.htf_bias,
                            "entry": round(dec.entry, 2), "sl": round(dec.sl, 2), "tp": round(dec.tp, 2), "rr": dec.rr,
                            "target": dec.target_kind, "lots": lots,
                            "components": json.dumps(dec.components),
                            "reasons": "; ".join(dec.reasons + ([note] if note else []))})
        except OSError as ex:
            log.error("decision log failed: %s", ex)

    def read_jsonl(self, path):
        out = []
        try:
            with open(path) as f:
                for line in f:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        return out
