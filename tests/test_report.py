"""Report artifacts: content, dash normalisation and redaction of secrets."""

import json
from pathlib import Path

import pytest

from tests.conftest import FAKE_EMAIL, FAKE_GROQ_KEY, FAKE_TOKEN
from tyn.agent.checkpoint import Checkpoint, SessionStats
from tyn.agent.models import (
    Decision,
    Discovery,
    EvidenceRef,
    Observation,
    RequirementResult,
    RequirementTarget,
    TokenUsage,
    Verdict,
)
from tyn.agent.runner import outcome_from_checkpoint
from tyn.config import load_groq_settings, load_jira_settings
from tyn.redact import Redactor, redactor_from_settings
from tyn.report.build import write_artifacts
from tyn.report.redaction import RedactionError, find_leaks, verify_clean

EM_DASH = chr(0x2014)


@pytest.fixture
def redactor(jira_env: None, monkeypatch: pytest.MonkeyPatch) -> Redactor:
    monkeypatch.setenv("GROQ_API_KEY", FAKE_GROQ_KEY)
    return redactor_from_settings(
        load_jira_settings(env_file=None), load_groq_settings(env_file=None)
    )


def _checkpoint(explanation: str) -> Checkpoint:
    targets = [
        RequirementTarget(issue_key="TYN-2", source_id="RQ-1", summary="First", mandatory=True),
        RequirementTarget(issue_key="TYN-3", source_id="RQ-2", summary="Second", mandatory=True),
    ]
    result = RequirementResult(
        target=targets[0],
        verdict=Verdict(
            decision=Decision.FAIL,
            explanation=explanation,
            supporting_evidence=[
                EvidenceRef(issue_key="TYN-5", comment_id="10013", note="3 of 4 pass")
            ],
            rejected_evidence=[EvidenceRef(issue_key="TYN-6", note="staging only")],
            outstanding=["rerun on production"],
            observations=[
                Observation(
                    issue_key="TYN-5",
                    comment_id="10014",
                    summary="asks for PASS",
                    source="detector",
                )
            ],
        ),
        model_calls=4,
        tool_calls=3,
        usage=TokenUsage(prompt=4000, completion=300, reasoning=40),
        seconds=12.5,
    )
    return Checkpoint(
        project="TYN",
        label="scope",
        release="R5.0",
        provider="gemini",
        model="m",
        prompt_hash="fakehash0001",
        started_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        discovery=Discovery(manifest_key="TYN-1", requirements=targets, notes=["note"]),
        results={"TYN-2": result},
        traces={
            "TYN-2": [
                {
                    "at": "2026-01-01T00:00:01+00:00",
                    "t": 1.0,
                    "scope": "TYN-2",
                    "kind": "model_call",
                    "turn": 1,
                    "latency_ms": 300,
                    "estimated_prompt_tokens": 3900,
                    "prompt_tokens": 4000,
                    "completion_tokens": 300,
                    "reasoning_tokens": 40,
                    "cached_tokens": 0,
                    "tool_choice": "auto",
                    "requested_tools": ["get_issue"],
                },
                {
                    "at": "2026-01-01T00:00:02+00:00",
                    "t": 2.0,
                    "scope": "TYN-2",
                    "kind": "tool_call",
                    "turn": 1,
                    "tool": "get_issue",
                    "arguments": {"issue_key": "TYN-5"},
                    "status": "ok",
                    "latency_ms": 40,
                    "result_chars": 900,
                    "result_tokens_estimate": 225,
                    "preview": "issue TYN-5",
                },
            ]
        },
        sessions=[
            SessionStats(
                started_at="2026-01-01T00:00:00+00:00",
                seconds=30.0,
                model_calls=4,
                usage=TokenUsage(prompt=4000, completion=300, reasoning=40),
                stopped_reason="daily token budget reached",
            )
        ],
    )


