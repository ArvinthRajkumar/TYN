"""Build the release report, trace and metrics from a checkpoint.

Everything here is derived from the saved checkpoint, so reports can be
re-rendered without new model calls (``python -m tyn report``). Text is
redacted before writing and verified afterwards. Typographic dashes in
model-written text are normalised to plain hyphens for consistency.
"""

import json
from pathlib import Path
from typing import Any, Final

from tyn.agent.checkpoint import Checkpoint
from tyn.agent.models import EvidenceRef, Observation, RequirementResult
from tyn.agent.runner import AssessmentOutcome
from tyn.paths import REPORTS_DIR, atomic_write_text, safe_child
from tyn.redact import Redactor
from tyn.report.redaction import verify_clean

REPORT_VERSION: Final = 1
# Em dash, en dash and figure dash, written as code points to keep the source ASCII.
_DASHES: Final = str.maketrans({chr(0x2014): "-", chr(0x2013): "-", chr(0x2012): "-"})


def _clean(text: str, redactor: Redactor) -> str:
    return redactor.redact(text.translate(_DASHES))


def _ref(ref: EvidenceRef) -> dict[str, Any]:
    return {"issue_key": ref.issue_key, "comment_id": ref.comment_id, "note": ref.note}


def _observation(item: Observation) -> dict[str, Any]:
    return {
        "issue_key": item.issue_key,
        "comment_id": item.comment_id,
        "summary": item.summary,
        "found_by": item.source,
    }


def _requirement(result: RequirementResult) -> dict[str, Any]:
    target, verdict = result.target, result.verdict
    return {
        "issue_key": target.issue_key,
        "source_id": target.source_id,
        "summary": target.summary,
        "mandatory": target.mandatory,
        "mandatory_source": target.mandatory_source,
        "decision": verdict.decision.value,
        "status": result.status,
        "failure": result.failure,
        "explanation": verdict.explanation,
        "supporting_evidence": [_ref(r) for r in verdict.supporting_evidence],
        "rejected_evidence": [_ref(r) for r in verdict.rejected_evidence],
        "outstanding": verdict.outstanding,
        "observations": [_observation(o) for o in verdict.observations],
        "validation_notes": result.validation_notes,
        "effort": {
            "seconds": result.seconds,
            "model_calls": result.model_calls,
            "tool_calls": result.tool_calls,
            "prompt_tokens": result.usage.prompt,
            "completion_tokens": result.usage.completion,
            "reasoning_tokens": result.usage.reasoning,
        },
    }


def report_data(outcome: AssessmentOutcome, generated_at: str) -> dict[str, Any]:
    """Return the JSON report structure."""
    checkpoint = outcome.checkpoint
    discovery = checkpoint.discovery
    observations = [
        {"requirement": r.target.issue_key, **_observation(o)}
        for r in outcome.assessed
        for o in r.verdict.observations
    ]
    return {
        "report_version": REPORT_VERSION,
        "generated_at": generated_at,
        "project": checkpoint.project,
        "scope_label": checkpoint.label,
        "release": checkpoint.release,
        "models": checkpoint.models,
        "complete": not outcome.overall.partial and outcome.stopped_reason is None,
        "stopped_reason": outcome.stopped_reason,
        "overall": {
            "decision": outcome.overall.decision.value,
            "reasons": list(outcome.overall.reasons),
            "computed_by": "code (BLOCKED if any mandatory FAIL; NEEDS_REVIEW if any mandatory "
            "INSUFFICIENT_EVIDENCE or unassessed; READY only if every mandatory PASS)",
        },
        "manifest_key": discovery.manifest_key if discovery else None,
        "discovery_notes": discovery.notes if discovery else ["discovery did not complete"],
        "requirements": [_requirement(r) for r in outcome.assessed],
        "not_assessed": [
            {"issue_key": t.issue_key, "source_id": t.source_id, "mandatory": t.mandatory}
            for t in outcome.unassessed
        ],
        "observations": observations,
    }


def _md_ref(ref: dict[str, Any]) -> str:
    where = (
        f"{ref['issue_key']} comment {ref['comment_id']}" if ref["comment_id"] else ref["issue_key"]
    )
    return f"{where}: {ref['note']}" if ref["note"] else where


