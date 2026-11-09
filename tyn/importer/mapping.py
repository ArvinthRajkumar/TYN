"""Mapping from source records to Jira fields and comment text.

Native Jira statuses, authors, timestamps and link types cannot carry the
source facts (the target board exposes a single Done column, and links in the
source are untyped), so every fact is written into text and labels instead:

* summary prefix ``[source_id][kind][recorded status]``
* a structured description header
* labels for scope, kind, release, recorded status and source id
* comments that start with the recorded time, actor and role, and end with a
  source marker used for idempotent re-runs
"""

import re
from dataclasses import dataclass
from typing import Final

from tyn.importer.dataset import SourceRecord

LABEL_PREFIX: Final = "rr-"
SOURCE_LABEL_PREFIX: Final = f"{LABEL_PREFIX}src-"
SUMMARY_MAX: Final = 255
HEADER_TITLE: Final = "Imported record header"
HEADER_SEPARATOR: Final = "---"
IMPORT_NOTE: Final = (
    "Native Jira status, author and timestamps reflect the import, not the recorded scenario."
)

# Import order puts the manifest and requirements first so a small --limit
# trial produces records whose links resolve to each other.
_KIND_PRIORITY: Final = {"manifest": 0, "requirement": 1}

_RECORDED_HEADER: Final = re.compile(
    r"^Recorded event (?P<ts>\S+) \| actor (?P<actor>[^|\n]+?) \| role (?P<role>[^|\n]+?)\n"
    r"(?P<body>.*)\Z",
    re.DOTALL,
)
_MARKER: Final = re.compile(
    r"\[source (?P<source>[A-Za-z0-9._-]+) comment (?P<index>\d+)/(?P<total>\d+)\]\s*\Z"
)
_LABEL_UNSAFE: Final = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class RecordedComment:
    """The scenario metadata and body parsed from a source comment."""

    timestamp: str
    actor: str
    role: str
    body: str


def label_slug(value: str) -> str:
    """Return ``value`` as a lowercase label fragment without spaces."""
    return _LABEL_UNSAFE.sub("-", value.strip()).strip("-").lower()


def source_label(source_id: str) -> str:
    """Return the label that marks an issue as the import of ``source_id``."""
    return f"{SOURCE_LABEL_PREFIX}{source_id}"


def source_id_from_labels(labels: list[str]) -> str | None:
    """Return the source id encoded in ``labels``, if any."""
    for label in labels:
        if label.startswith(SOURCE_LABEL_PREFIX):
            return label.removeprefix(SOURCE_LABEL_PREFIX)
    return None


def labels_for(record: SourceRecord, scope_label: str) -> list[str]:
    """Return the labels applied to the issue for ``record``."""
    return [
        scope_label,
        f"{LABEL_PREFIX}{label_slug(record.kind)}",
        f"{LABEL_PREFIX}release-{record.release}",
        f"{LABEL_PREFIX}status-{label_slug(record.recorded_status)}",
        source_label(record.source_id),
    ]


def summary_for(record: SourceRecord) -> str:
    """Return the Jira summary, truncated only if Jira's limit requires it.

    The untruncated summary is always kept in the description header.
    """
    summary = f"[{record.source_id}][{record.kind}][{record.recorded_status}] {record.summary}"
    summary = " ".join(summary.split())
    if len(summary) <= SUMMARY_MAX:
        return summary
    return summary[: SUMMARY_MAX - 3].rstrip() + "..."


def description_for(record: SourceRecord, scope_label: str) -> str:
    """Return the description text: structured header, then the original text."""
    links = ", ".join(record.links) if record.links else "none"
    header = [
        HEADER_TITLE,
        f"Source ID: {record.source_id}",
        f"Source kind: {record.kind}",
        f"Source release: {record.release}",
        f"Recorded status: {record.recorded_status}",
        f"Source summary: {record.summary}",
        f"Source links (outgoing, untyped in source): {links}",
        f"Dataset label: {scope_label}",
        IMPORT_NOTE,
        HEADER_SEPARATOR,
    ]
    return "\n".join([*header, record.description])


def parse_recorded_comment(raw: str) -> RecordedComment | None:
    """Parse the ``Recorded event ... | actor ... | role ...`` header."""
    match = _RECORDED_HEADER.match(raw)
    if match is None:
        return None
    return RecordedComment(
        timestamp=match["ts"],
        actor=match["actor"].strip(),
        role=match["role"].strip(),
        body=match["body"],
    )


def comment_marker(source_id: str, index: int, total: int) -> str:
    """Return the trailing marker that identifies a source comment."""
    return f"[source {source_id} comment {index}/{total}]"


def parse_comment_marker(text: str) -> tuple[str, int] | None:
    """Return ``(source_id, index)`` from a comment's trailing marker."""
    match = _MARKER.search(text.rstrip())
    if match is None:
        return None
    return match["source"], int(match["index"])


def format_comment(source_id: str, index: int, total: int, raw: str) -> str:
    """Return the Jira comment text for source comment ``index`` (1-based).

    The text starts with the recorded time, actor and role so a reader never
    mistakes the importing account for the scenario actor. A header that does
    not parse is kept verbatim and flagged instead of guessed.
    """
    parsed = parse_recorded_comment(raw)
    marker = comment_marker(source_id, index, total)
    if parsed is None:
        return f"[recorded header not parsed; source text follows verbatim]\n{raw}\n{marker}"
    return f"[{parsed.timestamp}] {parsed.actor} ({parsed.role}): {parsed.body}\n{marker}"


def import_order(records: list[SourceRecord]) -> list[SourceRecord]:
    """Return records with the manifest and requirements first, else file order."""
    indexed = list(enumerate(records))
    indexed.sort(key=lambda item: (_KIND_PRIORITY.get(item[1].kind, 2), item[0]))
    return [record for _, record in indexed]
