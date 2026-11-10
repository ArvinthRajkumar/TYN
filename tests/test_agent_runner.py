"""Discovery reconciliation, overall decision, checkpoints, resume and --only."""

import json
from pathlib import Path
from typing import Any

import pytest

from tests.agent_support import AgentWorld, ScriptedModel, build_world, call, make_loop
from tyn.agent.budget import TokenEstimator
from tyn.agent.checkpoint import CheckpointStore, Provenance
from tyn.agent.decision import decide
from tyn.agent.discovery import DiscoveryStage
from tyn.agent.llm import ChatResponse, ModelError, ResilientModel
from tyn.agent.loop import AgentLoop, LoopLimits
from tyn.agent.models import (
    Decision,
    Overall,
    RequirementResult,
    RequirementTarget,
    Verdict,
)
from tyn.agent.pacer import DailyBudget, DailyBudgetExhaustedError, TokenPacer
from tyn.agent.runner import AssessConfig, AssessmentRunner
from tyn.agent.trace import TraceRecorder

PROVENANCE = Provenance("scripted", "scripted-model", "fakehash0001")


@pytest.fixture
def world(tmp_path: Path) -> AgentWorld:
    return build_world(tmp_path)


def _result(
    key: str, decision: Decision, *, mandatory: bool = True, status: str = "completed"
) -> RequirementResult:
    return RequirementResult(
        target=RequirementTarget(issue_key=key, mandatory=mandatory),
        verdict=Verdict(decision=decision, explanation="x"),
        status=status,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("decisions", "unassessed", "expected"),
    [
        ([Decision.PASS, Decision.PASS], 0, Overall.READY),
        ([Decision.PASS, Decision.INSUFFICIENT_EVIDENCE], 0, Overall.NEEDS_REVIEW),
        ([Decision.FAIL, Decision.INSUFFICIENT_EVIDENCE], 0, Overall.BLOCKED),
        ([Decision.FAIL], 3, Overall.BLOCKED),
        ([Decision.PASS], 1, Overall.NEEDS_REVIEW),
        ([], 0, Overall.NEEDS_REVIEW),
    ],
)
def test_overall_decision_rules(
    decisions: list[Decision], unassessed: int, expected: Overall
) -> None:
    results = [_result(f"K-{i}", d) for i, d in enumerate(decisions)]
    assert decide(results, unassessed_mandatory=unassessed).decision is expected


def test_optional_requirements_do_not_change_the_outcome() -> None:
    results = [
        _result("K-1", Decision.PASS),
        _result("K-2", Decision.FAIL, mandatory=False),
        _result("K-3", Decision.INSUFFICIENT_EVIDENCE, mandatory=False),
    ]
    assert decide(results).decision is Overall.READY


def test_failed_investigation_marks_outcome_partial() -> None:
    outcome = decide([_result("K-1", Decision.INSUFFICIENT_EVIDENCE, status="failed")])
    assert outcome.partial
    assert outcome.decision is Overall.NEEDS_REVIEW


def _discovery_steps(world: AgentWorld, selected: list[dict[str, Any]]) -> list[Any]:
    return [
        call("get_release_manifest", {"release": "R5.0"}),
        call("list_requirements_for_release", {"release": "R5.0"}),
        call("submit_requirements", {"manifest_key": world.key("MAN-1"), "requirements": selected}),
    ]


def test_discovery_adds_omitted_manifest_links_and_resolves_mandatory(world: AgentWorld) -> None:
    model = ScriptedModel(
        _discovery_steps(
            world,
            [
                {"issue_key": world.key("RQ-1"), "mandatory": False},
                {"issue_key": world.key("RQ-2"), "mandatory": False},
            ],
        )
    )
    loop, _ = make_loop(model, world.toolbox, LoopLimits(max_model_calls=6))
    discovery = DiscoveryStage(loop, world.toolbox, release="R5.0").run()
    targets = {t.source_id: t for t in discovery.requirements}
    assert set(targets) == {"RQ-1", "RQ-2", "RQ-3"}
    assert (targets["RQ-1"].mandatory, targets["RQ-1"].mandatory_source) == (True, "conflict")
    assert (targets["RQ-2"].mandatory, targets["RQ-2"].mandatory_source) == (False, "model+record")
    assert (targets["RQ-3"].mandatory, targets["RQ-3"].mandatory_source) == (True, "record")
    assert any("added by code" in note for note in discovery.notes)
    assert "Assess the linked requirements." in discovery.manifest_rules


