"""Deterministic extraction of scope facts from evidence text.

``check_evidence_scope`` reports facts, not verdicts: for each comment it
lists the release, commit, service, environment and prompt values it can
find, the verbatim snippet they came from, and whether each value equals the
requirement's target. A dimension with no extracted value is ``unknown``.
Whether the evidence is applicable stays the model's judgement.

The patterns are generic phrasings ("release X", "commit <sha>", "prompt
bundle X", "approval scope: X", "for production"); nothing here knows any
ticket, value or expected answer.
"""

import re
from dataclasses import dataclass, field
from typing import Final, Literal

DIMENSIONS: Final = ("release", "commit", "service", "environment", "prompt")
Comparison = Literal["match", "mismatch", "no target"]
DimensionStatus = Literal["match", "mismatch", "mixed", "unknown", "no target"]

_FLAGS: Final = re.IGNORECASE
_ENV_WORDS: Final = (
    "production|prod|staging|stage|development|dev|test|qa|uat|sandbox|preview|"
    "pre-production|preprod"
)
_PATTERNS: Final[dict[str, tuple[re.Pattern[str], ...]]] = {
    "release": (
        re.compile(r"\brelease\b\s*[:=]?\s*(?P<v>[A-Za-z]{0,4}\d+(?:\.\d+)*[A-Za-z0-9-]*)", _FLAGS),
    ),
    "commit": (re.compile(r"\bcommit\b\s*[:=]?\s*(?P<v>[0-9a-f]{7,40})\b", _FLAGS),),
    "service": (re.compile(r"\bservice\b\s*[:=]?\s*(?P<v>[a-z0-9][a-z0-9._-]*[a-z0-9])", _FLAGS),),
    "environment": (
        re.compile(r"\benvironment\b\s*[:=]?\s*(?P<v>[A-Za-z][A-Za-z0-9_-]*)", _FLAGS),
        re.compile(r"\bapproval scope\b\s*[:=]?\s*(?P<v>[A-Za-z][A-Za-z0-9_-]*)", _FLAGS),
        re.compile(rf"\b(?:for|in|on|to)\s+(?P<v>{_ENV_WORDS})\b(?!-)", _FLAGS),
    ),
    "prompt": (
        re.compile(
            r"\bprompt\b(?:\s+(?:bundle|version))?\s*[:=]?\s*(?P<v>[A-Za-z]{0,3}\d+[A-Za-z0-9._-]*)",
            _FLAGS,
        ),
    ),
}
# Words that follow "service" or "environment" in ordinary prose.
_STOPWORDS: Final = frozenset(
    {"is", "was", "the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "owner", "level"}
)
_RUN_ID: Final = re.compile(r"\b[A-Z][A-Z0-9]{0,9}(?:-[A-Z0-9]{1,10})*-\d{1,6}\b")
_SNIPPET_MAX: Final = 320


@dataclass(frozen=True)
class TextUnit:
    """One piece of evidence text: a comment or a description."""

    comment_id: str | None
    recorded_at: str | None
    actor: str | None
    role: str | None
    text: str


@dataclass(frozen=True)
class Candidate:
    """One extracted value and how it compares with the target."""

    dimension: str
    value: str
    comparison: Comparison


@dataclass
class UnitFacts:
    """Facts extracted from one comment or description."""

    unit: TextUnit
    candidates: list[Candidate] = field(default_factory=list)
    run_ids: list[str] = field(default_factory=list)
    snippet: str = ""


@dataclass
class ScopeFacts:
    """All facts for one evidence issue."""

    units: list[UnitFacts]
    summary: dict[str, DimensionStatus]
    scanned: int


def normalise(dimension: str, value: str) -> str:
    """Return a comparison form of ``value`` for ``dimension``."""
    cleaned = value.strip().strip(".,;:").lower()
    if dimension == "environment":
        return {"prod": "production", "stage": "staging", "preprod": "pre-production"}.get(
            cleaned, cleaned
        )
    return cleaned


def compare(dimension: str, value: str, target: str | None) -> Comparison:
    """Compare an extracted value with the target value for its dimension."""
    if not target:
        return "no target"
    got, want = normalise(dimension, value), normalise(dimension, target)
    if dimension == "commit":
        # Short and full SHAs refer to the same commit when one prefixes the other.
        shorter = min(len(got), len(want))
        return "match" if shorter >= 7 and got[:shorter] == want[:shorter] else "mismatch"
    return "match" if got == want else "mismatch"


def extract(text: str) -> list[tuple[str, str, int]]:
    """Return ``(dimension, value, position)`` for every scope value in ``text``."""
    found: list[tuple[str, str, int]] = []
    for dimension, patterns in _PATTERNS.items():
        for pattern in patterns:
            for match in pattern.finditer(text):
                value = match["v"].rstrip(".,;:")
                if value.lower() in _STOPWORDS:
                    continue
                found.append((dimension, value, match.start()))
    found.sort(key=lambda item: item[2])
    unique: list[tuple[str, str, int]] = []
    seen: set[tuple[str, str]] = set()
    for dimension, value, position in found:
        key = (dimension, normalise(dimension, value))
        if key not in seen:
            seen.add(key)
            unique.append((dimension, value, position))
    return unique


def sentence_span(text: str, position: int) -> tuple[int, int]:
    """Return the start and end of the sentence or line containing ``position``."""
    dot = text.rfind(". ", 0, position)
    newline = text.rfind("\n", 0, position)
    start = max(dot + 2 if dot != -1 else 0, newline + 1 if newline != -1 else 0)
    ends = [i for i in (text.find(". ", position), text.find("\n", position)) if i != -1]
    end = min(ends) + 1 if ends else len(text)
    return start, end


def snippet_for(text: str, positions: list[int]) -> str:
    """Return the verbatim sentences holding ``positions``, joined and bounded."""
    spans: list[tuple[int, int]] = []
    for position in positions:
        span = sentence_span(text, position)
        if span not in spans:
            spans.append(span)
    parts = [text[start:end].strip() for start, end in sorted(spans)]
    joined = " ... ".join(part for part in parts if part)
    if len(joined) <= _SNIPPET_MAX:
        return joined
    return joined[: _SNIPPET_MAX - 3].rstrip() + "..."


def run_ids(text: str, exclude_prefixes: tuple[str, ...] = ()) -> list[str]:
    """Return run or record identifiers such as ``EVAL-55`` found in ``text``."""
    ids: list[str] = []
    for match in _RUN_ID.finditer(text):
        token = match.group(0)
        if token not in ids and not token.startswith(exclude_prefixes):
            ids.append(token)
    return ids


def scan_units(
    units: list[TextUnit], target: dict[str, str], exclude_prefixes: tuple[str, ...] = ()
) -> ScopeFacts:
    """Extract scope facts from every unit and summarise each dimension."""
    results: list[UnitFacts] = []
    for unit in units:
        values = extract(unit.text)
        if not values:
            continue
        facts = UnitFacts(unit=unit, run_ids=run_ids(unit.text, exclude_prefixes))
        facts.candidates = [
            Candidate(dimension, value, compare(dimension, value, target.get(dimension)))
            for dimension, value, _ in values
        ]
        facts.snippet = snippet_for(unit.text, [position for _, _, position in values])
        results.append(facts)
    return ScopeFacts(units=results, summary=summarise(results, target), scanned=len(units))


def summarise(units: list[UnitFacts], target: dict[str, str]) -> dict[str, DimensionStatus]:
    """Summarise each dimension across all units; unknown when nothing was found."""
    summary: dict[str, DimensionStatus] = {}
    for dimension in DIMENSIONS:
        comparisons = {
            c.comparison for u in units for c in u.candidates if c.dimension == dimension
        }
        if not target.get(dimension):
            summary[dimension] = "no target"
        elif not comparisons:
            summary[dimension] = "unknown"
        elif comparisons == {"match"}:
            summary[dimension] = "match"
        elif comparisons == {"mismatch"}:
            summary[dimension] = "mismatch"
        else:
            summary[dimension] = "mixed"
    return summary
