"""Exception hierarchy.

Every error raised on purpose by this package derives from ``TynError`` so the
CLI can report it cleanly. Messages never include secret values.
"""


class TynError(Exception):
    """Base class for expected, user-reportable failures."""


class ConfigError(TynError):
    """Configuration is missing or invalid."""


class UnsafePathError(TynError):
    """A computed output path escapes the allowed output directories."""


class DatasetError(TynError):
    """The import dataset is missing or does not match the expected shape."""


class StateError(TynError):
    """A local state file is unreadable or belongs to another scope."""


class ImportAbortedError(TynError):
    """The importer stopped early; finished work is saved and a re-run resumes."""


class JiraError(TynError):
    """Base class for Jira communication failures."""


class JiraHttpError(JiraError):
    """Jira answered with a non-retryable error status."""

    def __init__(self, method: str, path: str, status: int, detail: str) -> None:
        self.method = method
        self.path = path
        self.status = status
        self.detail = detail
        super().__init__(f"Jira {method} {path} failed with HTTP {status}: {detail}")


class JiraRetryExhaustedError(JiraError):
    """Retries were used up on a retryable failure."""


class AmbiguousWriteError(JiraError):
    """A write may or may not have been applied (timeout or server error).

    Writes are not blindly retried, because a repeated POST can duplicate a
    comment or an issue. Callers reconcile against Jira before retrying.
    """
