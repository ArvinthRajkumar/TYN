"""Configuration validation: fail fast, name the variable, never echo values."""

import pytest

from tests.conftest import FAKE_EMAIL, FAKE_GROQ_KEY, FAKE_TOKEN
from tyn.config import (
    DEFAULT_GROQ_MODEL,
    load_groq_settings,
    load_jira_settings,
    validate_jira_base_url,
    validate_project_key,
)
from tyn.errors import ConfigError


def test_missing_variables_are_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIRA_BASE_URL", "https://example-site.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", FAKE_EMAIL)
    with pytest.raises(ConfigError) as caught:
        load_jira_settings(env_file=None)
    message = str(caught.value)
    assert "JIRA_API_TOKEN is not set" in message
    assert "JIRA_PROJECT_KEY is not set" in message
    assert FAKE_EMAIL not in message


@pytest.mark.usefixtures("jira_env")
def test_invalid_value_message_does_not_echo_it(monkeypatch: pytest.MonkeyPatch) -> None:
    secret_looking = "http://secret-host-value.example.org/path"  # pragma: allowlist secret
    monkeypatch.setenv("JIRA_BASE_URL", secret_looking)
    with pytest.raises(ConfigError) as caught:
        load_jira_settings(env_file=None)
    assert "JIRA_BASE_URL is invalid" in str(caught.value)
    assert "secret-host-value" not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.usefixtures("jira_env")
def test_valid_settings_hide_secrets_in_repr() -> None:
    settings = load_jira_settings(env_file=None)
    assert settings.jira_base_url == "https://example-site.atlassian.net"
    assert settings.host == "example-site.atlassian.net"
    assert FAKE_TOKEN not in repr(settings)
    assert FAKE_EMAIL not in repr(settings)
    assert settings.jira_api_token.get_secret_value() == FAKE_TOKEN


@pytest.mark.parametrize(
    "url",
    [
        "http://site.atlassian.net",
        "https://site.atlassian.net.evil.com",
        "https://evil.com",
        "https://site.atlassian.net/jira",
        "https://user:pw@site.atlassian.net",  # pragma: allowlist secret
        "https://site.atlassian.net:8443",
        "https://site.atlassian.net/?x=1",
        "https://10.0.0.1",
    ],
)
def test_base_url_rejects_unsafe_values(url: str) -> None:
    with pytest.raises(ValueError, match="must"):
        validate_jira_base_url(url)


def test_base_url_normalises_case_and_trailing_slash() -> None:
    assert validate_jira_base_url(" https://My-Site.Atlassian.net/ ") == (
        "https://my-site.atlassian.net"
    )


@pytest.mark.parametrize("key", ["tyn", "T", "1ABC", "TOOLONGPROJECTKEY", "AB-1"])
def test_project_key_rejects_invalid(key: str) -> None:
    with pytest.raises(ValueError, match="project key"):
        validate_project_key(key)


def test_groq_defaults_and_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", FAKE_GROQ_KEY)
    settings = load_groq_settings(env_file=None)
    assert settings.groq_model == DEFAULT_GROQ_MODEL
    assert settings.groq_tpm_limit == 8_000
    assert settings.groq_tpd_limit == 200_000
    assert FAKE_GROQ_KEY not in repr(settings)


def test_groq_rejects_non_positive_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", FAKE_GROQ_KEY)
    monkeypatch.setenv("GROQ_TPM_LIMIT", "0")
    with pytest.raises(ConfigError, match="GROQ_TPM_LIMIT is invalid"):
        load_groq_settings(env_file=None)


def test_groq_missing_key_is_named() -> None:
    with pytest.raises(ConfigError, match="GROQ_API_KEY is not set"):
        load_groq_settings(env_file=None)


def test_env_file_is_read_with_utf8(tmp_path_factory: pytest.TempPathFactory) -> None:
    env_dir = tmp_path_factory.mktemp("env")
    env_file = env_dir / ".env"
    env_file.write_text(
        "JIRA_BASE_URL=https://example-site.atlassian.net\n"
        f"JIRA_EMAIL={FAKE_EMAIL}\nJIRA_API_TOKEN={FAKE_TOKEN}\nJIRA_PROJECT_KEY=TYN\n",
        encoding="utf-8",
    )
    assert load_jira_settings(env_file=env_file).jira_project_key == "TYN"