def test_discovery_falls_back_to_manifest_links_when_the_model_fails(world: AgentWorld) -> None:
    def broken(*_: Any) -> ChatResponse:
        raise ModelError("down")

    loop, _ = make_loop(ScriptedModel([broken]), world.toolbox)
    discovery = DiscoveryStage(loop, world.toolbox, release="R5.0").run()
    assert {t.source_id for t in discovery.requirements} == {"RQ-1", "RQ-2", "RQ-3"}
    assert any("did not complete" in note for note in discovery.notes)


def _requirement_steps(world: AgentWorld, source: str, evidence: str, decision: str) -> list[Any]:
    comment = str(world.fake.comments[world.key(evidence)][0]["id"])
    extra = [call("get_issue", {"issue_key": world.key("EV-11")})] if source == "RQ-1" else []
    return [
        call("get_issue", {"issue_key": world.key(evidence)}),
        *extra,
        call(
            "submit_verdict",
            {
                "decision": decision,
                "explanation": f"{source} decided from {evidence}.",
                "supporting_evidence": [
                    {"issue_key": world.key(evidence), "comment_id": comment, "note": "result"}
                ],
                "rejected_evidence": [],
            },
        ),
    ]


def _runner(
    world: AgentWorld, tmp_path: Path, steps: list[Any], **config: Any
) -> tuple[AssessmentRunner, CheckpointStore]:
    estimator = TokenEstimator()
    pacer = TokenPacer(1_000_000, sleep=lambda _: None)
    model = ResilientModel(
        ScriptedModel(steps),
        pacer=pacer,
        daily=DailyBudget(tmp_path / "state" / "usage.json", "scripted-model", 10_000_000),
        estimator=estimator,
        sleep=lambda _: None,
    )
    trace = TraceRecorder()
    store = CheckpointStore(world.fake.project, "agent-test-scope", "R5.0", root=tmp_path / "state")

    def factory(limits: LoopLimits) -> AgentLoop:
        return AgentLoop(
            model=model, toolbox=world.toolbox, estimator=estimator, trace=trace, limits=limits
        )

    runner = AssessmentRunner(
        toolbox=world.toolbox,
        model=model,
        pacer=pacer,
        loop_factory=factory,
        trace=trace,
        store=store,
        config=AssessConfig(release="R5.0", **config),
        provenance=PROVENANCE,
    )
    return runner, store


def _all_selected(world: AgentWorld) -> list[dict[str, Any]]:
    return [
        {"issue_key": world.key(source), "mandatory": source != "RQ-2"}
        for source in ("RQ-1", "RQ-2", "RQ-3")
    ]


def test_full_run_computes_the_overall_decision_in_code(world: AgentWorld, tmp_path: Path) -> None:
    steps = (
        _discovery_steps(world, _all_selected(world))
        + _requirement_steps(world, "RQ-1", "EV-10", "PASS")
        + _requirement_steps(world, "RQ-2", "EV-20", "FAIL")
        + _requirement_steps(world, "RQ-3", "EV-30", "PASS")
    )
    runner, store = _runner(world, tmp_path, steps)
    outcome = runner.run()
    assert outcome.overall.decision is Overall.READY
    assert not outcome.overall.partial
    assert outcome.stopped_reason is None
    saved = json.loads(store.path.read_text(encoding="utf-8"))
    assert set(saved["traces"]) == {
        "discovery",
        world.key("RQ-1"),
        world.key("RQ-2"),
        world.key("RQ-3"),
    }
    assert saved["sessions"][0]["model_calls"] == 10


def test_budget_exhaustion_checkpoints_and_resume_skips_completed(
    world: AgentWorld, tmp_path: Path
) -> None:
    def out_of_tokens(*_: Any) -> ChatResponse:
        raise DailyBudgetExhaustedError(
            199_000, 200_000, __import__("datetime").datetime(2026, 1, 2)
        )

    first_steps = (
        _discovery_steps(world, _all_selected(world))
        + _requirement_steps(world, "RQ-1", "EV-10", "FAIL")
        + [out_of_tokens]
    )
    runner, store = _runner(world, tmp_path, first_steps)
    partial = runner.run()
    assert partial.stopped_reason is not None
    assert "daily token budget" in partial.stopped_reason
    assert [r.target.source_id for r in partial.assessed] == ["RQ-1"]
    assert partial.overall.decision is Overall.BLOCKED
    assert partial.overall.partial

    resumed_steps = _requirement_steps(world, "RQ-2", "EV-20", "PASS") + _requirement_steps(
        world, "RQ-3", "EV-30", "INSUFFICIENT_EVIDENCE"
    )
    resumed_runner, _ = _runner(world, tmp_path, resumed_steps, resume=True)
    final = resumed_runner.run()
    assert [r.target.source_id for r in final.assessed] == ["RQ-1", "RQ-2", "RQ-3"]
    assert final.stopped_reason is None
    assert final.overall.decision is Overall.BLOCKED
    assert not final.overall.partial
    assert len(json.loads(store.path.read_text(encoding="utf-8"))["sessions"]) == 2


