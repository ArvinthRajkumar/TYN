"""Safe output paths, atomic writes and retry timing."""

import random
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tyn.errors import UnsafePathError
from tyn.paths import atomic_write_text, safe_child
from tyn.retry import RetryPolicy, parse_retry_after


@pytest.mark.parametrize("name", ["../escape.json", "..", "a/../../b", "/abs/path"])
def test_safe_child_refuses_escape(tmp_path: Path, name: str) -> None:
    with pytest.raises(UnsafePathError):
        safe_child(tmp_path / "state", name)


def test_safe_child_allows_nested(tmp_path: Path) -> None:
    path = safe_child(tmp_path / "reports", "raw/trace.jsonl")
    assert path.is_relative_to((tmp_path / "reports").resolve())


def test_atomic_write_uses_utf8_and_lf(tmp_path: Path) -> None:
    target = tmp_path / "state" / "x.json"
    atomic_write_text(target, "café\nline\n")
    assert target.read_bytes() == "café\nline\n".encode()
    assert list(target.parent.iterdir()) == [target]


def test_retry_after_parsing() -> None:
    now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after("2.5") == 2.5
    assert parse_retry_after("-3") == 0.0
    assert parse_retry_after("Thu, 01 Jan 2026 00:00:10 GMT", now=now) == 10.0
    assert parse_retry_after("soon") is None
    assert parse_retry_after(None) is None
    assert parse_retry_after("  ") is None


def test_backoff_is_bounded_and_jittered() -> None:
    policy = RetryPolicy(base_delay=2.0, max_delay=30.0)
    rng = random.Random(0)
    delays = [policy.backoff(attempt, rng) for attempt in range(1, 10)]
    assert all(0.7 * 2.0 <= d <= 1.3 * 30.0 for d in delays)
    assert delays[-1] <= 30.0 * 1.3


def test_delay_never_shorter_than_retry_after() -> None:
    policy = RetryPolicy(base_delay=1.0)
    assert policy.delay(1, random.Random(0), retry_after=12.0) == 12.0
