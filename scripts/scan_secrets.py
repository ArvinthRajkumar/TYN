"""Secret scanning over the working tree and every blob in git history.

Run from the repository root:

    python -m scripts.scan_secrets history   # detect-secrets plugins, all history
    python -m scripts.scan_secrets values    # exact configured values, tree and history

``history`` runs the detect-secrets plugins configured in
``.secrets.baseline`` over every blob reachable from any ref and reports
findings that are not already audited in the baseline. It needs no secrets
and runs in CI.

``values`` looks for the exact configured Jira token, Jira email, Jira host
and Groq key (read through ``tyn.config``) in tracked and untracked files and
in every historical blob. It needs a local ``.env`` and prints only the name
of a leaked setting and where it was found, never the value.

Exit code 1 means something was found.
"""

import argparse
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from tyn.config import load_groq_settings, load_jira_settings
from tyn.errors import ConfigError
from tyn.redact import redactor_from_settings

BASELINE: Final = Path(".secrets.baseline")


@dataclass(frozen=True)
class Blob:
    """One historical file version."""

    sha: str
    path: str
    content: bytes


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout


def iter_history_blobs() -> Iterator[Blob]:
    """Yield every distinct blob reachable from any ref, with one path for it."""
    paths: dict[str, str] = {}
    for line in _git("rev-list", "--objects", "--all").splitlines():
        sha, _, path = line.partition(" ")
        if path:
            paths.setdefault(sha, path)
    if not paths:
        return
    with subprocess.Popen(
        ["git", "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE
    ) as proc:
        if proc.stdin is None or proc.stdout is None:
            raise RuntimeError("git cat-file pipes were not created")
        for sha, path in paths.items():
            proc.stdin.write(f"{sha}\n".encode())
            proc.stdin.flush()
            header = proc.stdout.readline().decode().split()
            size = int(header[2])
            content = proc.stdout.read(size)
            proc.stdout.read(1)
            if header[1] == "blob":
                yield Blob(sha, path, content)
        proc.stdin.close()


def working_tree_files() -> list[Path]:
    """Return tracked and untracked, non-ignored files."""
    listing = _git("ls-files", "--cached", "--others", "--exclude-standard")
    return [Path(line) for line in listing.splitlines() if line and Path(line).is_file()]


def scan_history() -> int:
    """Run detect-secrets over all history and report unaudited findings."""
    from detect_secrets.core import baseline
    from detect_secrets.core.secrets_collection import SecretsCollection

    known = baseline.load(baseline.load_from_file(str(BASELINE)), str(BASELINE))
    known_hashes = {secret.secret_hash for _, secret in known}
    findings: list[str] = []
    blobs = 0
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for blob in iter_history_blobs():
            if blob.path == BASELINE.as_posix():
                continue
            blobs += 1
            target = root / blob.sha / Path(blob.path).name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob.content)
            collection = SecretsCollection(root=str(target.parent))
            collection.scan_file(target.name)
            findings.extend(
                f"{secret.type} in {blob.path} (blob {blob.sha[:10]}, line {secret.line_number})"
                for _, secret in collection
                if secret.secret_hash not in known_hashes
            )
    for finding in findings:
        print(f"FINDING {finding}")
    print(f"history scan: {blobs} blobs, {len(findings)} unaudited findings")
    return 1 if findings else 0


def scan_values() -> int:
    """Look for exact configured secret values in the tree and in history."""
    try:
        jira = load_jira_settings()
        groq = load_groq_settings()
    except ConfigError as exc:
        print(f"values scan needs the full configuration: {exc}")
        return 1
    redactor = redactor_from_settings(jira, groq)
    leaks: list[str] = []
    files = working_tree_files()
    for path in files:
        text = path.read_bytes().decode("utf-8", errors="replace")
        leaks.extend(
            f"{name} in working tree file {path.as_posix()}" for name in redactor.leaks(text)
        )
    blobs = 0
    for blob in iter_history_blobs():
        blobs += 1
        text = blob.content.decode("utf-8", errors="replace")
        leaks.extend(
            f"{name} in history blob {blob.sha[:10]} ({blob.path})" for name in redactor.leaks(text)
        )
    for leak in sorted(set(leaks)):
        print(f"LEAK {leak}")
    print(f"values scan: {len(files)} files, {blobs} history blobs, {len(set(leaks))} leaks")
    return 1 if leaks else 0


def main(argv: list[str] | None = None) -> int:
    """Parse the mode and run the matching scan."""
    parser = argparse.ArgumentParser(prog="python -m scripts.scan_secrets")
    parser.add_argument("mode", choices=["history", "values"])
    args = parser.parse_args(argv)
    return scan_history() if args.mode == "history" else scan_values()


if __name__ == "__main__":
    raise SystemExit(main())
