"""Importer behaviour against the in-memory fake Jira."""

import json
from pathlib import Path

import pytest

from tests.fakes import FakeJira, Fault
from tyn.errors import ImportAbortedError
from tyn.importer.dataset import Dataset
from tyn.importer.runner import Importer, ImportReport, ProgressEvent
from tyn.importer.state import ImportState

COMMENT_PATH = r"/rest/api/3/issue/TYN-\d+/comment"


def _importer(
    fake: FakeJira,
    tmp_path: Path,
    *,
    dry_run: bool = False,
    issue_type: str = "Task",
    events: list[ProgressEvent] | None = None,
) -> Importer:
    sleeps: list[float] = []
    sink = events.append if events is not None else (lambda _: None)
    return Importer(
        reader=fake.reader(sleeps),
        writer=None if dry_run else fake.writer(sleeps),
        state=ImportState(tmp_path / "state" / "id_map.json", fake.project, "tyn-test-scope"),
        project=fake.project,
        issue_type_name=issue_type,
        progress=sink,
        sleep=sleeps.append,
        reconcile_wait=0.0,
    )


def _run(fake: FakeJira, tmp_path: Path, dataset: Dataset, **kwargs: object) -> ImportReport:
    limit = kwargs.pop("limit", None)
    importer = _importer(fake, tmp_path, **kwargs)  # type: ignore[arg-type]
    return importer.run(dataset, limit=limit)  # type: ignore[arg-type]


def _key_of(fake: FakeJira, source_id: str) -> str:
    return next(
        key
        for key, issue in fake.issues.items()
        if f"rr-src-{source_id}" in issue["fields"]["labels"]
    )


def test_full_import_creates_everything_once(
    fake_dataset_run: tuple[FakeJira, ImportReport],
) -> None:
    fake, report = fake_dataset_run
    assert (report.issues.created, report.issues.failed) == (5, 0)
    assert (report.comments.created, report.comments.failed) == (6, 0)
    assert (report.links.created, report.links.pending) == (4, 0)
    assert report.link_type == "Source Reference"
    assert len(fake.issues) == 5


@pytest.fixture
def fake_dataset_run(tmp_path: Path, dataset: Dataset) -> tuple[FakeJira, ImportReport]:
    fake = FakeJira()
    return fake, _run(fake, tmp_path, dataset)


def test_comments_keep_order_actor_role_and_marker(
    fake_dataset_run: tuple[FakeJira, ImportReport],
) -> None:
    fake, _ = fake_dataset_run
    texts = fake.comment_texts(_key_of(fake, "EV-1"))
    assert [t.splitlines()[-1] for t in texts] == [
        "[source EV-1 comment 1/3]",
        "[source EV-1 comment 2/3]",
        "[source EV-1 comment 3/3]",
    ]
    assert texts[0].startswith("[2026-01-02T10:00:00+05:30] Alex Q (QA Lead): Run X-1 passed.")
    assert texts[2].startswith("[2026-01-03T11:00:00+05:30] Alex Q (QA Lead): Line one\nLine two")


def test_links_point_from_source_to_target(fake_dataset_run: tuple[FakeJira, ImportReport]) -> None:
    fake, _ = fake_dataset_run
    expected = {
        (_key_of(fake, a), _key_of(fake, b), "Source Reference")
        for a, b in [("MANIFEST", "REQ-A"), ("REQ-A", "EV-1"), ("EV-1", "EV-2"), ("WK-1", "EV-1")]
    }
    assert fake.outgoing() == expected


def test_rerun_is_idempotent_and_writes_nothing(tmp_path: Path, dataset: Dataset) -> None:
    fake = FakeJira()
    _run(fake, tmp_path, dataset)
    writes_before = len(fake.write_calls())
    report = _run(fake, tmp_path, dataset)
    assert len(fake.write_calls()) == writes_before
    assert (report.issues.created, report.issues.skipped) == (0, 5)
    assert (report.comments.created, report.comments.skipped) == (0, 6)
    assert (report.links.created, report.links.skipped) == (0, 4)


def test_rerun_without_state_file_uses_jira_as_truth(tmp_path: Path, dataset: Dataset) -> None:
    fake = FakeJira()
    _run(fake, tmp_path, dataset)
    (tmp_path / "state" / "id_map.json").unlink()
    report = _run(fake, tmp_path, dataset)
    assert report.issues.created == 0
    assert report.comments.created == 0
    assert report.links.created == 0
    assert len(fake.issues) == 5
    assert sum(len(c) for c in fake.comments.values()) == 6


def test_dry_run_reads_only(tmp_path: Path, dataset: Dataset) -> None:
    fake = FakeJira()
    report = _run(fake, tmp_path, dataset, dry_run=True)
    assert fake.write_calls() == []
    assert report.dry_run
    assert (report.issues.created, report.comments.created, report.links.created) == (5, 6, 4)
    assert not (tmp_path / "state" / "id_map.json").exists()


def test_limit_imports_first_records_and_leaves_links_pending(
    tmp_path: Path, dataset: Dataset
) -> None:
    fake = FakeJira()
    report = _run(fake, tmp_path, dataset, limit=2)
    assert report.issues.created == 2
    assert {issue["fields"]["summary"].split("]")[0] for issue in fake.issues.values()} == {
        "[MANIFEST",
        "[REQ-A",
    }
    assert (report.links.created, report.links.pending) == (1, 1)
    full = _run(fake, tmp_path, dataset)
    assert (full.issues.created, full.issues.skipped) == (3, 2)
    assert (full.links.created, full.links.skipped) == (3, 1)


