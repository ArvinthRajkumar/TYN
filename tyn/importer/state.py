"""Crash-safe importer state in ``state/id_map.json``.

The file is a cache, not the source of truth: Jira labels and comment markers
are. It is rewritten atomically after every change so an interrupted run
resumes without repeating finished work.
"""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tyn.errors import StateError
from tyn.paths import atomic_write_text


class ScopeState(BaseModel):
    """Progress for one project and scope label."""

    model_config = ConfigDict(extra="forbid")

    issues: dict[str, str] = Field(default_factory=dict)
    comments_done: set[str] = Field(default_factory=set)
    links_done: set[str] = Field(default_factory=set)
    link_type: str | None = None
    link_direction_verified: bool = False
    link_swap_fields: bool = False


class StateFile(BaseModel):
    """All scopes in the state file."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    scopes: dict[str, ScopeState] = Field(default_factory=dict)


class ImportState:
    """Load, mutate and persist the state of one import scope."""

    def __init__(self, path: Path, project: str, scope_label: str) -> None:
        self._path = path
        self._key = f"{project}|{scope_label}"
        self._file = self._load()
        self.scope = self._file.scopes.setdefault(self._key, ScopeState())

    def _load(self) -> StateFile:
        if not self._path.exists():
            return StateFile()
        try:
            return StateFile.model_validate(json.loads(self._path.read_text(encoding="utf-8")))
        except (OSError, ValueError, ValidationError):
            raise StateError(
                f"{self._path.as_posix()} is unreadable; delete it to rebuild from Jira"
            ) from None

    def save(self) -> None:
        """Persist the full state atomically."""
        payload = self._file.model_dump(mode="json")
        for scope in payload["scopes"].values():
            scope["comments_done"] = sorted(scope["comments_done"])
            scope["links_done"] = sorted(scope["links_done"])
        atomic_write_text(self._path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
