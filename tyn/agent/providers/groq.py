"""Groq adapter over the official ``groq`` SDK (OpenAI-compatible chat API)."""

from typing import Any, Final

import groq

from tyn.agent.llm import (
    ChatResponse,
    ContextTooLargeError,
    ModelError,
    ModelRateLimitedError,
    ModelTransientError,
    PrepaidCreditsDepletedError,
    ToolCall,
    ToolCallGenerationError,
    public_message,
)
from tyn.agent.models import TokenUsage
from tyn.errors import TynError
from tyn.retry import parse_retry_after

# Provider codes meaning the model's own output could not be parsed (for
# example a malformed tool call). A fresh sample usually succeeds.
_GENERATION_ERRORS: Final = frozenset(
    {"tool_use_failed", "output_parse_failed", "json_validate_failed"}
)

_RATE_HEADERS: Final = (
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-tokens",
    "x-ratelimit-remaining-requests",
    "retry-after",
)


def _usage_from(raw: Any) -> TokenUsage:
    if raw is None:
        return TokenUsage()
    completion_details = getattr(raw, "completion_tokens_details", None)
    prompt_details = getattr(raw, "prompt_tokens_details", None)
    return TokenUsage(
        prompt=int(getattr(raw, "prompt_tokens", 0) or 0),
        completion=int(getattr(raw, "completion_tokens", 0) or 0),
        reasoning=int(getattr(completion_details, "reasoning_tokens", 0) or 0),
        cached=int(getattr(prompt_details, "cached_tokens", 0) or 0),
    )


class GroqChatModel:
    """Single-request adapter over the official Groq SDK."""

    def __init__(self, client: groq.Groq, model: str, *, reasoning_effort: str = "low") -> None:
        self._client = client
        self.model_name = model
        self._reasoning_effort = reasoning_effort

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: str | dict[str, Any],
        max_completion_tokens: int,
    ) -> ChatResponse:
        """Send one request; map provider failures to typed errors."""
        request: dict[str, Any] = {
            "model": self.model_name,
            "messages": [public_message(m) for m in messages],
            "tools": tools,
            "tool_choice": tool_choice,
            "max_completion_tokens": max_completion_tokens,
            "reasoning_effort": self._reasoning_effort,
            "include_reasoning": False,
            "temperature": 0.2,
        }
        try:
            raw = self._client.chat.completions.with_raw_response.create(**request)
        except groq.RateLimitError as exc:
            raise ModelRateLimitedError(
                parse_retry_after(exc.response.headers.get("retry-after"))
            ) from None
        except groq.APIStatusError as exc:
            raise status_error(exc) from None
        except (groq.APITimeoutError, groq.APIConnectionError) as exc:
            raise ModelTransientError(f"model request failed: {type(exc).__name__}") from None
        completion = raw.parse()
        choice = completion.choices[0]
        calls = [
            ToolCall(call.id, call.function.name, call.function.arguments or "")
            for call in (choice.message.tool_calls or [])
        ]
        headers = {k: raw.headers[k] for k in _RATE_HEADERS if k in raw.headers}
        return ChatResponse(
            content=choice.message.content,
            tool_calls=calls,
            usage=_usage_from(completion.usage),
            headers=headers,
            finish_reason=choice.finish_reason,
        )


def status_error(exc: groq.APIStatusError) -> TynError:
    """Map a Groq HTTP error to this package's exceptions."""
    status = exc.status_code
    code = ""
    body = exc.body if isinstance(exc.body, dict) else {}
    error = body.get("error", body) if isinstance(body, dict) else {}
    if isinstance(error, dict):
        code = str(error.get("code") or "")
    if status == 402:
        return PrepaidCreditsDepletedError()
    if status == 413:
        return ContextTooLargeError("request too large for the model (413)")
    if status >= 500:
        return ModelTransientError(f"model provider error HTTP {status}")
    if status == 400 and code in _GENERATION_ERRORS:
        return ToolCallGenerationError(f"the model produced output the provider rejected ({code})")
    return ModelError(f"model request rejected: HTTP {status} {code}".strip())
