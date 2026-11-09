"""Dataset validation and the mapping of records to Jira text and labels."""

import json
from pathlib import Path

import pytest

from tests.conftest import sample_payload
from tyn.errors import DatasetError
from tyn.importer.dataset import Dataset, SourceRecord, load_dataset
from tyn.importer.mapping import (
    comment_marker,
    description_for,
    format_comment,
    import_order,
    labels_for,
    parse_comment_marker,
    parse_recorded_comment,
    source_id_from_labels,
    summary_for,
)


def _write(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_valid_dataset(tmp_path: Path) -> None:
    dataset = load_dataset(_write(tmp_path, sample_payload()))
    assert dataset.dataset_label == "tyn-test-scope"
    assert len(dataset.records) == 5


def test_unknown_record_key_is_rejected(tmp_path: Path) -> None:
    payload = sample_payload()
    payload["records"][0]["severity"] = "high"
    with pytest.raises(DatasetError, match="severity"):
        load_dataset(_write(tmp_path, payload))


def test_dangling_link_is_rejected(tmp_path: Path) -> None:
    payload = sample_payload()
    payload["records"][0]["links"] = ["NOPE-1"]
    with pytest.raises(DatasetError, match="NOPE-1"):
        load_dataset(_write(tmp_path, payload))


def test_duplicate_source_id_is_rejected(tmp_path: Path) -> None:
    payload = sample_payload()
    payload["records"][1]["source_id"] = "EV-1"
    with pytest.raises(DatasetError, match="duplicate"):
        load_dataset(_write(tmp_path, payload))


def test_label_unsafe_identifier_is_rejected(tmp_path: Path) -> None:
    payload = sample_payload()
    payload["dataset_label"] = "has space"
    with pytest.raises(DatasetError, match="dataset_label"):
        load_dataset(_write(tmp_path, payload))


def test_missing_and_malformed_files(tmp_path: Path) -> None:
    with pytest.raises(DatasetError, match="not found"):
        load_dataset(tmp_path / "absent.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(DatasetError, match="not valid JSON"):
        load_dataset(bad)


def test_comment_starts_with_recorded_time_actor_and_role() -> None:
    raw = "Recorded event 2026-01-02T10:00:00+05:30 | actor Alex Q | role QA Lead\nRun passed."
    text = format_comment("EV-1", 2, 3, raw)
    assert text.startswith("[2026-01-02T10:00:00+05:30] Alex Q (QA Lead): Run passed.")
    assert text.endswith(comment_marker("EV-1", 2, 3))
    assert parse_comment_marker(text) == ("EV-1", 2)


def test_multiline_comment_body_is_kept() -> None:
    raw = "Recorded event T | actor A B | role Security Owner\nfirst\nsecond"
    parsed = parse_recorded_comment(raw)
    assert parsed is not None
    assert parsed.body == "first\nsecond"
    assert parsed.role == "Security Owner"


def test_unparsed_header_is_flagged_and_kept_verbatim() -> None:
    text = format_comment("WK-1", 1, 1, "no recorded header here")
    assert text.startswith("[recorded header not parsed")
    assert "no recorded header here" in text
    assert parse_comment_marker(text) == ("WK-1", 1)


def test_marker_in_body_middle_is_not_taken_as_marker() -> None:
    assert parse_comment_marker("[source X comment 1/2] then more text") is None


def test_labels_carry_scope_kind_release_status_and_source(dataset: Dataset) -> None:
    record = dataset.records[0]
    labels = labels_for(record, dataset.dataset_label)
    assert labels == [
        "tyn-test-scope",
        "rr-evidence",
        "rr-release-R9.1",
        "rr-status-in-progress",
        "rr-src-EV-1",
    ]
    assert all(" " not in label for label in labels)
    assert source_id_from_labels(labels) == "EV-1"


def test_summary_prefix_and_truncation() -> None:
    record = SourceRecord(
        source_id="EV-9",
        kind="evidence",
        summary="x" * 400,
        release="R1",
        recorded_status="Done",
        description="",
        comments=[],
        links=[],
    )
    summary = summary_for(record)
    assert summary.startswith("[EV-9][evidence][Done] ")
    assert len(summary) == 255
    assert summary.endswith("...")


def test_description_header_keeps_original_text_and_links(dataset: Dataset) -> None:
    record = dataset.records[0]
    text = description_for(record, dataset.dataset_label)
    assert "Source ID: EV-1" in text
    assert "Recorded status: In Progress" in text
    assert "Source links (outgoing, untyped in source): EV-2" in text
    assert "Native Jira status, author and timestamps reflect the import" in text
    assert text.endswith(record.description)


def test_import_order_puts_manifest_and_requirements_first(dataset: Dataset) -> None:
    order = [record.source_id for record in import_order(dataset.records)]
    assert order == ["MANIFEST", "REQ-A", "EV-1", "EV-2", "WK-1"]
