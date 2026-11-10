"""Provider-neutral model access: shared types, errors and a resilient wrapper.

Provider adapters (``tyn.agent.providers``) perform exactly one request and
map SDK errors to the exceptions below. ``ResilientModel`` adds pacing, the
optional daily budget, the run-wide token cap, bounded retries with backoff
and jitter, and usage bookkeeping. The agent depends only on the
``ChatModel`` protocol, so tests inject scripted models.

Messages use one neutral shape (``system``, ``user``, ``assistant`` with
``tool_calls``, and ``tool`` results with ``tool_call_id`` and ``name``).
Keys starting with an underscore are private to an adapter: the assistant
message carries the provider's own response under ``_provider_content`` so
an adapter that must replay the model's turn verbatim (for example to return
Gemini thought signatures) can do so.
"""

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from tyn.agent.budget import TokenEstimator
from tyn.agent.models import TokenUsage
from tyn.agent.pacer import DailyBudget, TokenPacer
from tyn.errors import TynError
from tyn.retry import RetryPolicy

logger = logging.getLogger(__name__)

PROVIDER_CONTENT_KEY = "_provider_content"


class ModelError(TynError):
    """The model request failed and should not be retried as is."""


class ModelTransientError(ModelError):
    """A timeout, connection problem or 5xx; safe to retry."""


class ModelRateLimitedError(ModelError):
    """HTTP 429 from the provider."""

    def __init__(self, retry_after: float | None, *, daily_quota: bool = False) -> None:
        self.retry_after = retry_after
        self.daily_quota = daily_quota
        kind = "daily quota" if daily_quota else "rate limit"
        super().__init__(f"model provider {kind} (429)")


class ContextTooLargeError(ModelError):
    """The request is too large; prune context and retry once."""


class ToolCallGenerationError(ModelError):
    """The provider rejected the model's own tool call (e.g. malformed arguments)."""


class RateLimitWaitTooLongError(TynError):
    """The provider asked for a wait longer than the run will block for."""

    def __init__(self, seconds: float | None, *, daily_quota: bool = False) -> None:
        self.seconds = seconds
        if daily_quota:
            detail = "the provider's daily quota is exhausted"
        else:
            detail = f"the model provider asks to wait {seconds:.0f}s"
        super().__init__(f"{detail}; progress is saved, resume later with --resume")


class PrepaidCreditsDepletedError(TynError):
    """HTTP 402: the provider's prepaid balance is used up; retrying cannot help."""

    def __init__(self) -> None:
        super().__init__(
            "the model provider's prepaid credit balance is depleted (HTTP 402); add credits, "
            "then resume with --resume"
        )


class RunTokenCapReachedError(TynError):
    """The next request would exceed MAX_RUN_TOKENS for this invocation."""

    def __init__(self, used: int, cap: int) -> None:
        self.used = used
        self.cap = cap
        super().__init__(
            f"MAX_RUN_TOKENS reached ({used} of {cap} tokens used in this run); progress is "
            "saved, resume with --resume or raise the cap"
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
    provider_content: Any = None


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


def public_message(message: dict[str, Any]) -> dict[str, Any]:
    """Return a message without adapter-private keys."""
    return {key: value for key, value in message.items() if not key.startswith("_")}


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
    """Pacing, budgets, bounded retries and accounting around a model.

    Args:
        inner: The provider adapter.
        pacer: Per-minute token (and optionally request) window.
        daily: Daily usage ledger; it only enforces when a limit is set.
        estimator: Prompt token estimator, calibrated against usage.
        reserve_output: Whether the provider counts output tokens toward its
            per-minute token limit (Groq does; Gemini counts input only).
        max_run_tokens: Hard cap on prompt plus output tokens for this
            invocation; the request that would cross it is never sent.
    """

    def __init__(
        self,
        inner: ChatModel,
        *,
        pacer: TokenPacer,
        daily: DailyBudget | None,
        estimator: TokenEstimator,
        reserve_output: bool = True,
        max_run_tokens: int | None = None,
        policy: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._inner = inner
        self._pacer = pacer
        self._daily = daily
        self._estimator = estimator
        self._reserve_output = reserve_output
        self._max_run_tokens = max_run_tokens
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
        """Send one logical request with pacing, budgets and bounded retries."""
        raw_estimate = self._estimator.raw_request(messages, tools)
        estimate = self._estimator.estimate(messages, tools)
        worst_case = estimate + max_completion_tokens
        self._check_run_cap(worst_case)
        if self._daily is not None:
            self._daily.check(worst_case)
        reserve = worst_case if self._reserve_output else estimate
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
                self._wait_after_rate_limit(attempt, exc)
                last_error = exc
                continue
            except (ModelTransientError, ToolCallGenerationError) as exc:
                self._pacer.settle(charge, estimate)
                last_error = exc
                if attempt < self._policy.max_attempts:
                    self._backoff(attempt, exc)
                continue
            self._account(charge, response, raw_estimate)
            return replace(response, waited_seconds=round(self._waited_total() - waited_before, 3))
        message = f"model request failed after {self._policy.max_attempts} attempts: {last_error}"
        if isinstance(last_error, ToolCallGenerationError):
            raise ToolCallGenerationError(message)
        raise ModelError(message)

    def _check_run_cap(self, worst_case: int) -> None:
        if self._max_run_tokens is None:
            return
        used = self.stats.usage.total
        if used + worst_case > self._max_run_tokens:
            raise RunTokenCapReachedError(used, self._max_run_tokens)

    def _account(self, charge: Any, response: ChatResponse, raw_estimate: float) -> None:
        usage = response.usage
        self._pacer.settle(charge, usage.rate_limited if self._reserve_output else usage.prompt)
        self._pacer.observe_remaining(
            _int_header(response.headers, "x-ratelimit-remaining-tokens"),
            _int_header(response.headers, "x-ratelimit-limit-tokens"),
        )
        if self._daily is not None:
            self._daily.record(usage)
        self._estimator.calibrate(raw_estimate, usage.prompt)
        self.stats.calls += 1
        self.stats.usage = self.stats.usage.add(usage)

    def _waited_total(self) -> float:
        return (
            self._pacer.stats.slept_seconds
            + self.stats.rate_limit_wait_seconds
            + self.stats.backoff_seconds
        )

    def _wait_after_rate_limit(self, attempt: int, exc: ModelRateLimitedError) -> None:
        if exc.daily_quota:
            raise RateLimitWaitTooLongError(exc.retry_after, daily_quota=True)
        retry_after = exc.retry_after
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
