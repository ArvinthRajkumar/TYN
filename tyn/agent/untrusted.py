"""Handling of untrusted Jira text before it reaches the model.

Every tool result is wrapped in a ``<jira_data>`` envelope. Ticket text that
contains the envelope tag is escaped so it cannot close the envelope early.
A generic detector flags text that addresses automated reviewers or tries
to dictate an outcome; flagged text is still shown (it is evidence that
someone tried) but marked, and every hit is reported as an observation.
"""

import re
from typing import Final

ENVELOPE_TAG: Final = "jira_data"
_TAG_PATTERN: Final = re.compile(rf"<(/?)\s*{ENVELOPE_TAG}", re.IGNORECASE)

_INSTRUCTION_PATTERNS: Final = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier|other)\s+"
        r"(?:instructions|rules|guidance)",
        r"\b(?:note|message|instructions?)\s+(?:for|to)\s+(?:automated|ai|llm|bot|machine)",
        r"\b(?:automated|ai|llm)\s+(?:release\s+)?(?:review(?:er)?s?|agents?|assistants?)\b",
        r"\b(?:report|mark|treat|record|set|classify)\s+(?:\S+\s+){0,3}as\s+"
        r"(?:pass(?:ed)?|approved|ready|fail(?:ed)?|green)\b",
        r"\bdo\s+not\s+(?:mention|report|disclose|reveal|flag)\b",
        r"\b(?:disregard|override)\s+(?:the\s+)?(?:rules|policy|criteria|instructions)\b",
        r"\byou\s+(?:are|must|should)\s+now\b",
        r"\bsystem\s+prompt\b",
    )
)


def escape_envelope(text: str) -> str:
    """Neutralise envelope tags inside untrusted text."""
    return _TAG_PATTERN.sub(lambda m: f"&lt;{m.group(1)}{ENVELOPE_TAG}", text)


def wrap(tool_name: str, body: str) -> str:
    """Wrap a tool result body in the untrusted-data envelope."""
    return (
        f'<{ENVELOPE_TAG} tool="{tool_name}" trust="untrusted">\n'
        f"{escape_envelope(body)}\n</{ENVELOPE_TAG}>"
    )


def instruction_hits(text: str) -> list[str]:
    """Return the phrases in ``text`` that look like instructions to a reviewer."""
    hits: list[str] = []
    for pattern in _INSTRUCTION_PATTERNS:
        match = pattern.search(text)
        if match and match.group(0) not in hits:
            hits.append(match.group(0))
    return hits
