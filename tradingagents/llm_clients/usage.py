"""What a run spent: tokens from every model reply, and the nominal API-price cost a
Claude Code session reports. The CLI's stats handler shows the same for the terminal."""

from __future__ import annotations

import threading
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import LLMResult


class UsageTally(BaseCallbackHandler):
    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.calls, self.tokens_in, self.tokens_out, self.nominal_cost_usd = 0, 0, 0, 0.0

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        for generations in response.generations:
            for generation in generations:
                message = getattr(generation, "message", None)
                if not isinstance(message, AIMessage):
                    continue
                usage = message.usage_metadata or {}
                with self._lock:
                    self.calls += 1
                    self.tokens_in += int(usage.get("input_tokens", 0))
                    self.tokens_out += int(usage.get("output_tokens", 0))
                    self.nominal_cost_usd += float(message.response_metadata.get("total_cost_usd") or 0.0)

    def snapshot(self) -> dict:
        with self._lock:
            return {"calls": self.calls, "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
                    "nominal_cost_usd": self.nominal_cost_usd}
