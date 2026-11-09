"""Structured logging with a redaction filter on every handler."""

import logging
import sys
from typing import TextIO

from tyn.redact import Redactor

_FORMAT = "%(asctime)s level=%(levelname)s logger=%(name)s %(message)s"

# These libraries log full request URLs at INFO, which would include the Jira
# host. They are kept at WARNING; the filter still redacts anything they emit.
_NOISY_LOGGERS = ("httpx", "httpcore", "groq")


class RedactingFilter(logging.Filter):
    """Mask secrets in the final message and in any exception text."""

    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        """Rewrite the record in place and always let it through."""
        record.msg = self._redactor.redact(record.getMessage())
        record.args = None
        if record.exc_info:
            formatter = logging.Formatter()
            record.exc_text = self._redactor.redact(formatter.formatException(record.exc_info))
            record.exc_info = None
        return True


def configure_logging(level: str, redactor: Redactor, stream: TextIO | None = None) -> None:
    """Install a single redacting stderr handler on the root logger."""
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler.addFilter(RedactingFilter(redactor))
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
