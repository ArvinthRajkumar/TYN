"""Configuration loaded only from the environment and the local ``.env`` file.

Secrets are held as ``SecretStr`` so they never appear in reprs or logs.
Validation errors are rewritten to name the variable without echoing its
value, because pydantic's default error text includes the rejected input.
"""

import re
from pathlib import Path
from typing import Final, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from tyn.errors import ConfigError

DEFAULT_ENV_FILE: Final = Path(".env")
DEFAULT_GROQ_MODEL: Final = "openai/gpt-oss-120b"
DEFAULT_GEMINI_MODEL: Final = "gemini-3.8-flash"

# Atlassian Cloud sites live on <site>.atlassian.net. Anything else (other
# hosts, IP addresses, plain http) is rejected so credentials cannot be sent to
# an unexpected server through a mistyped or tampered variable.
_ATLASSIAN_HOST = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}\.atlassian\.net$")
_PROJECT_KEY = re.compile(r"^[A-Z][A-Z0-9_]{1,9}$")
_MODEL_ID = re.compile(r"^[A-Za-z0-9._/-]{1,128}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# An empty variable (for example "GEMINI_MODEL=") counts as unset, so the
# documented default applies instead of failing validation on "".
_COMMON_CONFIG = SettingsConfigDict(
    env_file_encoding="utf-8",
    extra="ignore",
    case_sensitive=False,
    env_ignore_empty=True,
)


def validate_jira_base_url(raw: str) -> str:
    """Return the normalised site root or raise ``ValueError``.

    The message never contains the rejected value.
    """
    parts = urlsplit(raw.strip())
    if parts.scheme != "https":
        raise ValueError("must use https")
    if parts.username or parts.password:
        raise ValueError("must not contain credentials")
    if parts.port is not None:
        raise ValueError("must not specify a port")
    if parts.query or parts.fragment:
        raise ValueError("must not contain a query or fragment")
    if parts.path not in ("", "/"):
        raise ValueError("must be the site root without a path")
    host = (parts.hostname or "").lower()
    if not _ATLASSIAN_HOST.fullmatch(host):
        raise ValueError("must be an https://<site>.atlassian.net address")
    return f"https://{host}"


class JiraSettings(BaseSettings):
    """Jira connection settings used by the importer and the agent."""

    model_config = _COMMON_CONFIG

    jira_base_url: str = Field(description="Jira Cloud site root")
    jira_email: SecretStr = Field(description="Account email for basic auth")
    jira_api_token: SecretStr = Field(description="Dedicated, revocable API token")
    jira_project_key: str = Field(description="Default project key")

    @field_validator("jira_base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        return validate_jira_base_url(value)

    @field_validator("jira_email")
    @classmethod
    def _check_email(cls, value: SecretStr) -> SecretStr:
        if not _EMAIL.fullmatch(value.get_secret_value().strip()):
            raise ValueError("must be an email address")
        return SecretStr(value.get_secret_value().strip())

    @field_validator("jira_api_token")
    @classmethod
    def _check_token(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("must not be empty")
        return SecretStr(value.get_secret_value().strip())

    @field_validator("jira_project_key")
    @classmethod
    def _check_project_key(cls, value: str) -> str:
        return validate_project_key(value)

    @property
    def host(self) -> str:
        """Hostname of the Jira site, used for redaction."""
        return urlsplit(self.jira_base_url).hostname or ""


class GroqSettings(BaseSettings):
    """Groq model settings used by the agent and the smoke test."""

    model_config = _COMMON_CONFIG

    groq_api_key: SecretStr = Field(description="Groq API key")
    groq_model: str = Field(default=DEFAULT_GROQ_MODEL, description="Model id")
    groq_tpm_limit: int = Field(default=8_000, gt=0, description="Tokens per minute")
    groq_tpd_limit: int = Field(default=200_000, gt=0, description="Tokens per day")

    @field_validator("groq_api_key")
    @classmethod
    def _check_key(cls, value: SecretStr) -> SecretStr:
        return _clean_secret(value)

    @field_validator("groq_model")
    @classmethod
    def _check_model(cls, value: str) -> str:
        return _clean_model(value)


def _clean_secret(value: SecretStr) -> SecretStr:
    if not value.get_secret_value().strip():
        raise ValueError("must not be empty")
    return SecretStr(value.get_secret_value().strip())


def _clean_model(value: str) -> str:
    if not _MODEL_ID.fullmatch(value.strip()):
        raise ValueError("must be a model id such as vendor/model-name")
    return value.strip()


class ModelSettings(BaseSettings):
    """Which model provider the agent uses and the run-wide token cap."""

    model_config = _COMMON_CONFIG

    llm_provider: Literal["gemini", "groq"] = Field(
        default="gemini", description="Model provider for the agent"
    )
    max_run_tokens: int = Field(
        default=400_000,
        gt=0,
        description="Hard cap on prompt plus output tokens for one assess invocation",
    )


class GeminiSettings(BaseSettings):
    """Gemini API settings used by the agent and the smoke test."""

    model_config = _COMMON_CONFIG

    gemini_api_key: SecretStr = Field(description="Gemini API key")
    gemini_model: str = Field(default=DEFAULT_GEMINI_MODEL, description="Model id")
    gemini_rpm_limit: int = Field(default=10, gt=0, description="Requests per minute")
    gemini_tpm_limit: int = Field(default=250_000, gt=0, description="Input tokens per minute")
    gemini_tpd_limit: int | None = Field(
        default=None, gt=0, description="Optional tokens per day; unset means no daily cap"
    )
    gemini_thinking_level: Literal["minimal", "low", "medium", "high"] = Field(
        default="low", description="Thinking effort; low keeps cost and latency down"
    )

    @field_validator("gemini_api_key")
    @classmethod
    def _check_key(cls, value: SecretStr) -> SecretStr:
        return _clean_secret(value)

    @field_validator("gemini_model")
    @classmethod
    def _check_model(cls, value: str) -> str:
        return _clean_model(value)


def validate_project_key(value: str) -> str:
    """Return a valid Jira project key or raise ``ValueError``."""
    if not _PROJECT_KEY.fullmatch(value.strip()):
        raise ValueError("must be an uppercase Jira project key such as TYN")
    return value.strip()


def _describe(exc: ValidationError) -> str:
    """Build a message naming each bad variable without its value."""
    problems: list[str] = []
    for error in exc.errors(include_input=False, include_url=False):
        name = str(error["loc"][0]).upper() if error["loc"] else "configuration"
        if error["type"] == "missing":
            problems.append(f"{name} is not set")
        else:
            reason = str(error["msg"]).removeprefix("Value error, ")
            problems.append(f"{name} is invalid: {reason}")
    return "Configuration error: " + "; ".join(problems) + ". See .env.example."


def load_jira_settings(env_file: Path | None = DEFAULT_ENV_FILE) -> JiraSettings:
    """Load and validate Jira settings, failing fast with a safe message."""
    try:
        return JiraSettings(_env_file=env_file)
    except ValidationError as exc:
        raise ConfigError(_describe(exc)) from None


def load_groq_settings(env_file: Path | None = DEFAULT_ENV_FILE) -> GroqSettings:
    """Load and validate Groq settings, failing fast with a safe message."""
    try:
        return GroqSettings(_env_file=env_file)
    except ValidationError as exc:
        raise ConfigError(_describe(exc)) from None


def load_gemini_settings(env_file: Path | None = DEFAULT_ENV_FILE) -> GeminiSettings:
    """Load and validate Gemini settings, failing fast with a safe message."""
    try:
        return GeminiSettings(_env_file=env_file)
    except ValidationError as exc:
        raise ConfigError(_describe(exc)) from None


def load_model_settings(env_file: Path | None = DEFAULT_ENV_FILE) -> ModelSettings:
    """Load the provider choice and run cap, failing fast with a safe message."""
    try:
        return ModelSettings(_env_file=env_file)
    except ValidationError as exc:
        raise ConfigError(_describe(exc)) from None


def optional_groq_settings(env_file: Path | None = DEFAULT_ENV_FILE) -> GroqSettings | None:
    """Return Groq settings when configured, else None (used for redaction)."""
    try:
        return load_groq_settings(env_file)
    except ConfigError:
        return None


def optional_gemini_settings(env_file: Path | None = DEFAULT_ENV_FILE) -> GeminiSettings | None:
    """Return Gemini settings when configured, else None (used for redaction)."""
    try:
        return load_gemini_settings(env_file)
    except ConfigError:
        return None
