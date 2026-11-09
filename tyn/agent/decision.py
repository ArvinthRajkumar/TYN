"""Deterministic overall release decision.

The model never decides the release outcome. Rules from the brief:

* BLOCKED if any mandatory requirement is FAIL;
* otherwise NEEDS_REVIEW if any mandatory requirement is INSUFFICIENT_EVIDENCE;
* READY only if every mandatory requirement is PASS.

Assumptions documented in the README: non-mandatory requirements are
reported but do not change the outcome; a run with no mandatory requirements
or with unassessed mandatory requirements cannot be READY.
"""

from dataclasses import dataclass

from tyn.agent.models import Decision, Overall, RequirementResult


@dataclass(frozen=True)
class OverallDecision:
    """The computed outcome and the reasons behind it."""

    decision: Overall
    reasons: tuple[str, ...]
    partial: bool


def decide(results: list[RequirementResult], *, unassessed_mandatory: int = 0) -> OverallDecision:
    """Aggregate requirement results into the release decision."""
    mandatory = [r for r in results if r.target.mandatory]
    failed = [r.target.issue_key for r in mandatory if r.verdict.decision is Decision.FAIL]
    insufficient = [
        r.target.issue_key
        for r in mandatory
        if r.verdict.decision is Decision.INSUFFICIENT_EVIDENCE
    ]
    partial = unassessed_mandatory > 0 or any(r.status == "failed" for r in results)
    if failed:
        # A demonstrated failure blocks the release even if other
        # requirements were not assessed.
        return OverallDecision(
            Overall.BLOCKED, (f"mandatory requirements failed: {', '.join(failed)}",), partial
        )
    reasons: list[str] = []
    if insufficient:
        reasons.append(
            f"mandatory requirements lack sufficient evidence: {', '.join(insufficient)}"
        )
    if unassessed_mandatory:
        reasons.append(f"{unassessed_mandatory} mandatory requirements were not assessed")
    if not mandatory and not unassessed_mandatory:
        reasons.append("no mandatory requirements were identified")
    if reasons:
        return OverallDecision(Overall.NEEDS_REVIEW, tuple(reasons), partial)
    return OverallDecision(Overall.READY, ("every mandatory requirement passed",), partial)
