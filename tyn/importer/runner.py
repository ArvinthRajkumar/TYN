"""Two-pass importer: issues with their comments, then links.

Idempotency comes from Jira itself: every issue carries a source-id label and
every comment ends with a source marker, so a re-run looks those up and only
creates what is missing. ``state/id_map.json`` caches progress to avoid
re-reading finished issues.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

from tyn.errors import (
    AmbiguousWriteError,
    DatasetError,
    ImportAbortedError,
    JiraError,
    JiraHttpError,
    JiraRetryExhaustedError,
)
from tyn.importer.dataset import Dataset, SourceRecord
from tyn.importer.mapping import (
    description_for,
    format_comment,
    import_order,
    labels_for,
    parse_comment_marker,
    source_id_from_labels,
    source_label,
    summary_for,
)
from tyn.importer.state import ImportState
from tyn.jira import jql
from tyn.jira.adf import adf_to_text, text_to_adf
from tyn.jira.client import JiraReader, JiraWriter
from tyn.jira.models import IssueLink, LinkType

logger = logging.getLogger(__name__)

DEDICATED_LINK_TYPE: Final = LinkType(
    id="", name="Source Reference", outward="references", inward="is referenced by"
)
FALLBACK_LINK_TYPE_NAME: Final = "Relates"
# Stop instead of hammering Jira when every write is being rejected.
MAX_CONSECUTIVE_FAILURES: Final = 5


@dataclass
class StageCounts:
    """Outcome counters for one import stage."""

    created: int = 0
    skipped: int = 0
    failed: int = 0
    pending: int = 0


@dataclass
class ImportReport:
    """Everything the CLI prints after a run."""

    dry_run: bool
    selected: int
    issues: StageCounts = field(default_factory=StageCounts)
    comments: StageCounts = field(default_factory=StageCounts)
    links: StageCounts = field(default_factory=StageCounts)
    duplicates: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    link_type: str | None = None

    @property
    def has_failures(self) -> bool:
        """True when any stage recorded a failure."""
        return bool(self.issues.failed or self.comments.failed or self.links.failed)


@dataclass(frozen=True)
class ProgressEvent:
    """A progress update for the CLI."""

    stage: str
    done: int
    total: int
    counts: StageCounts


ProgressSink = Callable[[ProgressEvent], None]


class _FailureBreaker:
    """Abort after too many consecutive failures; reset on success."""

    def __init__(self, limit: int = MAX_CONSECUTIVE_FAILURES) -> None:
        self._limit = limit
        self._streak = 0

    def success(self) -> None:
        self._streak = 0

    def failure(self, stage: str) -> None:
        self._streak += 1
        if self._streak >= self._limit:
            raise ImportAbortedError(
                f"{self._limit} consecutive {stage} failures; stopping. Progress is saved; "
                "fix the cause and re-run to resume"
            )


class Importer:
    """Import a dataset into one Jira project and scope label."""

    def __init__(
        self,
        *,
        reader: JiraReader,
        writer: JiraWriter | None,
        state: ImportState,
        project: str,
        issue_type_name: str,
        progress: ProgressSink,
        sleep: Callable[[float], None] = time.sleep,
        reconcile_wait: float = 5.0,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._state = state
        self._project = project
        self._issue_type_name = issue_type_name
        self._progress = progress
        self._sleep = sleep
        self._reconcile_wait = reconcile_wait
        self._breaker = _FailureBreaker()

    @property
    def _dry_run(self) -> bool:
        return self._writer is None

    def run(self, dataset: Dataset, limit: int | None = None) -> ImportReport:
        """Run all passes and return the counters."""
        ordered = import_order(dataset.records)
        selected = ordered if limit is None else ordered[:limit]
        report = ImportReport(dry_run=self._dry_run, selected=len(selected))
        self._reader.get_project(self._project)
        issue_type_id = self._resolve_issue_type()
        self._sync_with_jira(dataset.dataset_label, report)
        fresh = self._import_issues(selected, dataset.dataset_label, issue_type_id, report)
        self._import_comments(selected, fresh, report)
        self._import_links(selected, report)
        return report

    def _save(self) -> None:
        if not self._dry_run:
            self._state.save()

    def _resolve_issue_type(self) -> str:
        types = self._reader.get_create_issue_types(self._project)
        for item in types:
            if item.name.casefold() == self._issue_type_name.casefold() and not item.subtask:
                return item.id
        names = ", ".join(sorted(t.name for t in types if not t.subtask)) or "none"
        raise ImportAbortedError(
            f"issue type {self._issue_type_name!r} is not available in {self._project}; "
            f"available: {names}. Use --issue-type."
        )

    def _sync_with_jira(self, scope_label: str, report: ImportReport) -> None:
        """Make the cached id map agree with what Jira actually holds."""
        found: dict[str, list[str]] = {}
        query = jql.scoped(self._project, scope_label)
        for issue in self._reader.search_all(query, ["labels"]):
            source_id = source_id_from_labels(list(issue.fields.get("labels") or []))
            if source_id:
                found.setdefault(source_id, []).append(issue.key)
        cache = self._state.scope
        for source_id, keys in found.items():
            keys.sort(key=_key_number)
            if len(keys) > 1:
                report.duplicates.append(f"{source_id}: {', '.join(keys)}")
                logger.warning("duplicate_import source_id=%s keys=%s", source_id, keys)
            cache.issues[source_id] = keys[0]
        for source_id, key in list(cache.issues.items()):
            if source_id not in found and not self._issue_exists(key):
                logger.warning("cached_issue_missing source_id=%s key=%s", source_id, key)
                del cache.issues[source_id]
                cache.comments_done.discard(source_id)
                cache.links_done = {p for p in cache.links_done if source_id not in p.split(">")}
        self._save()

    def _issue_exists(self, key: str) -> bool:
        # Search is eventually consistent, so a cached key missing from search
        # results is confirmed with a direct read before it is forgotten.
        try:
            self._reader.get_issue(key, ["labels"])
        except JiraHttpError as exc:
            if exc.status == 404:
                return False
            raise
        return True

    def _import_issues(
        self,
        records: list[SourceRecord],
        scope_label: str,
        issue_type_id: str,
        report: ImportReport,
    ) -> set[str]:
        fresh: set[str] = set()
        counts = report.issues
        for done, record in enumerate(records, start=1):
            if record.source_id in self._state.scope.issues:
                counts.skipped += 1
            elif self._writer is None:
                counts.created += 1
            else:
                key = self._create_issue(self._writer, record, scope_label, issue_type_id, report)
                if key is not None:
                    self._state.scope.issues[record.source_id] = key
                    fresh.add(record.source_id)
                    counts.created += 1
                    self._save()
            self._progress(ProgressEvent("issues", done, len(records), counts))
        return fresh

    def _create_issue(
        self,
        writer: JiraWriter,
        record: SourceRecord,
        scope_label: str,
        issue_type_id: str,
        report: ImportReport,
    ) -> str | None:
        try:
            fields: dict[str, Any] = {
                "project": {"key": self._project},
                "issuetype": {"id": issue_type_id},
                "summary": summary_for(record),
                "description": text_to_adf(description_for(record, scope_label)),
                "labels": labels_for(record, scope_label),
            }
            key = self._create_with_recovery(writer, record.source_id, scope_label, fields)
        except JiraRetryExhaustedError:
            raise
        except (JiraError, DatasetError) as exc:
            self._record_failure(report, report.issues, "issues", f"{record.source_id}: {exc}")
            return None
        self._breaker.success()
        logger.info("issue_created source_id=%s key=%s", record.source_id, key)
        return key

    def _create_with_recovery(
        self, writer: JiraWriter, source_id: str, scope_label: str, fields: dict[str, Any]
    ) -> str:
        try:
            return writer.create_issue(fields).key
        except AmbiguousWriteError:
            logger.warning("issue_create_ambiguous source_id=%s; checking Jira", source_id)
        self._sleep(self._reconcile_wait)
        existing = self._find_by_source(source_id, scope_label)
        if existing is not None:
            return existing
        return writer.create_issue(fields).key

    def _find_by_source(self, source_id: str, scope_label: str) -> str | None:
        query = jql.scoped(self._project, scope_label, jql.label_equals(source_label(source_id)))
        keys = sorted(
            (issue.key for issue in self._reader.search_all(query, ["labels"])), key=_key_number
        )
        return keys[0] if keys else None

    def _import_comments(
        self, records: list[SourceRecord], fresh: set[str], report: ImportReport
    ) -> None:
        cache = self._state.scope
        todo = [r for r in records if r.source_id not in cache.comments_done]
        report.comments.skipped += sum(len(r.comments) for r in records) - sum(
            len(r.comments) for r in todo
        )
        total = sum(len(r.comments) for r in todo)
        progress = _Counter()
        for record in todo:
            key = cache.issues.get(record.source_id)
            if key is None:
                self._count_unmapped_comments(record, report, progress, total)
                continue
            present = set() if record.source_id in fresh else self._markers(key, record.source_id)
            complete = self._post_comments(record, key, present, report, progress, total)
            if complete and not self._dry_run:
                cache.comments_done.add(record.source_id)
                self._save()

    def _count_unmapped_comments(
        self, record: SourceRecord, report: ImportReport, progress: "_Counter", total: int
    ) -> None:
        # Dry run: the issue would be created, so all its comments would be.
        # Real run: the issue failed to import, so its comments wait.
        if self._dry_run:
            report.comments.created += len(record.comments)
        else:
            report.comments.pending += len(record.comments)
        progress.value += len(record.comments)
        self._progress(ProgressEvent("comments", progress.value, total, report.comments))

    def _markers(self, key: str, source_id: str) -> set[int]:
        present: set[int] = set()
        for comment in self._reader.iter_comments(key):
            marker = parse_comment_marker(adf_to_text(comment.body))
            if marker is not None and marker[0] == source_id:
                present.add(marker[1])
        return present

    def _post_comments(
        self,
        record: SourceRecord,
        key: str,
        present: set[int],
        report: ImportReport,
        progress: "_Counter",
        total: int,
    ) -> bool:
        counts = report.comments
        count = len(record.comments)
        for index, raw in enumerate(record.comments, start=1):
            progress.value += 1
            if index in present:
                counts.skipped += 1
            elif self._writer is None:
                counts.created += 1
            elif not self._post_one(
                self._writer, record.source_id, key, index, count, raw, present, report
            ):
                # Later comments wait so source order is kept on the next run.
                remaining = count - index
                counts.pending += remaining
                progress.value += remaining
                self._progress(ProgressEvent("comments", progress.value, total, counts))
                return False
            self._progress(ProgressEvent("comments", progress.value, total, counts))
        return True

    def _post_one(
        self,
        writer: JiraWriter,
        source_id: str,
        key: str,
        index: int,
        count: int,
        raw: str,
        present: set[int],
        report: ImportReport,
    ) -> bool:
        label = f"{source_id} comment {index}/{count}"
        try:
            body = text_to_adf(format_comment(source_id, index, count, raw))
            try:
                writer.add_comment(key, body)
            except AmbiguousWriteError:
                logger.warning("comment_ambiguous %s; checking Jira", label)
                self._sleep(self._reconcile_wait)
                if index not in self._markers(key, source_id):
                    writer.add_comment(key, body)
        except JiraRetryExhaustedError:
            raise
        except (JiraError, DatasetError) as exc:
            self._record_failure(report, report.comments, "comments", f"{label}: {exc}")
            return False
        present.add(index)
        report.comments.created += 1
        self._breaker.success()
        return True

    def _import_links(self, records: list[SourceRecord], report: ImportReport) -> None:
        pairs = [(r.source_id, target) for r in records for target in r.links]
        if not pairs:
            return
        link_type = self._resolve_link_type()
        report.link_type = link_type
        cache = self._state.scope
        planned = set(cache.issues) | ({r.source_id for r in records} if self._dry_run else set())
        link_cache: dict[str, list[IssueLink]] = {}
        for done, (source_id, target_id) in enumerate(pairs, start=1):
            pair = f"{source_id}>{target_id}"
            if pair in cache.links_done:
                report.links.skipped += 1
            elif source_id not in cache.issues or target_id not in cache.issues:
                if self._dry_run and source_id in planned and target_id in planned:
                    report.links.created += 1
                else:
                    report.links.pending += 1
            else:
                self._ensure_link(source_id, target_id, link_type, link_cache, report)
            self._progress(ProgressEvent("links", done, len(pairs), report.links))

    def _ensure_link(
        self,
        source_id: str,
        target_id: str,
        link_type: str,
        link_cache: dict[str, list[IssueLink]],
        report: ImportReport,
    ) -> None:
        cache = self._state.scope
        source_key = cache.issues[source_id]
        target_key = cache.issues[target_id]
        try:
            if source_key not in link_cache:
                link_cache[source_key] = self._reader.get_issue_links(source_key)
            if _has_outward_link(link_cache[source_key], link_type, target_key):
                report.links.skipped += 1
            elif self._writer is None:
                report.links.created += 1
                return
            else:
                self._create_link(self._writer, source_key, target_key, link_type)
                link_cache.pop(source_key, None)
                report.links.created += 1
        except JiraRetryExhaustedError:
            raise
        except JiraError as exc:
            self._record_failure(
                report, report.links, "links", f"{source_id} -> {target_id}: {exc}"
            )
            return
        self._breaker.success()
        if not self._dry_run:
            cache.links_done.add(f"{source_id}>{target_id}")
            self._save()

    def _create_link(
        self, writer: JiraWriter, source_key: str, target_key: str, link_type: str
    ) -> None:
        """Create ``source -> target`` and confirm the direction once per scope.

        The direction Jira assigns to the request's inward and outward fields
        is checked by reading the link back from the source issue; if it comes
        out reversed, the link is replaced and later links use swapped fields.
        """
        cache = self._state.scope
        self._post_link(writer, source_key, target_key, link_type, swap=cache.link_swap_fields)
        if cache.link_direction_verified:
            return
        links = self._reader.get_issue_links(source_key)
        if _has_outward_link(links, link_type, target_key):
            cache.link_direction_verified = True
            return
        reversed_link = next(
            (
                link
                for link in links
                if link.type.name == link_type
                and link.inward_issue is not None
                and link.inward_issue.key == target_key
            ),
            None,
        )
        if reversed_link is None:
            raise ImportAbortedError(
                f"created link {source_key} -> {target_key} could not be read back"
            )
        logger.warning("link_direction_reversed; correcting and swapping request fields")
        writer.delete_link(reversed_link.id)
        cache.link_swap_fields = not cache.link_swap_fields
        self._post_link(writer, source_key, target_key, link_type, swap=cache.link_swap_fields)
        if not _has_outward_link(self._reader.get_issue_links(source_key), link_type, target_key):
            raise ImportAbortedError("link direction could not be established; stopping")
        cache.link_direction_verified = True

    def _post_link(
        self, writer: JiraWriter, source_key: str, target_key: str, link_type: str, *, swap: bool
    ) -> None:
        if swap:
            writer.create_link(link_type, outward_key=target_key, inward_key=source_key)
        else:
            writer.create_link(link_type, outward_key=source_key, inward_key=target_key)

    def _resolve_link_type(self) -> str:
        cache = self._state.scope
        available = {t.name: t for t in self._reader.get_link_types()}
        if cache.link_type and cache.link_type in available:
            return cache.link_type
        chosen = self._choose_link_type(available)
        if not self._dry_run:
            cache.link_type = chosen
            self._save()
        return chosen

    def _choose_link_type(self, available: dict[str, LinkType]) -> str:
        dedicated = DEDICATED_LINK_TYPE.name
        if dedicated in available:
            return dedicated
        if self._writer is not None:
            try:
                self._writer.create_link_type(
                    dedicated,
                    inward=DEDICATED_LINK_TYPE.inward,
                    outward=DEDICATED_LINK_TYPE.outward,
                )
            except JiraHttpError as exc:
                if exc.status not in (400, 401, 403, 404):
                    raise
                logger.warning(
                    "link_type_create_denied status=%d; falling back to %s",
                    exc.status,
                    FALLBACK_LINK_TYPE_NAME,
                )
            else:
                return dedicated
        if FALLBACK_LINK_TYPE_NAME in available:
            return FALLBACK_LINK_TYPE_NAME
        if self._dry_run:
            return f"{dedicated} (to be created)"
        raise ImportAbortedError("no usable issue link type; create one named 'Source Reference'")

    def _record_failure(
        self, report: ImportReport, counts: StageCounts, stage: str, message: str
    ) -> None:
        counts.failed += 1
        report.failures.append(message)
        logger.warning("import_failure stage=%s %s", stage, message)
        self._breaker.failure(stage)


@dataclass
class _Counter:
    value: int = 0


def _has_outward_link(links: list[IssueLink], link_type: str, target_key: str) -> bool:
    return any(
        link.type.name == link_type
        and link.outward_issue is not None
        and link.outward_issue.key == target_key
        for link in links
    )


def _key_number(key: str) -> tuple[str, int]:
    project, _, number = key.rpartition("-")
    return (project, int(number) if number.isdigit() else 0)
