"""Removal of secret values from text before it is logged or written.

Values come from the loaded configuration at runtime, so nothing secret is
ever present in source code. Generic credential patterns are also masked as
a second line of defence for values that were never configured here.
"""

import base64
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from tyn.config import GroqSettings, JiraSettings

# Shorter values would mask ordinary words, so they are not treated as secrets.
_MIN_SECRET_LENGTH: Final = 4

_GENERIC_PATTERNS: Final = (
    (re.compile(r"(?i)(authorization\s*[:=]\s*)\S+(\s+\S+)?"), r"\1[REDACTED:AUTH]"),
    (re.compile(r"(?i)\b(basic|bearer)\s+[A-Za-z0-9+/=._-]{8,}"), r"\1 [REDACTED:AUTH]"),
    (re.compile(r"\bgsk_[A-Za-z0-9]{16,}"), "[REDACTED:GROQ_KEY]"),
    (re.compile(r"\bATATT[A-Za-z0-9_=-]{16,}"), "[REDACTED:ATLASSIAN_TOKEN]"),
)


@dataclass(frozen=True)
class SecretValue:
    """A named secret and the placeholder that replaces it."""

    name: str
    value: str
    placeholder: str
    case_insensitive: bool = False


class Redactor:
    """Replace configured secret values and generic credential patterns."""

    def __init__(self, secrets: Iterable[SecretValue]) -> None:
        usable = [s for s in secrets if len(s.value) >= _MIN_SECRET_LENGTH]
        # Longest first so a secret containing another is masked whole.
        self._secrets = tuple(sorted(usable, key=lambda s: len(s.value), reverse=True))
        self._compiled = tuple(
            (re.compile(re.escape(s.value), re.IGNORECASE if s.case_insensitive else 0), s)
            for s in self._secrets
        )

    def redact(self, text: str) -> str:
        """Return ``text`` with every known secret and credential pattern masked."""
        for pattern, secret in self._compiled:
            text = pattern.sub(secret.placeholder, text)
        for generic, replacement in _GENERIC_PATTERNS:
            text = generic.sub(replacement, text)
        return text

    def leaks(self, text: str) -> list[str]:
        """Return the names of configured secrets present in ``text``.

        Only names are returned so callers can report a leak without
        repeating the value.
        """
        return [secret.name for pattern, secret in self._compiled if pattern.search(text)]


def redactor_from_settings(
    jira: "JiraSettings | None" = None, groq: "GroqSettings | None" = None
) -> Redactor:
    """Build a redactor from whichever settings are loaded."""
    secrets: list[SecretValue] = []
    if jira is not None:
        email = jira.jira_email.get_secret_value()
        token = jira.jira_api_token.get_secret_value()
        basic = base64.b64encode(f"{email}:{token}".encode()).decode("ascii")
        secrets += [
            SecretValue("JIRA_API_TOKEN", token, "[REDACTED:JIRA_API_TOKEN]"),
            SecretValue("JIRA_BASIC_AUTH", basic, "[REDACTED:JIRA_API_TOKEN]"),
            SecretValue("JIRA_EMAIL", email, "[REDACTED:JIRA_EMAIL]", case_insensitive=True),
            SecretValue("JIRA_HOST", jira.host, "[jira-site]", case_insensitive=True),
        ]
    if groq is not None:
        key = groq.groq_api_key.get_secret_value()
        secrets.append(SecretValue("GROQ_API_KEY", key, "[REDACTED:GROQ_API_KEY]"))
    return Redactor(secrets)
