"""Scope fact extraction: phrasings, comparisons and the unknown default."""

import pytest

from tyn.agent.scope import TextUnit, compare, extract, run_ids, scan_units, snippet_for

TARGET = {
    "release": "R2.0",
    "commit": "abc1234",
    "service": "svc-a",
    "environment": "production",
    "prompt": "p4",
}


def _values(text: str) -> dict[str, str]:
    return {dimension: value for dimension, value, _ in extract(text)}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Run X-1: release R2.0; commit abc1234; service svc-a; environment production; "
            "prompt p4.",
            {
                "release": "R2.0",
                "commit": "abc1234",
                "service": "svc-a",
                "environment": "production",
                "prompt": "p4",
            },
        ),
        (
            "Approved: release R2.0, commit abc1234, service svc-a. Approval scope: staging.",
            {"release": "R2.0", "commit": "abc1234", "service": "svc-a", "environment": "staging"},
        ),
        (
            "Approved release R2.0, commit abc1234, service svc-a for production from an "
            "engineering standpoint.",
            {
                "release": "R2.0",
                "commit": "abc1234",
                "service": "svc-a",
                "environment": "production",
            },
        ),
        ("Harness configured with prompt bundle p3 (frozen last week).", {"prompt": "p3"}),
        (
            "Environment: Production. Prompt version: p4",
            {"environment": "Production", "prompt": "p4"},
        ),
        ("Deployed to prod for the canary.", {"environment": "prod"}),
        (
            "Release: v3.1.0-rc1 on commit ABC1234DEF",
            {"release": "v3.1.0-rc1", "commit": "ABC1234DEF"},  # pragma: allowlist secret
        ),
    ],
)
def test_extracts_common_phrasings(text: str, expected: dict[str, str]) -> None:
    assert _values(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Waived by release management for the next cycle.",
        "The service is down and the environment is noisy.",
        "Measured in production-equivalent load tests.",
        "Picked this up. Will update once the change is in review.",
        "Committed to fixing it soon.",
    ],
)
def test_prose_without_scope_values_extracts_nothing(text: str) -> None:
    assert extract(text) == []


def test_comparisons_normalise_case_aliases_and_sha_prefixes() -> None:
    assert compare("environment", "Prod", "production") == "match"
    assert compare("environment", "staging", "production") == "mismatch"
    assert compare("commit", "abc1234def5678", "abc1234") == "match"  # pragma: allowlist secret
    assert compare("commit", "abc1235", "abc1234") == "mismatch"
    assert compare("release", "r2.0", "R2.0") == "match"
    assert compare("prompt", "p4", None) == "no target"


def test_dimensions_default_to_unknown_without_values() -> None:
    facts = scan_units([TextUnit("1", None, None, None, "Status updated.")], TARGET)
    assert facts.units == []
    assert set(facts.summary.values()) == {"unknown"}


def test_prompt_in_separate_config_comment_is_reported_with_its_run_id() -> None:
    units = [
        TextUnit(
            "11",
            "2026-01-01T09:00:00Z",
            "Q",
            "QA Lead",
            "Evaluation EV-55 configured with prompt bundle p3.",
        ),
        TextUnit(
            "12",
            "2026-01-02T09:00:00Z",
            "Q",
            "QA Lead",
            "EV-55 completed: release R2.0; commit abc1234; service svc-a; environment production. "
            "Accuracy 96 percent.",
        ),
    ]
    facts = scan_units(units, TARGET)
    by_comment = {f.unit.comment_id: f for f in facts.units}
    assert by_comment["11"].run_ids == ["EV-55"]
    assert by_comment["12"].run_ids == ["EV-55"]
    assert [(c.dimension, c.comparison) for c in by_comment["11"].candidates] == [
        ("prompt", "mismatch")
    ]
    assert facts.summary["prompt"] == "mismatch"
    assert facts.summary["environment"] == "match"


def test_mixed_values_across_comments_are_reported_as_mixed() -> None:
    units = [
        TextUnit("1", None, None, None, "Run A-1: environment staging."),
        TextUnit("2", None, None, None, "Run A-2: environment production."),
    ]
    assert scan_units(units, TARGET).summary["environment"] == "mixed"


def test_snippet_is_verbatim_and_covers_every_matched_sentence() -> None:
    text = "Approved: release R2.0. Unrelated words here. Approval scope: staging. Tail."
    positions = [position for _, _, position in extract(text)]
    snippet = snippet_for(text, positions)
    assert snippet == "Approved: release R2.0. ... Approval scope: staging."
    for part in snippet.split(" ... "):
        assert part in text


def test_run_ids_skip_project_issue_keys() -> None:
    assert run_ids("See TYN-12 and run REG-80 and B-9", ("TYN-",)) == ["REG-80", "B-9"]
