"""Shared fixtures: a synthetic dataset and an isolated environment.

The dataset here is invented for tests. It mirrors the structure of the
supplied file, not its content.
"""

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tyn.importer.dataset import Dataset

CONFIG_VARS = (
    "JIRA_BASE_URL",
    "JIRA_EMAIL",
    "JIRA_API_TOKEN",
    "JIRA_PROJECT_KEY",
    "GROQ_API_KEY",
    "GROQ_MODEL",
    "GROQ_TPM_LIMIT",
    "GROQ_TPD_LIMIT",
)

FAKE_TOKEN = "fake-jira-token-0123456789"
FAKE_EMAIL = "Tester@Example.com"
FAKE_GROQ_KEY = "fake-groq-key-0123456789"


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Remove real configuration and run each test in an empty directory.

    Root logging handlers are restored afterwards because ``configure_logging``
    replaces them, and a handler bound to a closed capture stream would break
    later tests.
    """
    for name in CONFIG_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)


@pytest.fixture
def jira_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set a complete, valid Jira configuration with fake values."""
    monkeypatch.setenv("JIRA_BASE_URL", "https://example-site.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", FAKE_EMAIL)
    monkeypatch.setenv("JIRA_API_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("JIRA_PROJECT_KEY", "TYN")


def _comment(ts: str, actor: str, role: str, body: str) -> str:
    return f"Recorded event {ts} | actor {actor} | role {role}\n{body}"


def sample_payload() -> dict[str, Any]:
    """Return a small dataset with every structural feature the importer handles."""
    target = {
        "release": "R9.1",
        "commit": "abc1234",
        "prompt": "p3",
        "environment": "production",
        "service": "demo-svc",
    }
    return {
        "dataset_label": "tyn-test-scope",
        "target": target,
        "note": "Synthetic test data.",
        "records": [
            {
                "source_id": "EV-1",
                "kind": "evidence",
                "summary": "Check run - First requirement",
                "release": "R9.1",
                "recorded_status": "In Progress",
                "description": "Engineering record.\nResults are in the comments.",
                "comments": [
                    _comment("2026-01-02T10:00:00+05:30", "Alex Q", "QA Lead", "Run X-1 passed."),
                    _comment("2026-01-01T09:00:00+05:30", "Sam D", "Developer", "Picked up."),
                    _comment(
                        "2026-01-03T11:00:00+05:30", "Alex Q", "QA Lead", "Line one\nLine two"
                    ),
                ],
                "links": ["EV-2"],
            },
            {
                "source_id": "EV-2",
                "kind": "evidence",
                "summary": "Dependency check - First requirement",
                "release": "R9.1",
                "recorded_status": "To Do",
                "description": "Engineering record.",
                "comments": [
                    _comment("2026-01-04T10:00:00+05:30", "Alex Q", "QA Lead", "Dependency ok."),
                ],
                "links": [],
            },
            {
                "source_id": "WK-1",
                "kind": "work",
                "summary": "Background: other-svc R8.0",
                "release": "R8.0",
                "recorded_status": "Done",
                "description": "Engineering record.",
                "comments": ["no recorded header here"],
                "links": ["EV-1"],
            },
            {
                "source_id": "REQ-A",
                "kind": "requirement",
                "summary": "First requirement",
                "release": "R9.1",
                "recorded_status": "Done",
                "description": "Requirement ID: REQ-A\nMandatory: yes\nAcceptance criterion: X.",
                "comments": [
                    _comment("2026-01-05T10:00:00+05:30", "Sam D", "Developer", "Marked Done."),
                ],
                "links": ["EV-1"],
            },
            {
                "source_id": "MANIFEST",
                "kind": "manifest",
                "summary": "Release manifest",
                "release": "R9.1",
                "recorded_status": "Done",
                "description": "Target: {}\nRules.",
                "comments": [],
                "links": ["REQ-A"],
            },
        ],
    }


@pytest.fixture
def dataset() -> Dataset:
    """Return the synthetic dataset as a validated model."""
    return Dataset.model_validate(sample_payload())
