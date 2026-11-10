"""Assessment checkpoints in ``state/assessments/``.

Each finished requirement is saved with its trace slice immediately, so an
interrupted or budget-limited run resumes with ``--resume`` and skips
completed requirements. Each process run appends a session record with its
own counters, so metrics stay honest across resumes.
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tyn.agent.models import Discovery, RequirementResult, TokenUsage
from tyn.errors import StateError
from tyn.paths import STATE_DIR, atomic_write_text, safe_child

_UNSAFE: re.Pattern[str] = re.compile(r"[^A-Za-z0-9._-]+")


class SessionStats(BaseModel):
    """Counters for one process run of ``assess``."""

    model_config = ConfigDict(extra="forbid")

    started_at: str
    seconds: float = 0.0
    model_calls: int = 0
    model_retries: int = 0
    rate_limit_waits: int = 0
    rate_limit_wait_seconds: float = 0.0
    pacer_sleeps: int = 0
    pacer_sleep_seconds: float = 0.0
    jira_calls: int = 0
    usage: TokenUsage = Field(default_factory=TokenUsage)
    stopped_reason: str | None = None


@dataclass(frozen=True)
class Provenance:
    """What produced a checkpoint's results; resuming requires an exact match."""

    provider: str
    model: str
    prompt_hash: str

    def describe(self) -> str:
        """Return a one-line description for messages."""
        return f"provider {self.provider}, model {self.model}, prompt hash {self.prompt_hash}"


class Checkpoint(BaseModel):
    """Everything needed to resume and to report an assessment."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[2] = 2
    project: str
    label: str
    release: str
    provider: str
    model: str
    prompt_hash: str
    started_at: str
    updated_at: str
    discovery: Discovery | None = None
    results: dict[str, RequirementResult] = Field(default_factory=dict)
    traces: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    sessions: list[SessionStats] = Field(default_factory=list)


def now_iso() -> str:
    """Return the current UTC time as an ISO string."""
    return datetime.now(UTC).isoformat(timespec="seconds")


class CheckpointStore:
    """Load and save the checkpoint for one project, label and release."""

    def __init__(self, project: str, label: str, release: str, root: Path = STATE_DIR) -> None:
        name = "__".join(_UNSAFE.sub("_", part) for part in (project, label, release))
        self.path = safe_child(root / "assessments", f"{name}.json")
        self._identity = (project, label, release)

    def new(self, provenance: Provenance) -> Checkpoint:
        """Return an empty checkpoint, keeping any previous one as a backup."""
        if self.path.exists():
            self.path.replace(self.path.with_suffix(".previous.json"))
        project, label, release = self._identity
        stamp = now_iso()
        return Checkpoint(
            project=project,
            label=label,
            release=release,
            provider=provenance.provider,
            model=provenance.model,
            prompt_hash=provenance.prompt_hash,
            started_at=stamp,
            updated_at=stamp,
        )

    def load(self, expected: Provenance | None) -> Checkpoint | None:
        """Return the saved checkpoint, or None when there is none.

        When ``expected`` is given, a checkpoint produced by another provider,
        model or prompt version is refused, so a resumed report never mixes
        results from different runs. Re-rendering passes None.
        """
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise StateError(
                f"{self.path.as_posix()} is unreadable; run without --resume to start over"
            ) from None
        if not isinstance(payload, dict) or payload.get("version") != 2:
            raise StateError(
                f"{self.path.as_posix()} was written by an older version without provider, "
                "model and prompt provenance; run without --resume to start a fresh assessment"
            )
        try:
            checkpoint = Checkpoint.model_validate(payload)
        except ValidationError:
            raise StateError(
                f"{self.path.as_posix()} is unreadable; run without --resume to start over"
            ) from None
        if (checkpoint.project, checkpoint.label, checkpoint.release) != self._identity:
            raise StateError(f"{self.path.as_posix()} belongs to another assessment")
        saved = Provenance(checkpoint.provider, checkpoint.model, checkpoint.prompt_hash)
        if expected is not None and saved != expected:
            raise StateError(
                f"the saved assessment was produced by {saved.describe()}, but this run uses "
                f"{expected.describe()}; refusing to mix them. Run without --resume and "
                "--only to start a fresh assessment"
            )
        return checkpoint

    def save(self, checkpoint: Checkpoint) -> None:
        """Persist the checkpoint atomically."""
        checkpoint.updated_at = now_iso()
        atomic_write_text(self.path, checkpoint.model_dump_json(indent=1) + "\n")
