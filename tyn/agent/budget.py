"""Token estimation with calibration against reported usage.

The estimate starts at the usual four characters per token and is corrected
after every response by comparing the estimate with the provider's reported
prompt tokens, so the per-request input budget tracks the real tokenizer.
"""

import json
import math
from typing import Any, Final

_PER_MESSAGE_OVERHEAD: Final = 4
_MIN_FACTOR: Final = 0.5
_MAX_FACTOR: Final = 2.5
_SMOOTHING: Final = 0.3


class TokenEstimator:
    """Estimate prompt tokens for a chat request."""

    def __init__(self, chars_per_token: float = 4.0) -> None:
        self._chars_per_token = chars_per_token
        self.factor = 1.0

    def raw_text(self, text: str) -> float:
        """Return the uncalibrated token estimate for ``text``."""
        return len(text) / self._chars_per_token

    def raw_request(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> float:
        """Return the uncalibrated estimate for a whole request."""
        total = self.raw_text(json.dumps(tools, separators=(",", ":"))) if tools else 0.0
        for message in messages:
            total += _PER_MESSAGE_OVERHEAD
            total += self.raw_text(str(message.get("content") or ""))
            for call in message.get("tool_calls") or []:
                function = call.get("function", {})
                total += self.raw_text(str(function.get("name", "")))
                total += self.raw_text(str(function.get("arguments", "")))
        return total

    def estimate(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> int:
        """Return the calibrated token estimate for a request."""
        return math.ceil(self.raw_request(messages, tools) * self.factor)

    def calibrate(self, raw_estimate: float, actual_prompt_tokens: int) -> None:
        """Move the correction factor toward the observed ratio."""
        if raw_estimate <= 0 or actual_prompt_tokens <= 0:
            return
        observed = actual_prompt_tokens / raw_estimate
        blended = (1 - _SMOOTHING) * self.factor + _SMOOTHING * observed
        self.factor = min(_MAX_FACTOR, max(_MIN_FACTOR, blended))