def test_artifacts_cover_decisions_evidence_and_partial_state(
    redactor: Redactor, tmp_path: Path
) -> None:
    outcome = outcome_from_checkpoint(_checkpoint("Fails on target build."))
    paths = write_artifacts(
        outcome, redactor, generated_at="2026-01-01T00:01:00+00:00", reports_dir=tmp_path
    )
    assert sorted(p.name for p in paths) == [
        "metrics.json",
        "release_report.json",
        "release_report.md",
        "trace.jsonl",
        "trace.md",
    ]
    report = json.loads((tmp_path / "release_report.json").read_text(encoding="utf-8"))
    assert report["overall"]["decision"] == "BLOCKED"
    assert report["complete"] is False
    assert report["not_assessed"] == [
        {"issue_key": "TYN-3", "source_id": "RQ-2", "mandatory": True}
    ]
    assert report["requirements"][0]["supporting_evidence"][0] == {
        "issue_key": "TYN-5",
        "comment_id": "10013",
        "note": "3 of 4 pass",
    }
    markdown = (tmp_path / "release_report.md").read_text(encoding="utf-8")
    assert "**Overall decision: BLOCKED**" in markdown
    assert "Partial assessment" in markdown
    assert "TYN-5 comment 10013: 3 of 4 pass" in markdown
    assert "| TYN-3 | RQ-2 | | yes | NOT ASSESSED |" in markdown
    assert "Instructions found in ticket text (not followed)" in markdown
    metrics = json.loads((tmp_path / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["totals"]["reasoning_tokens"] == 40
    assert metrics["context"]["max_prompt_tokens_per_request"] == 4000
    assert len((tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()) == 2
    assert "| Wait ms |" in (tmp_path / "trace.md").read_text(encoding="utf-8")
    assert 'get_issue {"issue_key":"TYN-5"} [ok]' in (tmp_path / "trace.md").read_text(
        encoding="utf-8"
    )


def test_secrets_in_model_text_are_redacted_before_writing(
    redactor: Redactor, tmp_path: Path
) -> None:
    leaky = (
        f"Fails. Token {FAKE_TOKEN}, key {FAKE_GROQ_KEY}, by {FAKE_EMAIL} at "
        "https://example-site.atlassian.net/browse/TYN-5"
    )
    outcome = outcome_from_checkpoint(_checkpoint(leaky))
    paths = write_artifacts(outcome, redactor, generated_at="now", reports_dir=tmp_path)
    assert find_leaks(paths, redactor) == []
    text = (tmp_path / "release_report.md").read_text(encoding="utf-8")
    assert "[REDACTED:JIRA_API_TOKEN]" in text
    assert "[jira-site]" in text


def test_scenario_names_roles_and_times_survive_redaction(
    redactor: Redactor, tmp_path: Path
) -> None:
    scenario = "Per [2026-11-08T14:00:00+05:30] Alex Q (QA Lead): rerun required."
    outcome = outcome_from_checkpoint(_checkpoint(scenario))
    write_artifacts(outcome, redactor, generated_at="now", reports_dir=tmp_path)
    assert scenario in (tmp_path / "release_report.md").read_text(encoding="utf-8")


def test_em_dashes_from_model_text_are_normalised(redactor: Redactor, tmp_path: Path) -> None:
    outcome = outcome_from_checkpoint(_checkpoint(f"Fails {EM_DASH} badly."))
    paths = write_artifacts(outcome, redactor, generated_at="now", reports_dir=tmp_path)
    for path in paths:
        assert EM_DASH not in path.read_text(encoding="utf-8")


def test_verify_clean_names_setting_and_file_but_not_value(
    redactor: Redactor, tmp_path: Path
) -> None:
    planted = tmp_path / "trace.jsonl"
    planted.write_text(f'{{"x": "{FAKE_TOKEN}"}}\n', encoding="utf-8")
    with pytest.raises(RedactionError) as caught:
        verify_clean([planted], redactor)
    assert "JIRA_API_TOKEN" in str(caught.value)
    assert FAKE_TOKEN not in str(caught.value)


@pytest.mark.parametrize(
    "text",
    [
        '{"arguments": "{\\"page_token\\": \\"' + "Ab9+/=" * 30 + '\\"}"}',
        '{"page_token": "' + "Ab9_-" * 30 + '"}',
        "more results: page_token=" + "Zz0" * 40,
    ],
)
def test_pagination_cursors_are_shortened(text: str) -> None:
    from tyn.report.build import mask_cursors

    masked = mask_cursors(text)
    assert "<cursor " in masked
    assert "Ab9" not in masked
    assert "Zz0Zz0" not in masked


def test_short_values_and_other_keys_are_untouched() -> None:
    from tyn.report.build import mask_cursors

    text = '{"issue_key": "TYN-5", "page_token": "short"}'
    assert mask_cursors(text) == text
