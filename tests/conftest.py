"""Shared fixtures: an isolated environment with fake configuration values."""

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

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