def render_markdown(data: dict[str, Any]) -> str:
    """Render the human-readable report."""
    lines = [
        f"# Release readiness report: {data['release']}",
        "",
        f"**Overall decision: {data['overall']['decision']}**",
        "",
        *(f"- {reason}" for reason in data["overall"]["reasons"]),
        "",
    ]
    if not data["complete"]:
        lines += [
            "> **Partial assessment.** "
            + (data["stopped_reason"] or "Some requirements were not fully assessed.")
            + " Re-run `assess --resume` to complete it.",
            "",
        ]
    lines += [
        "| Field | Value |",
        "| --- | --- |",
        f"| Project | {data['project']} |",
        f"| Scope label | {data['scope_label']} |",
        f"| Manifest | {data['manifest_key'] or 'not found'} |",
        f"| Model | {', '.join(data['models'])} |",
        f"| Generated | {data['generated_at']} |",
        f"| Overall decision computed by | {data['overall']['computed_by']} |",
        "",
        "## Requirements",
        "",
        "| Requirement | Source | Summary | Mandatory | Decision |",
        "| --- | --- | --- | --- | --- |",
    ]
    for req in data["requirements"]:
        decision = (
            req["decision"] if req["status"] == "completed" else f"{req['decision']} (failed run)"
        )
        lines.append(
            f"| {req['issue_key']} | {req['source_id'] or ''} | {req['summary']} | "
            f"{'yes' if req['mandatory'] else 'no'} | {decision} |"
        )
    for item in data["not_assessed"]:
        lines.append(
            f"| {item['issue_key']} | {item['source_id'] or ''} | | "
            f"{'yes' if item['mandatory'] else 'no'} | NOT ASSESSED |"
        )
    lines += ["", "## Requirement details", ""]
    for req in data["requirements"]:
        lines += _requirement_section(req)
    lines += ["## Observations: instructions found in ticket text", ""]
    if data["observations"]:
        lines.append(
            "Ticket text that addressed reviewers or tried to dictate an outcome. It was "
            "treated as data and not followed."
        )
        lines.append("")
        for item in data["observations"]:
            where = (
                f"{item['issue_key']} comment {item['comment_id']}"
                if item["comment_id"]
                else item["issue_key"]
            )
            lines.append(
                f"- {where} (requirement {item['requirement']}, found by "
                f"{item['found_by']}): {item['summary']}"
            )
    else:
        lines.append("None found.")
    lines += ["", "## Discovery notes", ""]
    lines += [f"- {note}" for note in data["discovery_notes"]] or ["- none"]
    lines += [
        "",
        "## How this report was produced",
        "",
        "The agent read Jira only through read-only tools restricted in code to the project "
        "and scope label. Each requirement was investigated in a fresh, token-bounded context; "
        "every cited issue key and comment id was checked against what the tools had shown. "
        "The overall decision was computed in code from the per-requirement decisions. "
        "Evidence references are Jira issue keys and comment ids. The full tool-call trace "
        "is in `trace.jsonl` and `trace.md`; time and token usage are in `metrics.json`.",
        "",
    ]
    return "\n".join(lines)


def _requirement_section(req: dict[str, Any]) -> list[str]:
    mandatory = "mandatory" if req["mandatory"] else "not mandatory"
    lines = [
        f"### {req['issue_key']} ({req['source_id'] or 'no source id'}): {req['summary']}",
        "",
        f"- Decision: **{req['decision']}** ({mandatory})",
    ]
    if req["status"] != "completed":
        lines.append(f"- Run status: {req['status']} ({req['failure']})")
    lines += [f"- Explanation: {req['explanation']}", "- Supporting evidence:"]
    lines += [f"  - {_md_ref(ref)}" for ref in req["supporting_evidence"]] or ["  - none"]
    lines.append("- Rejected or inapplicable evidence:")
    lines += [f"  - {_md_ref(ref)}" for ref in req["rejected_evidence"]] or ["  - none"]
    lines.append("- Outstanding verification or clarification:")
    lines += [f"  - {item}" for item in req["outstanding"]] or ["  - none"]
    if req["observations"]:
        lines.append("- Instructions found in ticket text (not followed):")
        lines += [f"  - {_md_ref({**o, 'note': o['summary']})}" for o in req["observations"]]
    if req["validation_notes"]:
        lines.append("- Validation notes:")
        lines += [f"  - {note}" for note in req["validation_notes"]]
    effort = req["effort"]
    lines += [
        f"- Effort: {effort['model_calls']} model calls, {effort['tool_calls']} tool calls, "
        f"{effort['prompt_tokens']} prompt and {effort['completion_tokens']} completion tokens, "
        f"{effort['seconds']} s",
        "",
    ]
    return lines


def trace_events(checkpoint: Checkpoint) -> list[dict[str, Any]]:
    """Return trace events in report order: discovery, then each requirement."""
    order = ["discovery"]
    if checkpoint.discovery:
        order += [t.issue_key for t in checkpoint.discovery.requirements]
    return [event for scope in order for event in checkpoint.traces.get(scope, [])]


