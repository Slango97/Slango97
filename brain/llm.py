"""Optional direct LLM gateway.

This intentionally does NOT depend on the OpenAI Agents SDK.  It is a plain model gateway
used only as an advisory reasoning pass.  If disabled or unavailable, deterministic V5
brains remain fully functional.
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("v5.llm")


class DirectLLMGateway:
    def __init__(self, cfg):
        self.cfg = cfg
        self.enabled = bool(getattr(cfg.brain, "llm_enabled", False)) and str(getattr(cfg.intel, "provider", "none")).lower() == "openai"
        self.model = str(getattr(cfg.intel, "openai_model", "") or "")
        self.timeout_s = float(getattr(cfg.brain, "llm_timeout_s", 8.0))
        self.max_tokens = int(getattr(cfg.brain, "llm_max_tokens", 900))
        self.client = None
        if self.enabled:
            key = os.getenv("OPENAI_API_KEY", "")
            if not key or not self.model:
                self.enabled = False
                log.warning("V5 direct LLM disabled: OPENAI_API_KEY or INTEL__OPENAI_MODEL missing")
                return
            try:
                from openai import OpenAI
                self.client = OpenAI(api_key=key, timeout=self.timeout_s)
            except Exception as ex:
                self.enabled = False
                log.warning("V5 direct LLM unavailable: %s", ex)

    def reason(self, payload: dict) -> dict | None:
        if not self.enabled or self.client is None:
            return None
        prompt = (
            "You are the advisory Chief Trader reasoning layer for a deterministic MT5 trading system. "
            "Do not invent market facts. Do not alter risk beyond reducing it. Do not issue broker orders. "
            "Return JSON only with keys: action, permission, confidence, risk_multiplier, reasons, contradictions. "
            "The deterministic kernel remains authoritative.\n\n"
            + json.dumps(payload, default=str, ensure_ascii=False)
        )
        try:
            # Prefer the modern Responses API; keep a Chat Completions fallback for compatible SDK versions.
            raw = ""
            if hasattr(self.client, "responses"):
                resp = self.client.responses.create(model=self.model, input=prompt, max_output_tokens=self.max_tokens)
                raw = getattr(resp, "output_text", "") or ""
            elif hasattr(self.client, "chat") and hasattr(self.client.chat, "completions"):
                resp = self.client.chat.completions.create(model=self.model, messages=[{"role": "user", "content": prompt}],
                                                           max_tokens=self.max_tokens, temperature=0)
                raw = getattr(getattr(resp.choices[0], "message", None), "content", "") or ""
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1] if "\n" in raw else raw
                if raw.endswith("```"):
                    raw = raw[:-3].strip()
            start, end = raw.find("{"), raw.rfind("}")
            raw = raw[start:end + 1] if start >= 0 and end > start else raw
            data = json.loads(raw)
            if not isinstance(data, dict):
                return None
            action = str(data.get("action", "WAIT")).upper()
            permission = str(data.get("permission", "NO_TRADE")).upper()
            if action not in ("BUY", "SELL", "WAIT", "HALT"):
                return None
            if permission not in ("APPROVE", "REDUCE", "NO_TRADE", "BLOCK"):
                return None
            try:
                mult = max(0.0, min(1.0, float(data.get("risk_multiplier", 0.0))))
                conf = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
            except Exception:
                return None
            return {"action": action, "permission": permission, "confidence": conf, "risk_multiplier": mult,
                    "reasons": list(data.get("reasons", []))[:8], "contradictions": list(data.get("contradictions", []))[:8]}
        except Exception as ex:
            log.warning("V5 direct LLM reasoning failed: %s", ex)
            return None
