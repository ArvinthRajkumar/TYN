"""Token pacing, daily budget, resilient model retries and argument parsing."""

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from tyn.agent.arguments import ArgumentError, parse_arguments
from tyn.agent.budget import TokenEstimator
from tyn.agent.llm import (
    ChatResponse,
    ContextTooLargeError,
    ModelError,
    ModelRateLimitedError,
    ModelTransientError,
    RateLimitWaitTooLongError,
    ResilientModel,
    ToolCallGenerationError,
)
from tyn.agent.models import TokenUsage
from tyn.agent.pacer import BudgetError, DailyBudget, DailyBudgetExhaustedError, TokenPacer


class FakeClock:
    """Deterministic monotonic clock whose sleep advances time."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_pacer_sleeps_until_the_window_frees_capacity() -> None:
    clock = FakeClock()
    pacer = TokenPacer(1_000, headroom=1.0, monotonic=clock.monotonic, sleep=clock.sleep)
    pacer.acquire(600)
    clock.now += 10
    pacer.acquire(600)
    assert len(clock.sleeps) == 1
    assert clock.sleeps[0] == pytest.approx(50.05, abs=0.01)
    assert pacer.stats.sleeps == 1


def test_pacer_settles_to_actual_usage() -> None:
    clock = FakeClock()
    pacer = TokenPacer(1_000, headroom=1.0, monotonic=clock.monotonic, sleep=clock.sleep)
    charge = pacer.acquire(900)
    pacer.settle(charge, 100)
    pacer.acquire(800)
    assert clock.sleeps == []
    assert pacer.used() == 900


def test_pacer_adopts_provider_view_when_it_is_tighter() -> None:
    clock = FakeClock()
    pacer = TokenPacer(1_000, headroom=1.0, monotonic=clock.monotonic, sleep=clock.sleep)
    pacer.observe_remaining(remaining=300, limit=1_000)
    assert pacer.used() == 700
    pacer.observe_remaining(remaining=900, limit=1_000)
    assert pacer.used() == 700


def test_pacer_rejects_requests_that_can_never_fit() -> None:
    with pytest.raises(BudgetError, match="per-minute budget"):
        TokenPacer(1_000).acquire(5_000)


def test_daily_budget_persists_and_stops_before_the_limit(tmp_path: Path) -> None:
    path = tmp_path / "state" / "usage.json"
    budget = DailyBudget(path, "m", 1_000, today=lambda: date(2026, 1, 5))
    budget.record(TokenUsage(prompt=600, completion=100, reasoning=20))
    reloaded = DailyBudget(path, "m", 1_000, today=lambda: date(2026, 1, 5))
    assert reloaded.used_today() == 700
    with pytest.raises(DailyBudgetExhaustedError) as caught:
        reloaded.check(400)
    assert caught.value.resume_at.isoformat().startswith("2026-01-06T00:00")
    assert "--resume" in str(caught.value)
    tomorrow = DailyBudget(path, "m", 1_000, today=lambda: date(2026, 1, 6))
    tomorrow.check(900)
    assert (
        json.loads(path.read_text(encoding="utf-8"))["days"]["2026-01-05"]["m"]["reasoning"] == 20
    )


class FlakyModel:
    """A model that fails with the given errors before answering."""

    model_name = "flaky"

    def __init__(self, failures: list[Exception]) -> None:
        self.failures = failures
        self.calls = 0

    def complete(self, *args: Any, **kwargs: Any) -> ChatResponse:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return ChatResponse(
            content="ok",
            tool_calls=[],
            usage=TokenUsage(prompt=120, completion=30, reasoning=7, cached=0),
            headers={"x-ratelimit-remaining-tokens": "7000", "x-ratelimit-limit-tokens": "8000"},
        )


def _resilient(
    model: FlakyModel, tmp_path: Path, limit: int = 100_000
) -> tuple[ResilientModel, FakeClock]:
    clock = FakeClock()
    resilient = ResilientModel(
        model,
        pacer=TokenPacer(8_000, monotonic=clock.monotonic, sleep=clock.sleep),
        daily=DailyBudget(tmp_path / "usage.json", "flaky", limit),
        estimator=TokenEstimator(),
        sleep=clock.sleep,
    )
    return resilient, clock


MESSAGES = [{"role": "user", "content": "x" * 400}]


def test_rate_limit_honours_retry_after(tmp_path: Path) -> None:
    model = FlakyModel([ModelRateLimitedError(retry_after=7.0)])
    resilient, clock = _resilient(model, tmp_path)
    response = resilient.complete(MESSAGES, [], "auto", 100)
    assert response.content == "ok"
    assert model.calls == 2
    assert clock.sleeps[0] >= 7.0
    assert resilient.stats.rate_limit_waits == 1
    assert resilient.stats.usage.reasoning == 7


def test_rate_limit_with_long_wait_stops_cleanly(tmp_path: Path) -> None:
    resilient, _ = _resilient(FlakyModel([ModelRateLimitedError(retry_after=3_600)]), tmp_path)
    with pytest.raises(RateLimitWaitTooLongError, match="resume later"):
        resilient.complete(MESSAGES, [], "auto", 100)


@pytest.mark.parametrize(
    "failure", [ModelTransientError("timeout"), ToolCallGenerationError("bad tool call")]
)
def test_transient_failures_are_retried_with_backoff(tmp_path: Path, failure: Exception) -> None:
    model = FlakyModel([failure, failure])
    resilient, clock = _resilient(model, tmp_path)
    resilient.complete(MESSAGES, [], "auto", 100)
    assert model.calls == 3
    assert len(clock.sleeps) == 2
    assert resilient.stats.retries == 2


def test_retries_are_bounded(tmp_path: Path) -> None:
    model = FlakyModel([ModelTransientError("down")] * 10)
    resilient, _ = _resilient(model, tmp_path)
    with pytest.raises(ModelError, match="after 4 attempts"):
        resilient.complete(MESSAGES, [], "auto", 100)
    assert model.calls == 4


def test_context_too_large_is_raised_for_the_caller_to_prune(tmp_path: Path) -> None:
    resilient, _ = _resilient(FlakyModel([ContextTooLargeError("413")]), tmp_path)
    with pytest.raises(ContextTooLargeError):
        resilient.complete(MESSAGES, [], "auto", 100)


def test_daily_budget_is_checked_before_calling(tmp_path: Path) -> None:
    model = FlakyModel([])
    resilient, _ = _resilient(model, tmp_path, limit=150)
    with pytest.raises(DailyBudgetExhaustedError):
        resilient.complete(MESSAGES, [], "auto", 100)
    assert model.calls == 0


def test_estimator_calibrates_toward_reported_usage() -> None:
    estimator = TokenEstimator()
    messages = [{"role": "user", "content": "x" * 4_000}]
    raw = estimator.raw_request(messages, [])
    for _ in range(20):
        estimator.calibrate(raw, int(raw * 1.5))
    assert estimator.factor == pytest.approx(1.5, rel=0.02)
    estimator.calibrate(raw, int(raw * 100))
    assert estimator.factor <= 2.5


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"issue_key": "TYN-1"}', {"issue_key": "TYN-1"}),
        ('```json\n{"issue_key": "TYN-1"}\n```', {"issue_key": "TYN-1"}),
        ('{"issue_key": "TYN-1",}', {"issue_key": "TYN-1"}),
        ('Sure: {"a": 1} done', {"a": 1}),
        ("", {}),
    ],
)
def test_arguments_are_parsed_tolerantly(raw: str, expected: dict[str, Any]) -> None:
    assert parse_arguments(raw) == expected


@pytest.mark.parametrize("raw", ["{not json", "[1, 2]", '"text"'])
def test_unparseable_arguments_raise_a_short_error(raw: str) -> None:
    with pytest.raises(ArgumentError):
        parse_arguments(raw)


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (400, "output_parse_failed", ToolCallGenerationError),
        (400, "tool_use_failed", ToolCallGenerationError),
        (413, "", ContextTooLargeError),
        (503, "", ModelTransientError),
        (401, "invalid_api_key", ModelError),
    ],
)
def test_provider_status_errors_are_classified(status: int, code: str, expected: type) -> None:
    import groq
    import httpx

    from tyn.agent.llm import _status_error

    response = httpx.Response(status, request=httpx.Request("POST", "https://api.example/v1"))
    error = groq.APIStatusError("x", response=response, body={"error": {"code": code}})
    assert type(_status_error(error)) is expected


def test_waits_are_reported_separately_from_latency(tmp_path: Path) -> None:
    model = FlakyModel([ModelRateLimitedError(retry_after=5.0)])
    resilient, _ = _resilient(model, tmp_path)
    response = resilient.complete(MESSAGES, [], "auto", 100)
    assert response.waited_seconds >= 5.0
