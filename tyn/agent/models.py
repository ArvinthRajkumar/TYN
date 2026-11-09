"""Data shapes shared by the agent, checkpoints and reports."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Decision(StrEnum):
    """Per-requirement decision required by the brief."""

    PASS = "PASS"  # noqa: S105 - a decision value, not a password
    FAIL = "FAIL"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class Overall(StrEnum):
    """Release-level decision, always computed in code."""

    READY = "READY"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    BLOCKED = "BLOCKED"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceRef(_Model):
    """A citation: an issue and, when the fact sits in a comment, its id."""

    issue_key: str = Field(max_length=32)
    comment_id: str | None = Field(default=None, max_length=32)
    note: str = Field(default="", max_length=400)


class Observation(_Model):
    """Text in a ticket that tried to instruct the reviewer."""

    issue_key: str = Field(max_length=32)
    comment_id: str | None = Field(default=None, max_length=32)
    summary: str = Field(max_length=400)
    source: Literal["model", "detector"] = "model"


class Verdict(_Model):
    """What the model submits for one requirement."""

    decision: Decision
    explanation: str = Field(max_length=2000)
    supporting_evidence: list[EvidenceRef] = Field(default_factory=list, max_length=12)
    rejected_evidence: list[EvidenceRef] = Field(default_factory=list, max_length=12)
    outstanding: list[str] = Field(default_factory=list, max_length=8)
    observations: list[Observation] = Field(default_factory=list, max_length=6)


class TokenUsage(_Model):
    """Token counts reported by the provider (zero when not reported)."""

    prompt: int = 0
    completion: int = 0
    reasoning: int = 0
    cached: int = 0

    def add(self, other: "TokenUsage") -> "TokenUsage":
        """Return the element-wise sum."""
        return TokenUsage(
            prompt=self.prompt + other.prompt,
            completion=self.completion + other.completion,
            reasoning=self.reasoning + other.reasoning,
            cached=self.cached + other.cached,
        )

    @property
    def total(self) -> int:
        """Prompt plus completion tokens."""
        return self.prompt + self.completion


class RequirementTarget(_Model):
    """A requirement the discovery stage selected."""

    issue_key: str
    source_id: str | None = None
    summary: str = ""
    mandatory: bool = True
    mandatory_source: str = Field(
        default="model", description="model, record, or model+record when both agree"
    )
    note: str = ""


class RequirementResult(_Model):
    """The final, validated outcome for one requirement."""

    target: RequirementTarget
    verdict: Verdict
    status: Literal["completed", "failed"] = "completed"
    failure: str | None = None
    validation_notes: list[str] = Field(default_factory=list)
    seconds: float = 0.0
    model_calls: int = 0
    tool_calls: int = 0
    usage: TokenUsage = Field(default_factory=TokenUsage)


class Discovery(_Model):
    """Output of the discovery stage after code-side cross-checks."""

    manifest_key: str | None
    manifest_rules: str = ""
    requirements: list[RequirementTarget]
    notes: list[str] = Field(default_factory=list)
    seconds: float = 0.0
    model_calls: int = 0
    tool_calls: int = 0
    usage: TokenUsage = Field(default_factory=TokenUsage)
