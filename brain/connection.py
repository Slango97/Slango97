"""Connection/account Brain: broker binding, data freshness and execution environment."""
from __future__ import annotations

from .models import BrainFinding


class ConnectionBrain:
    name = "connection_account"

    def run(self, client, cfg, lifecycle: str = "RUNNING") -> BrainFinding:
        facts = {}
        reasons = []
        warnings = []
        try:
            acc = client.account()
            facts.update({
                "login": getattr(acc, "login", None),
                "server": getattr(acc, "server", None),
                "company": getattr(acc, "company", None),
                "currency": getattr(acc, "currency", None),
                "leverage": getattr(acc, "leverage", None),
                "balance": float(getattr(acc, "balance", 0.0) or 0.0),
                "equity": float(getattr(acc, "equity", 0.0) or 0.0),
                "margin_free": float(getattr(acc, "margin_free", 0.0) or 0.0),
            })
        except Exception as ex:
            return BrainFinding(self.name, "ERROR", 0, 0, 0, "account snapshot unavailable", warnings=[str(ex)])
        try:
            facts["symbol"] = getattr(client, "symbol", "")
            facts["symbol_auto_detected"] = bool(getattr(client, "symbol_auto_detected", False))
            facts["symbol_resolution"] = getattr(client, "symbol_resolution", {})
        except Exception:
            pass
        try:
            age = float(client.tick_age_s())
            # Tiny negative values can occur around offset recalculation; normalize harmless jitter.
            facts["tick_age_s"] = round(max(0.0, age), 3)
            fresh = age <= float(cfg.execution.max_tick_age_s) and age >= -5.0
        except Exception as ex:
            fresh = False
            warnings.append(f"tick age unavailable: {ex}")
            facts["tick_age_s"] = None
        try:
            facts["connected"] = bool(client.is_connected())
        except Exception:
            facts["connected"] = None
        try:
            dg = getattr(client, "diag", None)
            if dg is not None:
                facts["broker_diagnosis"] = {"status": dg.status, "cause": dg.cause, "errors": dg.errors, "warnings": dg.warnings}
                if dg.status == "FAIL":
                    reasons.extend(list(dg.errors)[:3])
            else:
                facts["broker_diagnosis"] = {"status": "UNKNOWN"}
        except Exception as ex:
            warnings.append(f"broker diagnosis unavailable: {ex}")
        if lifecycle != "RUNNING":
            reasons.append(f"engine {lifecycle}")
        if facts.get("connected") is False:
            reasons.append("MT5 terminal not connected")
        if not fresh:
            reasons.append("stale/invalid market data")
        diag_status = facts.get("broker_diagnosis", {}).get("status")
        if diag_status == "FAIL":
            reasons.append("broker diagnosis failed")
        if not reasons:
            reasons.append("account and broker binding healthy")
        if facts.get("connected") is False or not fresh or diag_status == "FAIL":
            score = 10.0
        elif lifecycle != "RUNNING":
            score = 35.0
        elif diag_status == "OK":
            score = 100.0
        else:
            score = 75.0
        return BrainFinding(self.name, "OK" if score >= 70 else "WARN", score, score / 100.0,
                            0, "account/broker environment healthy" if score >= 70 else "connection environment degraded",
                            reasons=reasons, warnings=warnings, facts=facts)
