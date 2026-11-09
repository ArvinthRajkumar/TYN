"""Safe file output.

All files the tool writes live under ``reports/`` or ``state/``. File names
are chosen by code, never by model output, and every path is re-checked after
resolution so a crafted name cannot escape its directory.
"""

import os
import tempfile
from pathlib import Path
from typing import Final

from tyn.errors import UnsafePathError

REPORTS_DIR: Final = Path("reports")
STATE_DIR: Final = Path("state")


def safe_child(root: Path, relative: str) -> Path:
    """Return ``root/relative`` resolved, refusing anything outside ``root``."""
    base = root.resolve()
    candidate = (base / relative).resolve()
    if candidate == base or not candidate.is_relative_to(base):
        raise UnsafePathError(f"refusing to write outside {root.as_posix()}/")
    return candidate


def atomic_write_text(path: Path, text: str) -> None:
    """Write UTF-8 text so readers never observe a half-written file.

    The temporary file sits in the target directory so ``os.replace`` stays on
    one filesystem and is atomic on both Windows and POSIX.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
