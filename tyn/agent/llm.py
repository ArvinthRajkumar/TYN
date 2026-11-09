"""Model access: a thin Groq adapter and a resilient wrapper.

``GroqChatModel`` performs exactly one request and maps SDK errors to this
package's exceptions. ``ResilientModel`` adds pacing, daily accounting,
bounded retries with backoff and jitter, and usage bookkeeping. The agent
depends only on the ``ChatModel`` protocol, so tests inject scripted models.
"""

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Final, Protocol

import groq

from tyn.agent.budget import TokenEstimator
from tyn.agent.models import TokenUsage
from tyn.agent.pacer import DailyBudget, TokenPacer
from tyn.errors import TynError
from tyn.retry import RetryPolicy, parse_retry_after

logger = logging.getLogger(__name__)

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


class ModelError(TynError):
    """The model request failed and should not be retried as is."""


class ModelTransientError(ModelError):
    """A timeout, connection problem or 5xx; safe to retry."""


class ModelRateLimitedError(ModelError):
    """HTTP 429 from the provider."""

    def __init__(self, retry_after: float | None) -> None:
        self.retry_after = retry_after
        super().__init__("model provider rate limit (429)")


class ContextTooLargeError(ModelError):
    """HTTP 413: the request is too large; prune context and retry once."""


class ToolCallGenerationError(ModelError):
    """The provider rejected the model's own tool call (e.g. malformed arguments)."""


class RateLimitWaitTooLongError(TynError):
    """The provider asked for a wait longer than the run will block for."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        super().__init__(
            f"model provider asks to wait {seconds:.0f}s; progress is saved, resume later "
            "with --resume"
        )


@dataclass(frozen=True)
class ToolCall:
    """A tool call requested by the model."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ChatResponse:
    """The parts of a completion the agent uses."""

    content: str | None
    tool_calls: list[ToolCall]
    usage: TokenUsage
    headers: dict[str, str] = field(default_factory=dict)
    finish_reason: str | None = None
    waited_seconds: float = 0.0


class ChatModel(Protocol):
    """One chat completion request."""

    @property
    def model_name(self) -> str:
        """The provider's model id."""
        ...

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: str | dict[str, Any],
        max_completion_tokens: int,
    ) -> ChatResponse:
        """Send one request and return the parsed response."""
        ...


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
            "messages": messages,
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
            raise _status_error(exc) from None
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


def _status_error(exc: groq.APIStatusError) -> ModelError:
    status = exc.status_code
    code = ""
    body = exc.body if isinstance(exc.body, dict) else {}
    error = body.get("error", body) if isinstance(body, dict) else {}
    if isinstance(error, dict):
        code = str(error.get("code") or "")
    if status == 413:
        return ContextTooLargeError("request too large for the model (413)")
    if status >= 500:
        return ModelTransientError(f"model provider error HTTP {status}")
    if status == 400 and code in _GENERATION_ERRORS:
        return ToolCallGenerationError(f"the model produced output the provider rejected ({code})")
    return ModelError(f"model request rejected: HTTP {status} {code}".strip())


@dataclass
class ModelStats:
    """Counters reported in metrics."""

    calls: int = 0
    retries: int = 0
    rate_limit_waits: int = 0
    rate_limit_wait_seconds: float = 0.0
    backoff_seconds: float = 0.0
    usage: TokenUsage = field(default_factory=TokenUsage)


class ResilientModel:
    """Pacing, daily budget, bounded retries and accounting around a model."""

    def __init__(
        self,
        inner: ChatModel,
        *,
        pacer: TokenPacer,
        daily: DailyBudget,
        estimator: TokenEstimator,
        policy: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._inner = inner
        self._pacer = pacer
        self._daily = daily
        self._estimator = estimator
        self._policy = policy or RetryPolicy(max_attempts=4, base_delay=2.0, max_delay=30.0)
        self._sleep = sleep
        self._rng = rng or random.Random()  # noqa: S311 - jitter, not security
        self.stats = ModelStats()

    @property
    def model_name(self) -> str:
        """Return the underlying model id."""
        return self._inner.model_name

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: str | dict[str, Any],
        max_completion_tokens: int,
    ) -> ChatResponse:
        """Send one logical request with pacing and bounded retries."""
        raw_estimate = self._estimator.raw_request(messages, tools)
        estimate = self._estimator.estimate(messages, tools)
        reserve = estimate + max_completion_tokens
        self._daily.check(reserve)
        last_error: ModelError | None = None
        waited_before = self._waited_total()
        for attempt in range(1, self._policy.max_attempts + 1):
            charge = self._pacer.acquire(reserve)
            try:
                response = self._inner.complete(messages, tools, tool_choice, max_completion_tokens)
            except ContextTooLargeError:
                self._pacer.settle(charge, 0)
                raise
            except ModelRateLimitedError as exc:
                self._wait_after_rate_limit(attempt, exc.retry_after)
                last_error = exc
                continue
            except (ModelTransientError, ToolCallGenerationError) as exc:
                self._pacer.settle(charge, estimate)
                last_error = exc
                if attempt < self._policy.max_attempts:
                    self._backoff(attempt, exc)
                continue
            self._pacer.settle(charge, response.usage.rate_limited)
            self._pacer.observe_remaining(
                _int_header(response.headers, "x-ratelimit-remaining-tokens"),
                _int_header(response.headers, "x-ratelimit-limit-tokens"),
            )
            self._daily.record(response.usage)
            self._estimator.calibrate(raw_estimate, response.usage.prompt)
            self.stats.calls += 1
            self.stats.usage = self.stats.usage.add(response.usage)
            return replace(response, waited_seconds=round(self._waited_total() - waited_before, 3))
        raise ModelError(
            f"model request failed after {self._policy.max_attempts} attempts: {last_error}"
        )

    def _waited_total(self) -> float:
        return (
            self._pacer.stats.slept_seconds
            + self.stats.rate_limit_wait_seconds
            + self.stats.backoff_seconds
        )

    def _wait_after_rate_limit(self, attempt: int, retry_after: float | None) -> None:
        if retry_after is not None and retry_after > self._policy.max_retry_after:
            raise RateLimitWaitTooLongError(retry_after)
        wait = self._policy.delay(attempt, self._rng, retry_after)
        self.stats.rate_limit_waits += 1
        self.stats.rate_limit_wait_seconds += wait
        self.stats.retries += 1
        logger.warning("model_rate_limited attempt=%d wait_s=%.1f", attempt, wait)
        self._sleep(wait)

    def _backoff(self, attempt: int, exc: ModelError) -> None:
        wait = self._policy.backoff(attempt, self._rng)
        self.stats.retries += 1
        self.stats.backoff_seconds += wait
        logger.warning("model_retry attempt=%d reason=%s wait_s=%.1f", attempt, exc, wait)
        self._sleep(wait)


def _int_header(headers: dict[str, str], name: str) -> int | None:
    try:
        return int(headers[name])
    except (KeyError, ValueError):
        return None
