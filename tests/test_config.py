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


def test_gemini_is_the_default_provider_with_documented_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tyn.config import DEFAULT_GEMINI_MODEL, load_gemini_settings, load_model_settings

    monkeypatch.setenv("GEMINI_API_KEY", "fake-gemini-key-0123456789")
    model = load_model_settings(env_file=None)
    assert model.llm_provider == "gemini"
    assert model.max_run_tokens == 400_000
    gemini = load_gemini_settings(env_file=None)
    assert gemini.gemini_model == DEFAULT_GEMINI_MODEL
    assert gemini.gemini_tpd_limit is None
    assert gemini.gemini_thinking_level == "low"
    assert "fake-gemini-key" not in repr(gemini)


def test_gemini_key_missing_is_named() -> None:
    from tyn.config import load_gemini_settings

    with pytest.raises(ConfigError, match="GEMINI_API_KEY is not set"):
        load_gemini_settings(env_file=None)


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("LLM_PROVIDER", "openai", "LLM_PROVIDER is invalid"),
        ("MAX_RUN_TOKENS", "0", "MAX_RUN_TOKENS is invalid"),
    ],
)
def test_model_settings_validation_names_the_variable(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, expected: str
) -> None:
    from tyn.config import load_model_settings

    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError, match=expected):
        load_model_settings(env_file=None)


def test_rejected_provider_value_is_not_echoed(monkeypatch: pytest.MonkeyPatch) -> None:
    from tyn.config import load_model_settings

    monkeypatch.setenv("LLM_PROVIDER", "secret-looking-provider")
    with pytest.raises(ConfigError) as caught:
        load_model_settings(env_file=None)
    assert "secret-looking-provider" not in str(caught.value)


def test_invalid_gemini_limits_are_named(monkeypatch: pytest.MonkeyPatch) -> None:
    from tyn.config import load_gemini_settings

    monkeypatch.setenv("GEMINI_API_KEY", "fake-gemini-key-0123456789")
    monkeypatch.setenv("GEMINI_RPM_LIMIT", "-1")
    monkeypatch.setenv("GEMINI_MODEL", "bad model id")
    with pytest.raises(ConfigError) as caught:
        load_gemini_settings(env_file=None)
    assert "GEMINI_RPM_LIMIT is invalid" in str(caught.value)
    assert "GEMINI_MODEL is invalid" in str(caught.value)
    assert "bad model id" not in str(caught.value)


def test_provider_config_follows_llm_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from tyn.agent import app

    monkeypatch.setenv("GEMINI_API_KEY", "fake-gemini-key-0123456789")
    monkeypatch.setenv("GROQ_API_KEY", FAKE_GROQ_KEY)
    monkeypatch.setattr(app, "load_model_settings", lambda: _model(None))
    monkeypatch.setattr(app, "load_gemini_settings", lambda: _gemini())
    monkeypatch.setattr(app, "load_groq_settings", lambda: load_groq_settings(env_file=None))
    gemini = app.load_provider_config()
    assert (gemini.name, gemini.reserve_output, gemini.requests_per_minute) == ("gemini", False, 10)
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setattr(app, "load_model_settings", lambda: _model(None))
    groq = app.load_provider_config()
    assert (groq.name, groq.reserve_output, groq.tokens_per_day) == ("groq", True, 200_000)


def _model(env_file: None) -> object:
    from tyn.config import load_model_settings

    return load_model_settings(env_file=env_file)


def _gemini() -> object:
    from tyn.config import load_gemini_settings

    return load_gemini_settings(env_file=None)


def test_empty_variables_fall_back_to_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    from tyn.config import DEFAULT_GEMINI_MODEL, load_gemini_settings

    monkeypatch.setenv("GEMINI_API_KEY", "fake-gemini-key-0123456789")
    monkeypatch.setenv("GEMINI_MODEL", "")
    monkeypatch.setenv("GEMINI_TPD_LIMIT", "")
    settings = load_gemini_settings(env_file=None)
    assert settings.gemini_model == DEFAULT_GEMINI_MODEL
    assert settings.gemini_tpd_limit is None


def test_empty_secret_counts_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from tyn.config import load_gemini_settings

    monkeypatch.setenv("GEMINI_API_KEY", "")
    with pytest.raises(ConfigError, match="GEMINI_API_KEY is not set"):
        load_gemini_settings(env_file=None)
