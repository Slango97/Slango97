"""Versioned model / parameter registry. Research artifacts and production pointers are kept apart; nothing reaches
production without passing gates AND an explicit, recorded approval. Every promotion is reversible."""
import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

KINDS = ("calibration", "edge_book", "params", "meta_model")


class PromotionError(RuntimeError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


ENTRY_KEYS = {"kind", "created", "sha256", "meta_sha256", "status", "signature"}


def _sign(text):
    key = os.getenv("REGISTRY_HMAC_KEY")
    return hmac.new(key.encode(), text.encode(), hashlib.sha256).hexdigest() if key else None


class Registry:
    def __init__(self, root):
        self.root = Path(root)
        (self.root / "artifacts").mkdir(parents=True, exist_ok=True)
        self.path = self.root / "manifest.json"
        self.m = json.loads(self.path.read_text()) if self.path.exists() else {"versions": {}, "production": {}, "history": []}

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.m, indent=2, default=str))
        os.replace(tmp, self.path)

    def register(self, kind, payload, meta):
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind}")
        vid = f"{kind}-{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}"
        payload_hash = _sha(payload)
        meta = dict(meta)
        meta.setdefault("artifact_schema", "v4.3")
        meta.setdefault("payload_sha256", payload_hash)
        (self.root / "artifacts" / f"{vid}.json").write_text(json.dumps({"payload": payload, "sha256": payload_hash, "meta_sha256": _sha(meta)}, indent=2, default=str))
        self.m["versions"][vid] = {"kind": kind, "created": _now(), "sha256": payload_hash, "meta_sha256": _sha(meta),
                                   "status": "candidate_passed" if meta.get("gates_passed") else "candidate_failed",
                                   "signature": _sign(payload_hash + _sha(meta)), **meta}
        self._save()
        return vid

    def get(self, vid):
        blob = json.loads((self.root / "artifacts" / f"{vid}.json").read_text())
        v = self.m["versions"][vid]
        if _sha(blob["payload"]) != blob["sha256"] or blob["sha256"] != v["sha256"]:
            raise PromotionError(f"artifact {vid} failed its payload integrity check")
        self.verify_meta(vid, blob)
        return blob["payload"]

    def meta(self, vid):
        return {k: x for k, x in self.m["versions"][vid].items() if k not in ENTRY_KEYS}

    def verify_meta(self, vid, blob=None):
        """The manifest's metadata (including gates_passed) must match the hash written at registration time,
        both in the manifest and in the artifact file; with REGISTRY_HMAC_KEY set, the signature must match too."""
        v = self.m["versions"][vid]
        blob = blob or json.loads((self.root / "artifacts" / f"{vid}.json").read_text())
        h = _sha(self.meta(vid))
        if "meta_sha256" in v and (h != v["meta_sha256"] or h != blob.get("meta_sha256", h)):
            raise PromotionError(f"metadata of {vid} was modified after registration")
        if os.getenv("REGISTRY_HMAC_KEY"):
            want = _sign(v["sha256"] + h)
            if not v.get("signature") or not hmac.compare_digest(v["signature"], want):
                raise PromotionError(f"signature of {vid} is missing or invalid")

    def promote(self, vid, approver, reason=""):
        v = self.m["versions"].get(vid)
        if v is None:
            raise PromotionError(f"unknown version {vid}")
        if v["status"] != "candidate_passed":
            raise PromotionError(f"{vid} is {v['status']}: only candidates that passed every gate can be promoted")
        self.verify_meta(vid)
        if not self.meta(vid).get("gates_passed"):
            raise PromotionError(f"{vid}: hashed metadata does not record passed gates")
        if not approver:
            raise PromotionError("an explicit approver is required")
        self.get(vid)
        prev = self.m["production"].get(v["kind"])
        if prev:
            self.m["versions"][prev]["status"] = "retired"
        self.m["production"][v["kind"]] = vid
        v["status"] = "production"
        self.m["history"].append({"action": "promote", "id": vid, "kind": v["kind"], "previous": prev,
                                  "approver": approver, "reason": reason, "time": _now()})
        self._save()

    def rollback(self, kind, approver, reason=""):
        if not approver:
            raise PromotionError("an explicit approver is required")
        cur = self.m["production"].get(kind)
        if cur is None:
            raise PromotionError(f"no production {kind} to roll back")
        prev = next((h["previous"] for h in reversed(self.m["history"]) if h["action"] == "promote" and h["id"] == cur), None)
        self.m["versions"][cur]["status"] = "retired"
        if prev:
            self.m["production"][kind] = prev
            self.m["versions"][prev]["status"] = "production"
        else:
            self.m["production"].pop(kind)
        self.m["history"].append({"action": "rollback", "kind": kind, "from": cur, "to": prev, "approver": approver,
                                  "reason": reason, "time": _now()})
        self._save()
        return prev

    def reject(self, vid, approver, reason=""):
        self.m["versions"][vid]["status"] = "rejected"
        self.m["history"].append({"action": "reject", "id": vid, "approver": approver, "reason": reason, "time": _now()})
        self._save()

    def production(self, kind):
        vid = self.m["production"].get(kind)
        if not vid:
            return None
        return {"id": vid, "payload": self.get(vid), "meta": self.m["versions"][vid]}

    def summary(self):
        return {"production": self.m["production"],
                "versions": {k: {"kind": v["kind"], "status": v["status"], "created": v["created"]} for k, v in self.m["versions"].items()},
                "history": self.m["history"][-10:]}
