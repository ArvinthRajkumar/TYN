"""Investigation trace: one event per model call, tool call and control step."""

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from tyn.agent.models import TokenUsage

PREVIEW_CHARS = 160


class TraceRecorder:
    """Collect trace events in memory; the runner persists them."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wallclock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._monotonic = monotonic
        self._wallclock = wallclock
        self._started = monotonic()
        self.events: list[dict[str, Any]] = []

    def event(self, kind: str, scope: str, **fields: Any) -> None:
        """Append one event; ``scope`` is ``discovery`` or a requirement key."""
        self.events.append(
            {
                "at": self._wallclock().isoformat(timespec="seconds"),
                "t": round(self._monotonic() - self._started, 3),
                "scope": scope,
                "kind": kind,
                **fields,
            }
        )

    def model_call(
        self,
        scope: str,
        *,
        turn: int,
        latency: float,
        usage: TokenUsage,
        estimate: int,
        tool_choice: str,
        tool_calls: list[str],
        waited: float = 0.0,
        remaining_tokens: str | None = None,
    ) -> None:
        """Record one model request; latency excludes pacing and retry waits."""
        self.event(
            "model_call",
            scope,
            turn=turn,
            latency_ms=round(max(0.0, latency - waited) * 1000),
            wait_ms=round(waited * 1000),
            ratelimit_remaining_tokens=remaining_tokens,
            estimated_prompt_tokens=estimate,
            prompt_tokens=usage.prompt,
            completion_tokens=usage.completion,
            reasoning_tokens=usage.reasoning,
            cached_tokens=usage.cached,
            tool_choice=tool_choice,
            requested_tools=tool_calls,
        )

    def tool_call(
        self,
        scope: str,
        *,
        turn: int,
        tool: str,
        arguments: Any,
        latency: float,
        result: str,
        status: str,
        estimated_tokens: int,
    ) -> None:
        """Record one tool execution with its result size."""
        self.event(
            "tool_call",
            scope,
            turn=turn,
            tool=tool,
            arguments=arguments,
            status=status,
            latency_ms=round(latency * 1000),
            result_chars=len(result),
            result_tokens_estimate=estimated_tokens,
            preview=" ".join(result.split())[:PREVIEW_CHARS],
        )

    def slice(self, scope: str) -> list[dict[str, Any]]:
        """Return events for one scope."""
        return [event for event in self.events if event["scope"] == scope]