def test_only_reinvestigates_one_requirement_reusing_discovery(
    world: AgentWorld, tmp_path: Path
) -> None:
    steps = _discovery_steps(world, _all_selected(world)) + _requirement_steps(
        world, "RQ-2", "EV-20", "PASS"
    )
    runner, _ = _runner(world, tmp_path, steps, only="RQ-2")
    outcome = runner.run()
    assert [r.target.source_id for r in outcome.assessed] == ["RQ-2"]
    assert {t.source_id for t in outcome.unassessed} == {"RQ-1", "RQ-3"}
    assert outcome.overall.partial

    again, _ = _runner(
        world, tmp_path, _requirement_steps(world, "RQ-2", "EV-20", "FAIL"), only=world.key("RQ-2")
    )
    rerun = again.run()
    assert rerun.assessed[0].verdict.decision is Decision.FAIL


def test_only_with_unknown_requirement_lists_choices(world: AgentWorld, tmp_path: Path) -> None:
    from tyn.errors import TynError

    runner, _ = _runner(
        world, tmp_path, _discovery_steps(world, _all_selected(world)), only="NOPE-9"
    )
    with pytest.raises(TynError, match="available"):
        runner.run()


def test_fresh_run_keeps_the_previous_checkpoint_as_backup(
    world: AgentWorld, tmp_path: Path
) -> None:
    steps = _discovery_steps(world, _all_selected(world)) + _requirement_steps(
        world, "RQ-2", "EV-20", "PASS"
    )
    runner, store = _runner(world, tmp_path, steps, only="RQ-2")
    runner.run()
    store.new(PROVENANCE)
    assert store.path.with_suffix(".previous.json").exists()


def test_run_token_cap_stops_cleanly_and_checkpoints(world: AgentWorld, tmp_path: Path) -> None:
    from tyn.agent.llm import RunTokenCapReachedError

    def over_cap(*_: Any) -> ChatResponse:
        raise RunTokenCapReachedError(9_900, 10_000)

    steps = (
        _discovery_steps(world, _all_selected(world))
        + _requirement_steps(world, "RQ-1", "EV-10", "FAIL")
        + [over_cap]
    )
    runner, store = _runner(world, tmp_path, steps)
    outcome = runner.run()
    assert outcome.stopped_reason is not None
    assert "MAX_RUN_TOKENS" in outcome.stopped_reason
    saved = json.loads(store.path.read_text(encoding="utf-8"))
    assert list(saved["results"]) == [world.key("RQ-1")]
    assert saved["sessions"][0]["stopped_reason"] == outcome.stopped_reason


def test_depleted_credits_stop_cleanly(world: AgentWorld, tmp_path: Path) -> None:
    from tyn.agent.llm import PrepaidCreditsDepletedError

    def no_credit(*_: Any) -> ChatResponse:
        raise PrepaidCreditsDepletedError()

    runner, _ = _runner(world, tmp_path, [no_credit])
    outcome = runner.run()
    assert outcome.stopped_reason is not None
    assert "402" in outcome.stopped_reason
    assert outcome.overall.partial


def test_resume_refuses_a_checkpoint_from_another_provider(
    world: AgentWorld, tmp_path: Path
) -> None:
    from tyn.errors import StateError

    steps = _discovery_steps(world, _all_selected(world)) + _requirement_steps(
        world, "RQ-2", "EV-20", "PASS"
    )
    runner, store = _runner(world, tmp_path, steps, only="RQ-2")
    runner.run()
    for other in (
        Provenance("groq", "scripted-model", PROVENANCE.prompt_hash),
        Provenance("scripted", "another-model", PROVENANCE.prompt_hash),
        Provenance("scripted", "scripted-model", "000000000000"),
    ):
        with pytest.raises(StateError, match="refusing to mix"):
            store.load(other)
    assert store.load(PROVENANCE) is not None
    assert store.load(None) is not None


def test_checkpoints_without_provenance_are_refused(world: AgentWorld, tmp_path: Path) -> None:
    from tyn.errors import StateError

    store = CheckpointStore(world.fake.project, "agent-test-scope", "R5.0", root=tmp_path / "state")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps({"version": 1, "models": ["x"]}), encoding="utf-8")
    with pytest.raises(StateError, match="older version"):
        store.load(PROVENANCE)
