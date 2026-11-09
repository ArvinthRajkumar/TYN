"""Redaction of committed artifacts.

Artifacts are redacted before they are written and verified after: the
configured Jira token, email and site host and the Groq key (taken from the
environment through ``tyn.config``) must not appear in any file under
``reports/``. Scenario actor names, roles and timestamps are untouched.
"""

from pathlib import Path

from tyn.errors import TynError
from tyn.redact import Redactor


class RedactionError(TynError):
    """A configured secret was found in an artifact after redaction."""


def find_leaks(paths: list[Path], redactor: Redactor) -> list[str]:
    """Return ``"<setting> in <file>"`` for every configured value found."""
    leaks: list[str] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        leaks.extend(f"{name} in {path.as_posix()}" for name in redactor.leaks(text))
    return leaks


def verify_clean(paths: list[Path], redactor: Redactor) -> None:
    """Raise ``RedactionError`` naming the setting and file of any leak."""
    leaks = find_leaks(paths, redactor)
    if leaks:
        raise RedactionError("secret values found in artifacts: " + "; ".join(leaks))
