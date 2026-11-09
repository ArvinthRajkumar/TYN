"""Command line entry point: ``python -m tyn <command>``.

Exit codes: 0 success, 1 finished with failures, 2 configuration or input
error, 130 interrupted (progress is saved; re-run to resume).
"""

import argparse
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from tyn.config import load_jira_settings, validate_project_key
from tyn.errors import TynError
from tyn.importer.dataset import load_dataset
from tyn.importer.runner import Importer, ImportReport, ProgressEvent, StageCounts
from tyn.importer.state import ImportState
from tyn.jira.client import JiraHttp, JiraReader, JiraWriter, build_http_client
from tyn.logging_setup import configure_logging
from tyn.paths import STATE_DIR, safe_child
from tyn.redact import Redactor, redactor_from_settings

DEFAULT_DATASET: Final = Path("inputs/dataset.json")
EXIT_OK: Final = 0
EXIT_FAILURES: Final = 1
EXIT_ERROR: Final = 2
EXIT_INTERRUPTED: Final = 130


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for all commands."""
    parser = argparse.ArgumentParser(prog="python -m tyn", description=__doc__)
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="log verbosity on stderr (default WARNING)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    importer = commands.add_parser("import", help="import the dataset into Jira")
    importer.add_argument("--project", help="Jira project key (default JIRA_PROJECT_KEY)")
    importer.add_argument("--dataset", type=Path, default=DEFAULT_DATASET, help="dataset JSON path")
    importer.add_argument(
        "--dry-run", action="store_true", help="read Jira and report what would change"
    )
    importer.add_argument(
        "--limit",
        type=_positive_int,
        help="import only the first N records (manifest and requirements come first)",
    )
    importer.add_argument("--issue-type", default="Task", help="issue type name (default Task)")
    return parser


class ProgressPrinter:
    """Print throttled progress lines so long runs show they are alive."""

    def __init__(self, interval: float = 5.0, every: int = 25) -> None:
        self._interval = interval
        self._every = every
        self._started = time.monotonic()
        self._last_print = 0.0
        self._last_stage = ""

    def __call__(self, event: ProgressEvent) -> None:
        """Print when the stage changes, finishes, or enough work passed."""
        now = time.monotonic()
        stage_changed = event.stage != self._last_stage
        finished = event.done >= event.total
        due = now - self._last_print >= self._interval or event.done % self._every == 0
        if not (stage_changed or finished or due):
            return
        self._last_stage = event.stage
        self._last_print = now
        elapsed = now - self._started
        print(
            f"[{event.stage}] {event.done}/{event.total} {_format_counts(event.counts)} "
            f"elapsed={_clock(elapsed)}",
            flush=True,
        )


def _clock(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes:02d}:{secs:02d}"


def _format_counts(counts: StageCounts) -> str:
    return (
        f"created={counts.created} skipped={counts.skipped} "
        f"failed={counts.failed} pending={counts.pending}"
    )


def print_import_report(report: ImportReport) -> None:
    """Print the final import summary."""
    created = "would create" if report.dry_run else "created"
    title = "Import summary (dry run, nothing changed)" if report.dry_run else "Import summary"
    print(title)
    print(f"  records selected: {report.selected}")
    for name, counts in (
        ("issues", report.issues),
        ("comments", report.comments),
        ("links", report.links),
    ):
        print(
            f"  {name:<9} {created} {counts.created}, skipped {counts.skipped}, "
            f"failed {counts.failed}, pending {counts.pending}"
        )
    print(f"  link type: {report.link_type or 'not needed'}")
    print(f"  duplicate issues in Jira: {', '.join(report.duplicates) or 'none'}")
    if report.links.pending:
        print("  pending links wait for records that are not imported yet")
    for failure in report.failures[:20]:
        print(f"  failure: {failure}")
    if len(report.failures) > 20:
        print(f"  ... {len(report.failures) - 20} more failures in the log")


def run_import(args: argparse.Namespace) -> int:
    """Execute the ``import`` command."""
    settings = load_jira_settings()
    redactor = redactor_from_settings(settings)
    configure_logging(args.log_level, redactor)
    try:
        project = validate_project_key(args.project or settings.jira_project_key)
    except ValueError as exc:
        raise TynError(f"--project {exc}") from None
    dataset = load_dataset(args.dataset)
    state = ImportState(safe_child(STATE_DIR, "id_map.json"), project, dataset.dataset_label)
    with build_http_client(settings) as client:
        http = JiraHttp(client)
        importer = Importer(
            reader=JiraReader(http),
            writer=None if args.dry_run else JiraWriter(http),
            state=state,
            project=project,
            issue_type_name=args.issue_type,
            progress=ProgressPrinter(),
        )
        try:
            report = importer.run(dataset, limit=args.limit)
        except TynError as exc:
            raise TynError(redactor.redact(str(exc))) from None
    print_import_report(report)
    return EXIT_FAILURES if report.has_failures else EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, run the command and map errors to exit codes."""
    args = build_parser().parse_args(argv)
    try:
        if args.command == "import":
            return run_import(args)
    except KeyboardInterrupt:
        print("interrupted; progress is saved, re-run the same command to resume", file=sys.stderr)
        return EXIT_INTERRUPTED
    except TynError as exc:
        print(f"error: {Redactor([]).redact(str(exc))}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_ERROR
