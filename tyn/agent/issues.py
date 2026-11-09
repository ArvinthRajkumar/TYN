"""Parsing imported Jira issues into compact, citable views.

The importer stores source facts in a summary prefix, a description header,
labels and a fixed comment layout; this module reads them back. Anything
that does not follow the layout is shown as-is rather than guessed.
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from tyn.importer.mapping import (
    HEADER_SEPARATOR,
    HEADER_TITLE,
    LABEL_PREFIX,
    SOURCE_LABEL_PREFIX,
)
from tyn.jira.adf import adf_to_text
from tyn.jira.models import IssueRef, JiraComment

_SUMMARY_PREFIX: Final = re.compile(
    r"^\[(?P<source>[^\]]+)\]\[(?P<kind>[^\]]+)\]\[(?P<status>[^\]]+)\]\s*(?P<title>.*)$", re.DOTALL
)
_COMMENT: Final = re.compile(
    r"^\[(?P<ts>[^\]\n]+)\] (?P<actor>[^()\n]+?) \((?P<role>[^()\n]+)\): (?P<body>.*)$", re.DOTALL
)
_MARKER_LINE: Final = re.compile(r"\n?\[source [^\]\n]+ comment \d+/\d+\]\s*\Z")


@dataclass(frozen=True)
class CommentView:
    """One comment with its recorded scenario metadata."""

    id: str
    recorded_at: str | None
    actor: str | None
    role: str | None
    body: str
    jira_order: int

    def sort_key(self) -> tuple[int, datetime, int]:
        """Order by recorded time; unparseable times go last in Jira order."""
        parsed = parse_time(self.recorded_at)
        if parsed is None:
            return (1, datetime.max.replace(tzinfo=UTC), self.jira_order)
        return (0, parsed, self.jira_order)

    def normalised_body(self) -> str:
        """Body with whitespace collapsed, used for exact duplicate detection."""
        return " ".join(self.body.split())


@dataclass
class IssueView:
    """An imported issue reduced to the facts the agent needs."""

    key: str
    labels: list[str]
    source_id: str | None
    kind: str | None
    release: str | None
    recorded_status: str | None
    title: str
    description: str
    header: dict[str, str] = field(default_factory=dict)
    comments: list[CommentView] | None = None

    def tag(self) -> str:
        """Short provenance tag such as ``[REC-1 evidence R1 Done]``."""
        parts = [self.source_id or "no-source-id", self.kind or "?", self.release or "?"]
        parts.append(self.recorded_status or "?")
        return "[" + " ".join(parts) + "]"


def parse_time(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp; naive times are treated as UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def split_description(text: str) -> tuple[dict[str, str], str]:
    """Split the importer header from the original description."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != HEADER_TITLE or HEADER_SEPARATOR not in lines:
        return {}, text
    cut = lines.index(HEADER_SEPARATOR)
    header: dict[str, str] = {}
    for line in lines[1:cut]:
        name, sep, value = line.partition(": ")
        if sep:
            header[name.strip()] = value.strip()
    return header, "\n".join(lines[cut + 1 :])


def _label_value(labels: list[str], prefix: str) -> str | None:
    return next((label.removeprefix(prefix) for label in labels if label.startswith(prefix)), None)


def _kind_from_labels(labels: list[str]) -> str | None:
    reserved = (SOURCE_LABEL_PREFIX, f"{LABEL_PREFIX}release-", f"{LABEL_PREFIX}status-")
    for label in labels:
        if label.startswith(LABEL_PREFIX) and not label.startswith(reserved):
            return label.removeprefix(LABEL_PREFIX)
    return None


def issue_view(issue: IssueRef) -> IssueView:
    """Build an ``IssueView`` from a search or get-issue result."""
    fields: dict[str, Any] = issue.fields
    labels = [str(label) for label in fields.get("labels") or []]
    summary = str(fields.get("summary") or "")
    description = adf_to_text(fields.get("description")) if fields.get("description") else ""
    header, original = split_description(description)
    prefix = _SUMMARY_PREFIX.match(summary)
    return IssueView(
        key=issue.key,
        labels=labels,
        source_id=header.get("Source ID") or _label_value(labels, SOURCE_LABEL_PREFIX),
        kind=header.get("Source kind") or _kind_from_labels(labels),
        release=header.get("Source release") or _label_value(labels, f"{LABEL_PREFIX}release-"),
        recorded_status=header.get("Recorded status") or (prefix["status"] if prefix else None),
        title=header.get("Source summary") or (prefix["title"] if prefix else summary),
        description=original,
        header=header,
    )


def comment_view(comment: JiraComment, order: int) -> CommentView:
    """Parse one imported comment into recorded time, actor, role and body."""
    text = _MARKER_LINE.sub("", adf_to_text(comment.body))
    match = _COMMENT.match(text)
    if match is None:
        return CommentView(comment.id, None, None, None, text, order)
    return CommentView(
        id=comment.id,
        recorded_at=match["ts"],
        actor=match["actor"].strip(),
        role=match["role"].strip(),
        body=match["body"],
        jira_order=order,
    )
