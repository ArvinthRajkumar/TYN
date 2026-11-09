"""Tolerant parsing of tool-call arguments produced by the model.

Malformed JSON never crashes the loop: a few common slips are repaired, and
anything else becomes a short error returned to the model as the tool
result so it can resend the call.
"""

import json
import re
from typing import Any, Final

_FENCE: Final = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")
_TRAILING_COMMA: Final = re.compile(r",\s*([}\]])")


class ArgumentError(ValueError):
    """Arguments could not be parsed into a JSON object."""


def parse_arguments(raw: str | None) -> dict[str, Any]:
    """Return the arguments as a dict or raise ``ArgumentError`` with a short reason."""
    text = (raw or "").strip()
    if not text:
        return {}
    for candidate in _candidates(text):
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
        raise ArgumentError("arguments must be a JSON object")
    raise ArgumentError("arguments were not valid JSON; resend the call with a JSON object")


def _candidates(text: str) -> list[str]:
    stripped = _FENCE.sub("", text)
    candidates = [text, stripped, _TRAILING_COMMA.sub(r"\1", stripped)]
    start, end = stripped.find("{"), stripped.rfind("}")
    if 0 <= start < end:
        inner = stripped[start : end + 1]
        candidates += [inner, _TRAILING_COMMA.sub(r"\1", inner)]
    return list(dict.fromkeys(candidates))
