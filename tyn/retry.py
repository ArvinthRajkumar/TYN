"""Bounded exponential backoff shared by the Jira and Groq clients."""

import random
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


@dataclass(frozen=True)
class RetryPolicy:
    """Limits for one logical request.

    Attributes:
        max_attempts: Total tries including the first one.
        base_delay: Delay before the second try, in seconds.
        max_delay: Cap for the exponential part of the delay.
        max_retry_after: Longest server-requested wait honoured before giving
            up; longer waits are reported instead of blocking the run.
        jitter_low: Lower multiplier applied to the backoff delay.
        jitter_high: Upper multiplier applied to the backoff delay.
    """

    max_attempts: int = 5
    base_delay: float = 2.0
    max_delay: float = 30.0
    max_retry_after: float = 120.0
    jitter_low: float = 0.7
    jitter_high: float = 1.3

    def backoff(self, attempt: int, rng: random.Random) -> float:
        """Return the jittered delay after failed try number ``attempt`` (1-based)."""
        raw = min(self.max_delay, self.base_delay * (2.0 ** (attempt - 1)))
        return raw * rng.uniform(self.jitter_low, self.jitter_high)

    def delay(self, attempt: int, rng: random.Random, retry_after: float | None) -> float:
        """Return the wait before the next try, never shorter than ``retry_after``."""
        computed = self.backoff(attempt, rng)
        return max(computed, retry_after) if retry_after is not None else computed


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Parse a ``Retry-After`` header given as seconds or as an HTTP date."""
    if value is None or not value.strip():
        return None
    text = value.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    return max(0.0, (when - current).total_seconds())
