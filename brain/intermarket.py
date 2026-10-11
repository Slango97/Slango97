"""Intermarket Brain using the existing auxiliary macro feed object when available."""
from __future__ import annotations

from .models import BrainFinding


class IntermarketBrain:
    name = "intermarket"

    def run(self, aux=None, symbol: str = "") -> BrainFinding:
        facts = {"symbol": symbol, "aux_available": aux is not None}
        if aux is None:
            return BrainFinding(self.name, "WARN", 45, 0.35, 0, "intermarket data unavailable",
                                warnings=["DXY/US10Y auxiliary feed not available"], facts=facts)
        # MacroFeeds already produces signed biases for gold/selected asset.  We preserve its values.
        for key in ("dxy", "usd_index", "us10y", "eurusd_usd_proxy", "bias", "drivers"):
            if hasattr(aux, key):
                facts[key] = getattr(aux, key)
            elif isinstance(aux, dict) and key in aux:
                facts[key] = aux[key]
        bias = 0
        try:
            x = float(facts.get("bias", 0.0) or 0.0)
            bias = 1 if x > 0.15 else -1 if x < -0.15 else 0
        except Exception:
            pass
        facts["raw"] = aux if isinstance(aux, (dict, list, tuple, str, int, float, bool)) else repr(aux)
        return BrainFinding(self.name, "OK", 72, 0.60, bias, "intermarket context available",
                            reasons=["auxiliary macro feed loaded"], facts=facts)
