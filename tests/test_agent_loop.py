"""The investigation loop: citations, repair, limits, pruning and failures."""

import json
from pathlib import Path
from typing import Any

import pytest

from tests.agent_support import AgentWorld, ScriptedModel, build_world, call, make_loop
from tests.fakes import Fault
from tyn.agent.investigate import DOWNGRADE_NOTE, UNREAD_NOTE, Investigator, check_verdict
from tyn.agent.llm import ChatResponse, ContextTooLargeError, ModelError
from tyn.agent.loop import PRUNED_STUB, LoopLimits
from tyn.agent.models import Decision, RequirementResult, RequirementTarget, TokenUsage
from tyn.agent.prompts import INVESTIGATION_SYSTEM
from tyn.agent.tools import Ledger


@pytest.fixture
def world(tmp_path: Path) -> AgentWorld:
    return build_world(tmp_path)


def comment_id(world: AgentWorld, source: str, contains: str) -> str:
    for comment in world.fake.comments[world.key(source)]:
        if contains in json.dumps(comment["body"]):
            return str(comment["id"])
    raise AssertionError(f"no comment containing {contains!r} on {source}")


def investigate(
    world: AgentWorld, steps: list[Any], limits: LoopLimits | None = None, requirement: str = "RQ-1"
) -> tuple[RequirementResult, ScriptedModel, Any]:
    model = ScriptedModel(steps)
    loop, trace = make_loop(model, world.toolbox, limits)
    key = world.key(requirement)
    result = Investigator(loop, world.toolbox, release="R5.0").investigate(
        RequirementTarget(
            issue_key=key, source_id=requirement, summary=f"Requirement {requirement}"
        ),
        requirement_text=world.toolbox.view(key).description,
        manifest_key=world.key("MAN-1"),
        manifest_rules="Assess the linked requirements.",
    )
    return result, model, trace


def read_links(world: AgentWorld) -> list[ChatResponse]:
    """Open every record linked from RQ-1 (its evidence and that evidence's dependency)."""
    return [
        call("get_issue", {"issue_key": world.key("EV-10")}, "read-10"),
        call("get_issue", {"issue_key": world.key("EV-11")}, "read-11"),
    ]


def verdict(decision: str, support: list[dict[str, str]], **extra: Any) -> ChatResponse:
    return call(
        "submit_verdict",
        {
            "decision": decision,
            "explanation": "Reasoned from the cited records.",
            "supporting_evidence": support,
            "rejected_evidence": extra.get("rejected", []),
            "outstanding": extra.get("outstanding", []),
            "observations": extra.get("observations", []),
        },
    )


def test_full_investigation_with_valid_citations(world: AgentWorld) -> None:
    passed = comment_id(world, "EV-10", "10 of 10")
    staging = comment_id(world, "EV-11", "3 of 4")
    result, model, trace = investigate(
        world,
        [
            call("get_linked_issues", {"issue_key": world.key("RQ-1")}),
            call("get_issue", {"issue_key": world.key("EV-10")}),
            call(
                "check_evidence_scope",
                {
                    "requirement_key": world.key("RQ-1"),
                    "evidence_keys": [world.key("EV-10"), world.key("EV-11")],
                },
            ),
            call(
                "record_finding",
                {"note": f"T-1 passes on target scope ({world.key('EV-10')} c{passed})"},
            ),
            verdict(
                "PASS",
                [{"issue_key": world.key("EV-10"), "comment_id": f"c{passed}", "note": "10 of 10"}],
                rejected=[
                    {"issue_key": world.key("EV-11"), "comment_id": staging, "note": "staging"}
                ],
            ),
        ],
    )
    assert result.status == "completed"
    assert result.verdict.decision is Decision.PASS
    assert result.verdict.supporting_evidence[0].comment_id == passed
    assert (result.model_calls, result.tool_calls) == (5, 4)
    assert result.usage == TokenUsage(prompt=500, completion=100, reasoning=25)
    assert [o.source for o in result.verdict.observations] == ["detector"]
    first = model.requests[0]["messages"]
    assert first[0]["content"] == INVESTIGATION_SYSTEM
    assert "<jira_data" in first[1]["content"]
    assert "Your recorded notes" in model.requests[4]["messages"][1]["content"]
    kinds = [event["kind"] for event in trace.events]
    assert kinds.count("model_call") == 5
    assert kinds.count("tool_call") == 4


