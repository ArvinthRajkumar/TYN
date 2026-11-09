"""Safe JQL construction.

JQL is always assembled in code from validated pieces. Values are quoted and
escaped; the project and scope label clauses are always ANDed in front so no
caller can widen a query beyond the imported dataset.
"""

import re
from typing import Final

from tyn.errors import TynError

_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f]")
_MAX_VALUE_LENGTH: Final = 200


class JqlValueError(TynError):
    """A value cannot be used safely inside JQL."""


def quote(value: str) -> str:
    """Return ``value`` as a double-quoted, escaped JQL string literal."""
    if _CONTROL.search(value):
        raise JqlValueError("JQL values must not contain control characters")
    if len(value) > _MAX_VALUE_LENGTH:
        raise JqlValueError(f"JQL values are limited to {_MAX_VALUE_LENGTH} characters")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def scoped(project: str, scope_label: str, *clauses: str) -> str:
    """Return JQL restricted to one project and one scope label.

    ``clauses`` must themselves be produced by this module's helpers.
    """
    parts = [f"project = {quote(project)}", f"labels = {quote(scope_label)}"]
    parts.extend(f"({clause})" for clause in clauses if clause)
    return " AND ".join(parts)


def label_equals(label: str) -> str:
    """Return a clause matching issues that carry ``label``."""
    return f"labels = {quote(label)}"


def label_in(labels: list[str]) -> str:
    """Return a clause matching issues that carry any of ``labels``."""
    if not labels:
        raise JqlValueError("label_in needs at least one label")
    return "labels in (" + ", ".join(quote(label) for label in labels) + ")"


_ISSUE_KEY: Final = re.compile(r"^[A-Z][A-Z0-9_]{1,9}-\d{1,7}$")
# Characters with meaning in Jira's text search syntax are replaced by spaces,
# so a summary search can only ever match words.
_TEXT_UNSAFE: Final = re.compile(r"[^A-Za-z0-9 ]+")


def is_issue_key(value: str) -> bool:
    """Return True for a syntactically valid Jira issue key."""
    return bool(_ISSUE_KEY.fullmatch(value))


def key_in(keys: list[str]) -> str:
    """Return a clause matching any of ``keys``; every key is validated."""
    if not keys:
        raise JqlValueError("key_in needs at least one key")
    bad = [key for key in keys if not is_issue_key(key)]
    if bad:
        raise JqlValueError(f"invalid issue keys: {', '.join(bad[:3])}")
    return "key in (" + ", ".join(keys) + ")"


def summary_contains(text: str) -> str:
    """Return a summary text-search clause built from plain words only."""
    words = " ".join(_TEXT_UNSAFE.sub(" ", text).split())
    if not words:
        raise JqlValueError("summary search needs at least one letter or digit")
    return f"summary ~ {quote(words)}"
