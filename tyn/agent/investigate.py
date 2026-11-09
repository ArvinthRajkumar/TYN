"""One requirement investigation in a fresh context, with citation checks.

Every issue key and comment id the model cites must have appeared in a tool
result during this investigation. An invalid submission gets one repair
round; if it is still invalid the verdict is downgraded to
INSUFFICIENT_EVIDENCE, keeping only the citations that check out.
"""

from typing import Any, Final

from pydantic import ValidationError

from tyn.agent.loop import AgentLoop, LoopResult, SubmitCheck
from tyn.agent.models import (
    Decision,
    EvidenceRef,
    Observation,
    RequirementResult,
    RequirementTarget,
    Verdict,
)
from tyn.agent.prompts import (
    INVESTIGATION_SYSTEM,
    RECORD_FINDING,
    VERDICT_SUBMIT,
    investigation_seed,
)
from tyn.agent.tools import SCHEMAS, Ledger

INVESTIGATION_TOOLS: Final = (
    "get_issue",
    "get_comments",
    "get_linked_issues",
    "search_issues",
    "check_evidence_scope",
)
DOWNGRADE_NOTE: Final = " [Downgraded to INSUFFICIENT_EVIDENCE: citations failed validation.]"


def investigation_tools() -> list[dict[str, Any]]:
    """Return the tool schemas offered during an investigation."""
    return [SCHEMAS[name] for name in INVESTIGATION_TOOLS] + [RECORD_FINDING, VERDICT_SUBMIT]


def _normalise_refs(items: Any) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        ref = dict(item)
        comment = ref.get("comment_id")
        if comment in ("", None):
            ref.pop("comment_id", None)
        else:
            ref["comment_id"] = str(comment).strip().lstrip("cC")
        ref["issue_key"] = str(ref.get("issue_key", "")).strip().upper()
        refs.append(ref)
    return refs


def normalise_verdict_args(args: dict[str, Any]) -> dict[str, Any]:
    """Accept common harmless variations (``c123`` ids, empty comment ids)."""
    data = dict(args)
    for name in ("supporting_evidence", "rejected_evidence"):
        data[name] = _normalise_refs(data.get(name))
    observations = _normalise_refs(data.get("observations"))
    for observation in observations:
        observation.setdefault("summary", observation.pop("note", ""))
        observation.pop("note", None)
        observation["source"] = "model"
    data["observations"] = observations
    data.setdefault("outstanding", [])
    return data


def check_verdict(args: dict[str, Any], ledger: Ledger) -> SubmitCheck:
    """Validate shape and citations; provide a downgraded fallback."""
    try:
        verdict = Verdict.model_validate(normalise_verdict_args(args))
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)[:4]
        )
        return SubmitCheck(ok=False, error=f"submit_verdict arguments invalid: {details}")
    problems: list[str] = []
    valid_support = _valid(verdict.supporting_evidence, ledger, problems)
    valid_rejected = _valid(verdict.rejected_evidence, ledger, problems)
    valid_observations = [
        o for o in verdict.observations if ledger.problem_with(o.issue_key, o.comment_id) is None
    ]
    if verdict.decision is not Decision.INSUFFICIENT_EVIDENCE and not verdict.supporting_evidence:
        problems.append(f"{verdict.decision.value} needs at least one supporting citation")
    if not problems:
        return SubmitCheck(ok=True, value=verdict)
    fallback = Verdict(
        decision=Decision.INSUFFICIENT_EVIDENCE,
        explanation=(verdict.explanation + DOWNGRADE_NOTE)[:2000],
        supporting_evidence=valid_support,
        rejected_evidence=valid_rejected,
        outstanding=[*verdict.outstanding, *(f"citation problem: {p}" for p in problems)][:8],
        observations=valid_observations,
    )
    return SubmitCheck(
        ok=False,
        error="citation check failed: " + "; ".join(problems) + ". Cite only keys and comment "
        "ids shown by tools, then call submit_verdict again.",
        fallback=fallback,
        notes=[f"citation problem: {p}" for p in problems],
    )


def _valid(refs: list[EvidenceRef], ledger: Ledger, problems: list[str]) -> list[EvidenceRef]:
    kept: list[EvidenceRef] = []
    for ref in refs:
        problem = ledger.problem_with(ref.issue_key, ref.comment_id)
        if problem:
            problems.append(problem)
        else:
            kept.append(ref)
    return kept


class Investigator:
    """Investigate requirements one at a time, each in a fresh context."""

    def __init__(self, loop: AgentLoop, *, release: str) -> None:
        self._loop = loop
        self._release = release

    def investigate(
        self,
        target: RequirementTarget,
        *,
        requirement_text: str,
        manifest_key: str | None,
        manifest_rules: str,
    ) -> RequirementResult:
        """Run one investigation and turn its outcome into a result."""
        ledger = Ledger()
        ledger.issue_keys.add(target.issue_key)
        if manifest_key:
            ledger.issue_keys.add(manifest_key)
        row = f"[{target.source_id or '?'}] {target.summary}".strip()

        def seed(notes: list[str]) -> str:
            return investigation_seed(
                release=self._release,
                requirement_key=target.issue_key,
                requirement_row=row,
                requirement_text=requirement_text,
                manifest_key=manifest_key,
                manifest_rules=manifest_rules,
                notes=notes,
            )

        outcome = self._loop.run(
            scope=target.issue_key,
            system=INVESTIGATION_SYSTEM,
            seed=seed,
            tools=investigation_tools(),
            submit_name="submit_verdict",
            validate=check_verdict,
            ledger=ledger,
        )
        return _result(target, outcome, ledger)


def _result(target: RequirementTarget, outcome: LoopResult, ledger: Ledger) -> RequirementResult:
    verdict = outcome.value if isinstance(outcome.value, Verdict) else None
    status = "completed"
    if verdict is None:
        status = "failed"
        verdict = Verdict(
            decision=Decision.INSUFFICIENT_EVIDENCE,
            explanation="The investigation did not produce a valid verdict; no decision could "
            "be supported by checked evidence.",
            outstanding=[f"re-run the assessment for this requirement ({outcome.failure})"],
        )
    elif outcome.failure:
        status = "failed"
    detector = [o for o in ledger.observations if o.source == "detector"]
    merged: list[Observation] = list(verdict.observations)
    for observation in detector:
        if not any(m.comment_id == observation.comment_id for m in merged):
            merged.append(observation)
    verdict = verdict.model_copy(update={"observations": merged[:6]})
    notes = list(outcome.validation_notes)
    if outcome.tool_errors:
        notes.append(f"{outcome.tool_errors} tool calls returned input errors to the model")
    if outcome.pruned_results:
        notes.append(f"{outcome.pruned_results} earlier tool results were pruned to fit the budget")
    return RequirementResult(
        target=target,
        verdict=verdict,
        status=status,  # type: ignore[arg-type]
        failure=outcome.failure,
        validation_notes=notes,
        seconds=round(outcome.seconds, 2),
        model_calls=outcome.model_calls,
        tool_calls=outcome.tool_calls,
        usage=outcome.usage,
    )
