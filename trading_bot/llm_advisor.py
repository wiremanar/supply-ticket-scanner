"""LLM decision layer (Claude).

The rule-based layer produces candidate buys/sells. This layer hands Claude a
structured snapshot — candidates, current positions, cash, and each symbol's
indicator breakdown — and asks for a final, bounded decision per symbol with a
short rationale. The model may only choose among the symbols it is shown, and
its sizing is always re-checked by the deterministic risk manager afterward.

If the model is disabled, unavailable, or returns something unparseable, the
advisor degrades to the rule-based candidates (when ``fallback_to_rules``).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .brokers.base import Account, Position
from .config import LLMConfig
from .strategies import Signal

try:
    import anthropic
except ImportError:  # pragma: no cover
    anthropic = None


@dataclass
class Decision:
    symbol: str
    action: str          # "buy" | "sell" | "hold"
    # Fraction of equity to *target* for this name on a buy, in [0, 1].
    target_weight: float
    confidence: float    # [0, 1]
    rationale: str
    source: str          # "llm" | "rules"


SYSTEM_PROMPT = """You are a disciplined trading assistant inside an automated system.
You are given a set of candidate symbols with technical indicators, the current
portfolio, and available cash. Decide an action for each symbol.

Rules you must follow:
- Only output decisions for symbols present in the input. Never invent symbols.
- Valid actions: "buy", "sell", "hold".
- "sell" is only meaningful for symbols currently held.
- target_weight is the fraction of total equity you want this position to reach
  (0.0 to 1.0). It is a target, not an order size; downstream risk limits will
  shrink it as needed. Use 0 for hold/sell.
- Be conservative. Prefer "hold" when signals conflict or are weak.
- confidence is 0.0 to 1.0.
- Keep each rationale to one sentence.

Respond with ONLY a JSON object of this exact shape, no prose, no markdown:
{"decisions": [{"symbol": "AAA", "action": "buy", "target_weight": 0.1, "confidence": 0.6, "rationale": "..."}]}
"""


class LLMAdvisor:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self._client = None
        if cfg.enabled and anthropic is not None and os.getenv("ANTHROPIC_API_KEY"):
            self._client = anthropic.Anthropic()

    @property
    def available(self) -> bool:
        return self._client is not None

    def decide(
        self,
        signals: dict[str, Signal],
        positions: dict[str, Position],
        account: Account,
    ) -> list[Decision]:
        candidates = {s: sig for s, sig in signals.items()
                      if sig.candidate != "hold" or s in positions}
        if not candidates:
            return []

        if not self.available:
            return self._rule_decisions(candidates, positions) if self.cfg.fallback_to_rules else []

        payload = self._build_payload(candidates, positions, account)
        try:
            raw = self._call_model(payload)
            decisions = self._parse(raw, set(candidates))
            if decisions:
                return decisions
        except Exception:
            pass  # fall through to rules

        return self._rule_decisions(candidates, positions) if self.cfg.fallback_to_rules else []

    # --- model plumbing ------------------------------------------------------
    def _build_payload(self, candidates, positions, account) -> str:
        rows = []
        for symbol, sig in candidates.items():
            held = positions.get(symbol)
            rows.append({
                "symbol": symbol,
                "last_price": round(sig.last_price, 4),
                "composite_score": round(sig.score, 3),
                "rsi": None if sig.rsi_value != sig.rsi_value else round(sig.rsi_value, 1),
                "rule_candidate": sig.candidate,
                "held_quantity": held.quantity if held else 0,
                "held_avg_price": held.avg_price if held else 0,
            })
        return json.dumps({
            "cash": round(account.cash, 2),
            "equity": round(account.equity, 2),
            "candidates": rows,
        })

    def _call_model(self, payload: str) -> str:
        msg = self._client.messages.create(
            model=self.cfg.model,
            max_tokens=self.cfg.max_tokens,
            temperature=self.cfg.temperature,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": payload}],
        )
        parts = [b.text for b in msg.content if getattr(b, "type", None) == "text"]
        return "".join(parts)

    def _parse(self, raw: str, allowed: set[str]) -> list[Decision]:
        raw = raw.strip()
        # Tolerate accidental code fences.
        if raw.startswith("```"):
            raw = raw.strip("`")
            raw = raw[raw.find("{"):]
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            return []
        obj = json.loads(raw[start:end + 1])
        out: list[Decision] = []
        for d in obj.get("decisions", []):
            symbol = str(d.get("symbol", "")).upper()
            action = str(d.get("action", "hold")).lower()
            if symbol not in allowed or action not in ("buy", "sell", "hold"):
                continue
            out.append(Decision(
                symbol=symbol,
                action=action,
                target_weight=max(0.0, min(1.0, float(d.get("target_weight", 0) or 0))),
                confidence=max(0.0, min(1.0, float(d.get("confidence", 0) or 0))),
                rationale=str(d.get("rationale", ""))[:280],
                source="llm",
            ))
        return out

    # --- deterministic fallback ---------------------------------------------
    def _rule_decisions(self, candidates, positions) -> list[Decision]:
        out: list[Decision] = []
        for symbol, sig in candidates.items():
            if sig.candidate == "buy":
                out.append(Decision(symbol, "buy", target_weight=min(0.1, abs(sig.score) * 0.2),
                                    confidence=abs(sig.score), rationale=f"rule signal {sig.detail}",
                                    source="rules"))
            elif sig.candidate == "sell" and symbol in positions:
                out.append(Decision(symbol, "sell", target_weight=0.0, confidence=abs(sig.score),
                                    rationale=f"rule signal {sig.detail}", source="rules"))
        return out
