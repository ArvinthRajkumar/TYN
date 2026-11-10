"""Client-side rate limiting for the model provider.

``TokenPacer`` keeps a sliding 60 second window of token charges, and
optionally of requests, and sleeps before a request that would exceed either
per-minute limit, so the run waits instead of collecting 429s. What a
request reserves depends on the provider (see ``ResilientModel``): Groq
counts output toward its minute limit, so the completion cap is reserved
too; Gemini counts input only. Charges are settled to real usage afterwards.

``DailyBudget`` persists usage per UTC day in ``state/usage.json`` and, when
a daily limit is configured, stops the run cleanly before it would be crossed.
"""

import json
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, Field, ValidationError, model_validator

from tyn.agent.models import TokenUsage
from tyn.errors import TynError
from tyn.paths import atomic_write_text

WINDOW_SECONDS: Final = 60.0
_MAX_WAIT_LOOPS: Final = 1_000


class BudgetError(TynError):
    """A single request can never fit the configured limits."""


class DailyBudgetExhaustedError(TynError):
    """The daily token limit would be exceeded; resume later."""

    def __init__(self, used: int, limit: int, resume_at: datetime) -> None:
        self.used = used
        self.limit = limit
        self.resume_at = resume_at
        super().__init__(
            f"daily token budget reached ({used} of {limit} used today); "
            f"progress is saved, resume after {resume_at.strftime('%Y-%m-%d %H:%M UTC')} "
            "with --resume"
        )


@dataclass
class _Charge:
    at: float
    tokens: int


@dataclass
class PacerStats:
    """Counters reported in metrics."""

    sleeps: int = 0
    slept_seconds: float = 0.0


class TokenPacer:
    """Sliding-window token budget, and optionally a request budget, for one model."""

    def __init__(
        self,
        tokens_per_minute: int,
        *,
        requests_per_minute: int | None = None,
        headroom: float = 0.9,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.capacity = int(tokens_per_minute * headroom)
        self.request_capacity = (
            max(1, int(requests_per_minute * headroom)) if requests_per_minute else None
        )
        self._monotonic = monotonic
        self._sleep = sleep
        self._charges: deque[_Charge] = deque()
        self._requests: deque[float] = deque()
        self.stats = PacerStats()

    def used(self) -> int:
        """Return tokens charged inside the current window."""
        self._expire()
        return sum(charge.tokens for charge in self._charges)

    def acquire(self, tokens: int) -> _Charge:
        """Wait until ``tokens`` fit in the window, then reserve them."""
        if tokens > self.capacity:
            raise BudgetError(
                f"request needs about {tokens} tokens but the per-minute budget is "
                f"{self.capacity}; lower the input budget or completion cap"
            )
        for _ in range(_MAX_WAIT_LOOPS):
            self._expire()
            used = sum(charge.tokens for charge in self._charges)
            tokens_fit = used + tokens <= self.capacity
            requests_fit = (
                self.request_capacity is None or len(self._requests) < self.request_capacity
            )
            if tokens_fit and requests_fit:
                now = self._monotonic()
                charge = _Charge(now, tokens)
                self._charges.append(charge)
                if self.request_capacity is not None:
                    self._requests.append(now)
                return charge
            wait = 0.0 if tokens_fit else self._wait_for(used + tokens - self.capacity)
            if not requests_fit:
                wait = max(wait, self._requests[0] + WINDOW_SECONDS - self._monotonic() + 0.05)
            wait = max(0.05, wait)
            self.stats.sleeps += 1
            self.stats.slept_seconds += wait
            self._sleep(wait)
        raise BudgetError("token pacer could not make room; the clock may be stalled")

    def settle(self, charge: _Charge, actual_tokens: int) -> None:
        """Replace a reservation with the tokens actually charged."""
        charge.tokens = max(0, actual_tokens)

    def penalise(self, tokens: int) -> None:
        """Record tokens the provider says were used that we did not track."""
        if tokens > 0:
            self._charges.append(_Charge(self._monotonic(), tokens))

    def observe_remaining(self, remaining: int | None, limit: int | None) -> None:
        """Align with the provider's view when it reports less headroom than we model."""
        if remaining is None or limit is None:
            return
        provider_used = limit - remaining
        self.penalise(provider_used - self.used())

    def _expire(self) -> None:
        cutoff = self._monotonic() - WINDOW_SECONDS
        while self._charges and self._charges[0].at <= cutoff:
            self._charges.popleft()
        while self._requests and self._requests[0] <= cutoff:
            self._requests.popleft()

    def _wait_for(self, excess: int) -> float:
        freed = 0
        now = self._monotonic()
        for charge in self._charges:
            freed += charge.tokens
            if freed >= excess:
                return max(0.05, charge.at + WINDOW_SECONDS - now + 0.05)
        return WINDOW_SECONDS


class _DayUsage(BaseModel):
    prompt: int = 0
    completion: int = 0
    reasoning: int = 0
    cached: int = 0
    total: int = 0
    counted: int = 0

    @model_validator(mode="before")
    @classmethod
    def _count_legacy_totals(cls, data: Any) -> Any:
        # Entries written before cached tokens were tracked count in full,
        # which errs toward stopping early rather than hitting the limit.
        if isinstance(data, dict) and "counted" not in data:
            return {**data, "counted": data.get("total", 0)}
        return data


class _UsageFile(BaseModel):
    version: int = 1
    days: dict[str, dict[str, _DayUsage]] = Field(default_factory=dict)


class DailyBudget:
    """Daily token accounting persisted in ``state/usage.json``.

    Usage is always recorded. A cap is enforced only when ``limit`` is set
    (Groq's free tier has one; Gemini has none unless configured). Providers
    do not document whether daily windows are calendar days or rolling, so a
    UTC calendar day is assumed, and a provider's own daily-quota 429 or long
    ``retry-after`` stops the run as well.
    """

    def __init__(
        self,
        path: Path,
        model: str,
        limit: int | None,
        *,
        today: Callable[[], date] = lambda: datetime.now(UTC).date(),
    ) -> None:
        self._path = path
        self._model = model
        self._limit = limit
        self._today = today
        self._data = self._load()

    def _load(self) -> _UsageFile:
        if not self._path.exists():
            return _UsageFile()
        try:
            return _UsageFile.model_validate(json.loads(self._path.read_text(encoding="utf-8")))
        except (OSError, ValueError, ValidationError):
            raise TynError(
                f"{self._path.as_posix()} is unreadable; delete it only if you are sure "
                "no tokens were used today"
            ) from None

    def _entry(self) -> _DayUsage:
        day = self._data.days.setdefault(self._today().isoformat(), {})
        return day.setdefault(self._model, _DayUsage())

    def used_today(self) -> int:
        """Return tokens counted toward the daily limit today (cached excluded)."""
        return self._entry().counted

    def check(self, upcoming: int) -> None:
        """Raise if ``upcoming`` more tokens would exceed today's limit, if one is set."""
        if self._limit is None:
            return
        used = self.used_today()
        if used + upcoming > self._limit:
            tomorrow = datetime.combine(self._today() + timedelta(days=1), datetime.min.time(), UTC)
            raise DailyBudgetExhaustedError(used, self._limit, tomorrow)

    def record(self, usage: TokenUsage) -> None:
        """Add one response's usage and persist it."""
        entry = self._entry()
        entry.prompt += usage.prompt
        entry.completion += usage.completion
        entry.reasoning += usage.reasoning
        entry.cached += usage.cached
        entry.total += usage.total
        entry.counted += usage.rate_limited
        atomic_write_text(self._path, self._data.model_dump_json(indent=2) + "\n")
