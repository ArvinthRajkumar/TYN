"""Redaction of configured secrets in text and log records."""

import base64
import io
import logging

import pytest

from tests.conftest import FAKE_EMAIL, FAKE_GROQ_KEY, FAKE_TOKEN
from tyn.config import load_groq_settings, load_jira_settings
from tyn.logging_setup import configure_logging
from tyn.redact import Redactor, SecretValue, redactor_from_settings


@pytest.fixture
def redactor(jira_env: None, monkeypatch: pytest.MonkeyPatch) -> Redactor:
    monkeypatch.setenv("GROQ_API_KEY", FAKE_GROQ_KEY)
    return redactor_from_settings(
        load_jira_settings(env_file=None), load_groq_settings(env_file=None)
    )


def test_configured_values_are_masked(redactor: Redactor) -> None:
    text = (
        f"token={FAKE_TOKEN} key={FAKE_GROQ_KEY} by {FAKE_EMAIL.lower()} "
        "at https://EXAMPLE-SITE.atlassian.net/rest/api/3/issue/TYN-1"
    )
    cleaned = redactor.redact(text)
    for value in (FAKE_TOKEN, FAKE_GROQ_KEY, FAKE_EMAIL.lower(), "example-site"):
        assert value.lower() not in cleaned.lower()
    assert "[jira-site]" in cleaned
    assert "TYN-1" in cleaned


def test_basic_auth_header_value_is_masked(redactor: Redactor) -> None:
    encoded = base64.b64encode(f"{FAKE_EMAIL}:{FAKE_TOKEN}".encode()).decode()
    cleaned = redactor.redact(f"Authorization: Basic {encoded}")
    assert encoded not in cleaned
    assert "REDACTED" in cleaned


def test_leaks_reports_names_not_values(redactor: Redactor) -> None:
    found = redactor.leaks(f"x {FAKE_TOKEN} y")
    assert found == ["JIRA_API_TOKEN"]


def test_generic_patterns_without_configuration() -> None:
    cleaned = Redactor([]).redact("Bearer abcdefghijklmnop and gsk_" + "A" * 30)
    assert "abcdefghijklmnop" not in cleaned
    assert "gsk_AAAA" not in cleaned


def test_short_values_are_not_treated_as_secrets() -> None:
    assert Redactor([SecretValue("X", "ab", "[R]")]).redact("tab") == "tab"


def test_scenario_text_is_preserved(redactor: Redactor) -> None:
    text = "[2026-01-02T10:00:00+05:30] Alex Q (QA Lead): Run X-1 passed."
    assert redactor.redact(text) == text


def test_logging_filter_masks_message_args_and_exceptions(redactor: Redactor) -> None:
    stream = io.StringIO()
    configure_logging("INFO", redactor, stream=stream)
    log = logging.getLogger("tyn.test")
    log.info("calling with %s", FAKE_TOKEN)
    try:
        raise RuntimeError(f"boom {FAKE_GROQ_KEY}")
    except RuntimeError:
        log.exception("failed for %s", FAKE_EMAIL)
    output = stream.getvalue()
    assert FAKE_TOKEN not in output
    assert FAKE_GROQ_KEY not in output
    assert FAKE_EMAIL not in output
    assert "RuntimeError" in output