def render_trace_markdown(events: list[dict[str, Any]], release: str) -> str:
    """Render the trace as one table per scope."""
    lines = [
        f"# Investigation trace: {release}",
        "",
        "Every model and tool call in order. Latency excludes waits; Wait ms is time spent "
        "in the client-side token pacer or retry backoff before the request. Tool result "
        "tokens are estimates; model tokens are as reported by the provider.",
        "",
    ]
    scope = None
    for event in events:
        if event["scope"] != scope:
            scope = event["scope"]
            lines += [
                "",
                f"## {scope}",
                "",
                "| t (s) | Turn | Event | Detail | Result chars | Tokens in/out/reasoning "
                "| Latency ms | Wait ms |",
                "| --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        lines.append(_trace_row(event))
    lines.append("")
    return "\n".join(lines)


def _trace_row(event: dict[str, Any]) -> str:
    kind = event["kind"]
    tokens = ""
    detail = ""
    chars = ""
    if kind == "model_call":
        tokens = (
            f"{event['prompt_tokens']}/{event['completion_tokens']}/{event['reasoning_tokens']}"
        )
        requested = ", ".join(event["requested_tools"]) or "text reply"
        detail = f"choice={event['tool_choice']}; requested {requested}"
    elif kind == "tool_call":
        arguments = json.dumps(event["arguments"], ensure_ascii=False, separators=(",", ":"))
        detail = f"{event['tool']} {arguments} [{event['status']}]"
        chars = str(event["result_chars"])
        tokens = f"~{event['result_tokens_estimate']} (result)"
    else:
        detail = ", ".join(
            f"{k}={v}" for k, v in event.items() if k not in ("at", "t", "scope", "kind")
        )
    detail = detail.replace("|", "\\|")
    return (
        f"| {event['t']} | {event.get('turn', '')} | {kind} | {detail} | {chars} | {tokens} | "
        f"{event.get('latency_ms', '')} | {event.get('wait_ms', '')} |"
    )


def metrics_data(outcome: AssessmentOutcome) -> dict[str, Any]:
    """Return measured time, token and call counts."""
    checkpoint = outcome.checkpoint
    sessions = [s.model_dump() for s in checkpoint.sessions]
    model_calls = [e for e in trace_events(checkpoint) if e["kind"] == "model_call"]
    prompts = [e["prompt_tokens"] for e in model_calls]
    totals = {
        "model_calls": sum(s.model_calls for s in checkpoint.sessions),
        "tool_calls": sum(r.tool_calls for r in outcome.assessed)
        + (checkpoint.discovery.tool_calls if checkpoint.discovery else 0),
        "jira_calls": sum(s.jira_calls for s in checkpoint.sessions),
        "prompt_tokens": sum(s.usage.prompt for s in checkpoint.sessions),
        "completion_tokens": sum(s.usage.completion for s in checkpoint.sessions),
        "reasoning_tokens": sum(s.usage.reasoning for s in checkpoint.sessions),
        "cached_tokens": sum(s.usage.cached for s in checkpoint.sessions),
        "model_retries": sum(s.model_retries for s in checkpoint.sessions),
        "rate_limit_waits": sum(s.rate_limit_waits for s in checkpoint.sessions),
        "rate_limit_wait_seconds": round(
            sum(s.rate_limit_wait_seconds for s in checkpoint.sessions), 2
        ),
        "pacer_sleeps": sum(s.pacer_sleeps for s in checkpoint.sessions),
        "pacer_sleep_seconds": round(sum(s.pacer_sleep_seconds for s in checkpoint.sessions), 2),
    }
    totals["total_tokens"] = totals["prompt_tokens"] + totals["completion_tokens"]
    discovery = checkpoint.discovery
    return {
        "release": checkpoint.release,
        "models": checkpoint.models,
        "wall_time_seconds": round(sum(s.seconds for s in checkpoint.sessions), 2),
        "sessions": sessions,
        "totals": totals,
        "context": {
            "requests": len(prompts),
            "max_prompt_tokens_per_request": max(prompts, default=0),
            "mean_prompt_tokens_per_request": round(sum(prompts) / len(prompts)) if prompts else 0,
            "pruned_tool_results": sum(
                1 for e in trace_events(checkpoint) if e["kind"] in ("prune", "prune_after_413")
            ),
        },
        "discovery": discovery.model_dump(include={"seconds", "model_calls", "tool_calls", "usage"})
        if discovery
        else None,
        "requirements": [
            {
                "issue_key": r.target.issue_key,
                "source_id": r.target.source_id,
                "decision": r.verdict.decision.value,
                "status": r.status,
                "seconds": r.seconds,
                "model_calls": r.model_calls,
                "tool_calls": r.tool_calls,
                "prompt_tokens": r.usage.prompt,
                "completion_tokens": r.usage.completion,
                "reasoning_tokens": r.usage.reasoning,
                "cached_tokens": r.usage.cached,
            }
            for r in outcome.assessed
        ],
    }


def write_artifacts(
    outcome: AssessmentOutcome,
    redactor: Redactor,
    *,
    generated_at: str,
    reports_dir: Path = REPORTS_DIR,
) -> list[Path]:
    """Write all artifacts, redacted, and verify none contains a secret."""
    data = report_data(outcome, generated_at)
    events = trace_events(outcome.checkpoint)
    files = {
        "release_report.json": json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        "release_report.md": render_markdown(data),
        "trace.jsonl": "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events),
        "trace.md": render_trace_markdown(events, outcome.checkpoint.release),
        "metrics.json": json.dumps(metrics_data(outcome), indent=2) + "\n",
    }
    written: list[Path] = []
    for name, text in files.items():
        path = safe_child(reports_dir, name)
        atomic_write_text(path, _clean(text, redactor))
        written.append(path)
    verify_clean(written, redactor)
    return written