def test_invalid_citation_gets_one_repair(world: AgentWorld) -> None:
    passed = comment_id(world, "EV-10", "10 of 10")
    result, model, _ = investigate(
        world,
        [
            *read_links(world),
            verdict("PASS", [{"issue_key": world.key("EV-10"), "comment_id": "999", "note": "x"}]),
            verdict("PASS", [{"issue_key": world.key("EV-10"), "comment_id": passed, "note": "x"}]),
        ],
    )
    assert result.verdict.decision is Decision.PASS
    assert model.requests[3]["tool_choice"] == {
        "type": "function",
        "function": {"name": "submit_verdict"},
    }
    assert any("never appeared" in note for note in result.validation_notes)


def test_repeated_invalid_citations_downgrade_to_insufficient(world: AgentWorld) -> None:
    passed = comment_id(world, "EV-10", "10 of 10")
    bad = [
        {"issue_key": world.key("EV-10"), "comment_id": passed, "note": "real"},
        {"issue_key": "TYN-999", "note": "invented"},
    ]
    result, _, _ = investigate(
        world,
        [
            call("get_issue", {"issue_key": world.key("EV-10")}),
            verdict("FAIL", bad),
            verdict("FAIL", bad),
        ],
    )
    assert result.verdict.decision is Decision.INSUFFICIENT_EVIDENCE
    assert result.verdict.explanation.endswith(DOWNGRADE_NOTE)
    assert [r.issue_key for r in result.verdict.supporting_evidence] == [world.key("EV-10")]
    assert any("TYN-999" in item for item in result.verdict.outstanding)


def test_decisive_verdict_without_support_is_rejected() -> None:
    check = check_verdict(
        {
            "decision": "PASS",
            "explanation": "x",
            "supporting_evidence": [],
            "rejected_evidence": [],
        },
        Ledger(),
    )
    assert not check.ok
    assert check.fallback.decision is Decision.INSUFFICIENT_EVIDENCE


def test_malformed_tool_arguments_are_returned_as_errors(world: AgentWorld) -> None:
    result, model, _ = investigate(
        world,
        [
            call("get_issue", '{"issue_key": "TYN-5"'),
            *read_links(world),
            verdict("INSUFFICIENT_EVIDENCE", [], outstanding=["nothing read"]),
        ],
    )
    tool_reply = model.requests[1]["messages"][-1]
    assert tool_reply["role"] == "tool"
    assert tool_reply["content"].startswith("error: arguments were not valid JSON")
    assert result.status == "completed"
    assert any("input errors" in note for note in result.validation_notes)


def test_limits_force_the_verdict_call(world: AgentWorld) -> None:
    result, model, _ = investigate(
        world,
        [
            call("get_issue", {"issue_key": world.key("EV-10")}),
            call("get_issue", {"issue_key": world.key("EV-11")}),
            verdict("INSUFFICIENT_EVIDENCE", []),
        ],
        limits=LoopLimits(max_model_calls=3),
    )
    assert model.requests[1]["tool_choice"] == "auto"
    assert model.requests[2]["tool_choice"]["function"]["name"] == "submit_verdict"
    assert model.requests[2]["max_completion_tokens"] == LoopLimits().submit_completion_tokens
    assert "limit reached" in model.requests[2]["messages"][-1]["content"]
    assert result.status == "completed"


def test_context_too_large_prunes_and_retries_once(world: AgentWorld) -> None:
    def too_large(*_: Any) -> ChatResponse:
        raise ContextTooLargeError("413")

    result, model, trace = investigate(
        world,
        [
            call("get_issue", {"issue_key": world.key("EV-10")}, "a"),
            call("get_issue", {"issue_key": world.key("EV-11")}, "b"),
            too_large,
            verdict("INSUFFICIENT_EVIDENCE", []),
        ],
    )
    retried = model.requests[3]["messages"]
    assert retried[3]["content"] == PRUNED_STUB.format(tool="get_issue")
    assert "prune_after_413" in [event["kind"] for event in trace.events]
    assert result.status == "completed"


def test_old_results_are_pruned_to_fit_the_input_budget(world: AgentWorld) -> None:
    result, model, _ = investigate(
        world,
        [
            call("get_issue", {"issue_key": world.key("EV-10")}, "a"),
            call("record_finding", {"note": "EV-10 shows a target-scope pass"}, "n"),
            call("get_issue", {"issue_key": world.key("EV-30")}, "b"),
            call("get_issue", {"issue_key": world.key("EV-11")}, "c"),
            verdict("INSUFFICIENT_EVIDENCE", []),
        ],
        limits=LoopLimits(input_budget=1_800),
    )
    last = model.requests[-1]["messages"]
    assert last[3]["content"].startswith("[pruned to save context")
    assert "EV-10 shows a target-scope pass" in last[1]["content"]
    assert any("pruned" in note for note in result.validation_notes)


