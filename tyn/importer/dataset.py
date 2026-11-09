"""Validated model of the import dataset.

Unknown keys are rejected rather than ignored: silently dropping a field
would lose recorded evidence, which the brief requires us to preserve.
"""

import json
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from tyn.errors import DatasetError

# Identifiers end up inside Jira labels, which cannot contain spaces.
_LABEL_SAFE = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"


class SourceRecord(BaseModel):
    """One source ticket exactly as supplied."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(pattern=_LABEL_SAFE)
    kind: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    summary: str = Field(min_length=1)
    release: str = Field(pattern=_LABEL_SAFE)
    recorded_status: str = Field(min_length=1, max_length=64)
    description: str
    comments: list[str]
    links: list[str]


class Dataset(BaseModel):
    """The whole import file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_label: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
    target: dict[str, str]
    records: list[SourceRecord] = Field(min_length=1)
    note: str | None = None

    @model_validator(mode="after")
    def _check_references(self) -> Self:
        ids = [record.source_id for record in self.records]
        duplicates = sorted({sid for sid in ids if ids.count(sid) > 1})
        if duplicates:
            raise ValueError(f"duplicate source_id values: {', '.join(duplicates[:5])}")
        known = set(ids)
        dangling = sorted(
            {link for record in self.records for link in record.links if link not in known}
        )
        if dangling:
            raise ValueError(f"links to unknown source_id values: {', '.join(dangling[:5])}")
        return self


def load_dataset(path: Path) -> Dataset:
    """Read and validate the dataset file."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise DatasetError(
            f"dataset not found at {path.as_posix()}; place the supplied file there"
        ) from None
    except OSError as exc:
        raise DatasetError(f"cannot read dataset {path.as_posix()}: {exc.strerror}") from None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DatasetError(f"dataset is not valid JSON (line {exc.lineno})") from None
    try:
        return Dataset.model_validate(payload)
    except ValidationError as exc:
        problems = [
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)[:5]
        ]
        raise DatasetError(
            "dataset does not match the expected shape: " + "; ".join(problems)
        ) from None