def test_failed_comment_stops_that_issue_and_resumes_in_order(
    tmp_path: Path, dataset: Dataset
) -> None:
    fake = FakeJira()
    # Comment posts run REQ-A 1/1, then EV-1 1/3, then EV-1 2/3: fail the third.
    fake.faults.append(Fault("POST", COMMENT_PATH, status=400, after=2))
    report = _run(fake, tmp_path, dataset)
    assert report.comments.failed == 1
    assert report.comments.pending == 1
    assert (
        "EV-1"
        not in json.loads((tmp_path / "state" / "id_map.json").read_text(encoding="utf-8"))[
            "scopes"
        ]["TYN|tyn-test-scope"]["comments_done"]
    )
    resumed = _run(fake, tmp_path, dataset)
    assert resumed.comments.failed == 0
    assert resumed.comments.created == 2
    markers = [t.splitlines()[-1] for t in fake.comment_texts(_key_of(fake, "EV-1"))]
    assert markers == [f"[source EV-1 comment {i}/3]" for i in (1, 2, 3)]


def test_ambiguous_comment_write_is_not_duplicated(tmp_path: Path, dataset: Dataset) -> None:
    fake = FakeJira()
    fake.faults.append(Fault("POST", COMMENT_PATH, status=502, apply_first=True))
    report = _run(fake, tmp_path, dataset)
    assert report.comments.failed == 0
    assert sum(len(c) for c in fake.comments.values()) == 6


def test_ambiguous_issue_create_is_recovered_by_search(tmp_path: Path, dataset: Dataset) -> None:
    fake = FakeJira()
    fake.faults.append(Fault("POST", r"/rest/api/3/issue", status=504, apply_first=True))
    report = _run(fake, tmp_path, dataset)
    assert report.issues.failed == 0
    assert len(fake.issues) == 5


def test_rate_limited_comment_is_retried(tmp_path: Path, dataset: Dataset) -> None:
    fake = FakeJira()
    fake.faults.append(Fault("POST", COMMENT_PATH, status=429, headers={"Retry-After": "1"}))
    report = _run(fake, tmp_path, dataset)
    assert report.comments.created == 6
    assert report.comments.failed == 0


def test_link_type_falls_back_to_relates_without_admin(tmp_path: Path, dataset: Dataset) -> None:
    fake = FakeJira(allow_link_type_create=False)
    report = _run(fake, tmp_path, dataset)
    assert report.link_type == "Relates"
    assert all(link_type == "Relates" for _, _, link_type in fake.outgoing())


def test_reversed_link_semantics_are_detected_and_corrected(
    tmp_path: Path, dataset: Dataset
) -> None:
    fake = FakeJira(invert_link_semantics=True)
    report = _run(fake, tmp_path, dataset)
    assert report.links.created == 4
    expected = {
        (_key_of(fake, a), _key_of(fake, b), "Source Reference")
        for a, b in [("MANIFEST", "REQ-A"), ("REQ-A", "EV-1"), ("EV-1", "EV-2"), ("WK-1", "EV-1")]
    }
    assert fake.outgoing() == expected
    state = json.loads((tmp_path / "state" / "id_map.json").read_text(encoding="utf-8"))
    scope = state["scopes"]["TYN|tyn-test-scope"]
    assert scope["link_swap_fields"] is True
    assert scope["link_direction_verified"] is True


def test_cached_issue_deleted_in_jira_is_recreated(tmp_path: Path, dataset: Dataset) -> None:
    fake = FakeJira()
    _run(fake, tmp_path, dataset)
    gone = _key_of(fake, "EV-2")
    del fake.issues[gone]
    report = _run(fake, tmp_path, dataset)
    assert report.issues.created == 1
    assert report.comments.created == 1
    assert report.links.created == 1


def test_duplicate_issues_in_jira_are_reported(tmp_path: Path, dataset: Dataset) -> None:
    fake = FakeJira()
    _run(fake, tmp_path, dataset)
    fake.add_issue(["tyn-test-scope", "rr-src-EV-1"])
    report = _run(fake, tmp_path, dataset)
    assert len(report.duplicates) == 1
    assert report.duplicates[0].startswith("EV-1:")


def test_repeated_failures_abort_the_run(tmp_path: Path, dataset: Dataset) -> None:
    fake = FakeJira()
    fake.faults.append(Fault("POST", r"/rest/api/3/issue", status=400, times=99))
    with pytest.raises(ImportAbortedError, match="consecutive"):
        _run(fake, tmp_path, dataset)


def test_missing_issue_type_lists_available(tmp_path: Path, dataset: Dataset) -> None:
    fake = FakeJira(issue_types=("Story",))
    with pytest.raises(ImportAbortedError, match="available: Story"):
        _run(fake, tmp_path, dataset)


def test_progress_reports_every_stage(tmp_path: Path, dataset: Dataset) -> None:
    fake = FakeJira()
    events: list[ProgressEvent] = []
    _run(fake, tmp_path, dataset, events=events)
    stages = [e.stage for e in events]
    assert stages.count("issues") == 5
    assert stages.count("comments") == 6
    assert stages.count("links") == 4
    assert events[-1].done == events[-1].total


def test_state_file_belongs_to_one_scope(tmp_path: Path, dataset: Dataset) -> None:
    fake = FakeJira()
    _run(fake, tmp_path, dataset)
    other = ImportState(tmp_path / "state" / "id_map.json", "OTHER", "tyn-test-scope")
    assert other.scope.issues == {}