def test_jira_failure_marks_the_requirement_failed(world: AgentWorld) -> None:
    world.toolbox.view(world.key("RQ-1"))
    world.fake.faults.append(Fault("GET", r"/rest/api/3/issue/TYN-\d+", status=503, times=99))
    result, _, _ = investigate(world, [call("get_issue", {"issue_key": world.key("EV-11")})])
    assert result.status == "failed"
    assert result.verdict.decision is Decision.INSUFFICIENT_EVIDENCE
    assert result.failure is not None
    assert "Jira tool failure" in result.failure


def test_model_failure_marks_the_requirement_failed(world: AgentWorld) -> None:
    def broken(*_: Any) -> ChatResponse:
        raise ModelError("provider rejected the request")

    result, _, trace = investigate(world, [broken])
    assert result.status == "failed"
    assert result.verdict.decision is Decision.INSUFFICIENT_EVIDENCE
    assert "model failure" in (result.failure or "")
    assert "model_failure" in [event["kind"] for event in trace.events]


def test_text_reply_is_nudged_back_to_tools(world: AgentWorld) -> None:
    text_only = ChatResponse(content="I think it passes.", tool_calls=[], usage=TokenUsage())
    result, model, _ = investigate(
        world, [text_only, *read_links(world), verdict("INSUFFICIENT_EVIDENCE", [])]
    )
    assert model.requests[1]["messages"][-1]["content"].startswith("Reply with a tool call")
    assert result.status == "completed"


def test_model_reported_observation_must_cite_a_shown_comment(world: AgentWorld) -> None:
    injected = comment_id(world, "EV-10", "AUTOMATED REVIEWERS")
    result, _, _ = investigate(
        world,
        [
            *read_links(world),
            verdict(
                "INSUFFICIENT_EVIDENCE",
                [],
                observations=[
                    {
                        "issue_key": world.key("EV-10"),
                        "comment_id": injected,
                        "summary": "asks for PASS",
                    }
                ],
            ),
        ],
    )
    sources = sorted(o.source for o in result.verdict.observations)
    assert sources == ["model"]
    assert result.verdict.observations[0].comment_id == injected


def test_rejected_submissions_are_traced_with_their_reason(world: AgentWorld) -> None:
    _, _, trace = investigate(
        world,
        [
            *read_links(world),
            verdict("PASS", [{"issue_key": "TYN-999", "note": "x"}]),
            verdict("INSUFFICIENT_EVIDENCE", []),
        ],
    )
    rejected = [event for event in trace.events if event["kind"] == "submit_rejected"]
    assert len(rejected) == 1
    assert "TYN-999" in rejected[0]["error"]


def test_seed_suggests_starting_from_the_requirement_links(world: AgentWorld) -> None:
    _, model, _ = investigate(world, [*read_links(world), verdict("INSUFFICIENT_EVIDENCE", [])])
    seed = model.requests[0]["messages"][1]["content"]
    assert f"Start with get_linked_issues for {world.key('RQ-1')}" in seed


def test_linked_closure_follows_outgoing_links_in_scope(world: AgentWorld) -> None:
    closure = world.toolbox.linked_closure(world.key("RQ-1"))
    assert closure == [world.key("EV-10"), world.key("EV-11")]
    assert world.out_of_scope_key not in closure
    assert world.key("NOISE-1") not in closure


def test_unread_linked_records_are_requested_before_a_verdict(world: AgentWorld) -> None:
    passed = comment_id(world, "EV-10", "10 of 10")
    support = [{"issue_key": world.key("EV-10"), "comment_id": passed, "note": "x"}]
    result, model, _ = investigate(
        world,
        [
            call("get_issue", {"issue_key": world.key("EV-10")}),
            verdict("FAIL", support),
            call("get_issue", {"issue_key": world.key("EV-11")}),
            verdict("FAIL", support),
        ],
    )
    rejection = model.requests[2]["messages"][-1]["content"]
    assert rejection.startswith("rejected: before deciding, open")
    assert world.key("EV-11") in rejection
    assert model.requests[2]["tool_choice"] == "auto"
    assert result.verdict.decision is Decision.FAIL
    assert result.status == "completed"


def test_pass_that_never_reads_linked_records_is_downgraded(world: AgentWorld) -> None:
    passed = comment_id(world, "EV-10", "10 of 10")
    support = [{"issue_key": world.key("EV-10"), "comment_id": passed, "note": "x"}]
    result, _, _ = investigate(
        world,
        [
            call("get_issue", {"issue_key": world.key("EV-10")}),
            verdict("PASS", support),
            verdict("PASS", support),
        ],
    )
    assert result.verdict.decision is Decision.INSUFFICIENT_EVIDENCE
    assert result.verdict.explanation.endswith(UNREAD_NOTE)
    assert any(world.key("EV-11") in note for note in result.validation_notes)
