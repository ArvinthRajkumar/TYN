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

from tyn.agent.app import (
    AssessOptions,
    load_provider_config,
    run_assessment,
    validate_scope_value,
)
from tyn.agent.checkpoint import CheckpointStore, now_iso
from tyn.agent.runner import AssessmentOutcome, outcome_from_checkpoint
from tyn.config import (
    JiraSettings,
    load_jira_settings,
    optional_gemini_settings,
    optional_groq_settings,
    validate_project_key,
)
from tyn.errors import TynError
from tyn.importer.dataset import load_dataset
from tyn.importer.runner import Importer, ImportReport, ProgressEvent, StageCounts
from tyn.importer.state import ImportState
from tyn.jira.client import JiraHttp, JiraReader, JiraWriter, build_http_client
from tyn.logging_setup import configure_logging
from tyn.paths import STATE_DIR, safe_child
from tyn.redact import Redactor, redactor_from_settings
from tyn.report.build import write_artifacts

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
    # Shared options live on each subcommand so they also work after the
    # command name.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="log verbosity on stderr (default WARNING)",
    )
    parser = argparse.ArgumentParser(prog="python -m tyn", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    importer = commands.add_parser("import", parents=[common], help="import the dataset into Jira")
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
    assess = commands.add_parser(
        "assess", parents=[common], help="assess release readiness from Jira evidence"
    )
    assess.add_argument("--project", help="Jira project key (default JIRA_PROJECT_KEY)")
    assess.add_argument("--label", required=True, help="dataset scope label on imported issues")
    assess.add_argument("--release", required=True, help="target release, e.g. R2.4")
    assess.add_argument(
        "--resume", action="store_true", help="continue a saved run, skipping finished requirements"
    )
    assess.add_argument(
        "--only", help="investigate one requirement (Jira key or source id) for a trial run"
    )
    assess.add_argument(
        "--input-budget",
        type=_positive_int,
        default=5_000,
        help="estimated input tokens per model request (default 5000)",
    )
    assess.add_argument(
        "--max-model-calls",
        type=_positive_int,
        default=10,
        help="model calls per requirement before a verdict is forced (default 10)",
    )
    assess.add_argument(
        "--time-limit-min",
        type=_positive_int,
        default=90,
        help="overall wall-clock limit in minutes (default 90)",
    )
    report = commands.add_parser(
        "report", parents=[common], help="re-render reports from a saved assessment checkpoint"
    )
    report.add_argument("--project", help="Jira project key (default JIRA_PROJECT_KEY)")
    report.add_argument("--label", required=True, help="dataset scope label")
    report.add_argument("--release", required=True, help="target release")
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


def full_redactor(jira: JiraSettings) -> Redactor:
    """Redact the Jira secrets and every configured provider key.

    Keys of the provider not in use are included too when present, so a
    stray value can never reach logs or artifacts.
    """
    return redactor_from_settings(
        jira, groq=optional_groq_settings(), gemini=optional_gemini_settings()
    )


def run_assess(args: argparse.Namespace) -> int:
    """Execute the ``assess`` command."""
    jira = load_jira_settings()
    provider = load_provider_config()
    redactor = full_redactor(jira)
    configure_logging(args.log_level, redactor)
    try:
        project = validate_project_key(args.project or jira.jira_project_key)
    except ValueError as exc:
        raise TynError(f"--project {exc}") from None
    options = AssessOptions(
        project=project,
        label=validate_scope_value("label", args.label),
        release=validate_scope_value("release", args.release),
        resume=args.resume,
        only=args.only,
        input_budget=args.input_budget,
        max_model_calls=args.max_model_calls,
        time_limit_minutes=float(args.time_limit_min),
    )
    try:
        outcome = run_assessment(jira, provider, options)
    except TynError as exc:
        raise TynError(redactor.redact(str(exc))) from None
    print_assessment(outcome)
    print_written(write_artifacts(outcome, redactor, generated_at=now_iso()))
    complete = not outcome.overall.partial and outcome.stopped_reason is None
    return EXIT_OK if complete else EXIT_FAILURES


def run_report(args: argparse.Namespace) -> int:
    """Execute the ``report`` command: no Jira or model calls are made."""
    jira = load_jira_settings()
    redactor = full_redactor(jira)
    configure_logging(args.log_level, redactor)
    try:
        project = validate_project_key(args.project or jira.jira_project_key)
    except ValueError as exc:
        raise TynError(f"--project {exc}") from None
    store = CheckpointStore(
        project,
        validate_scope_value("label", args.label),
        validate_scope_value("release", args.release),
    )
    checkpoint = store.load(None)
    if checkpoint is None:
        raise TynError(f"no saved assessment at {store.path.as_posix()}; run assess first")
    outcome = outcome_from_checkpoint(checkpoint)
    print_assessment(outcome)
    print_written(write_artifacts(outcome, redactor, generated_at=now_iso()))
    complete = not outcome.overall.partial and outcome.stopped_reason is None
    return EXIT_OK if complete else EXIT_FAILURES


def print_written(paths: list[Path]) -> None:
    """List the artifacts that were written."""
    print("Artifacts (redacted and verified):")
    for path in paths:
        print(f"  {path.relative_to(Path.cwd().resolve()).as_posix()}")


def print_assessment(outcome: AssessmentOutcome) -> None:
    """Print a short console summary of an assessment."""
    saved = outcome.checkpoint
    print(f"Provider: {saved.provider}, model {saved.model}, prompt hash {saved.prompt_hash}")
    print(f"Overall decision: {outcome.overall.decision.value}")
    for reason in outcome.overall.reasons:
        print(f"  {reason}")
    for result in outcome.assessed:
        target = result.target
        flag = "mandatory" if target.mandatory else "optional"
        status = "" if result.status == "completed" else f" ({result.status}: {result.failure})"
        print(
            f"  {target.issue_key:<10} {target.source_id or '':<10} {flag:<9} "
            f"{result.verdict.decision.value}{status}"
        )
    for target in outcome.unassessed:
        print(f"  {target.issue_key:<10} {target.source_id or '':<10} not assessed")
    if outcome.stopped_reason:
        print(f"Stopped early: {outcome.stopped_reason}")
    if outcome.overall.partial or outcome.stopped_reason:
        print("This is a partial assessment. Re-run with --resume to complete it.")


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, run the command and map errors to exit codes."""
    args = build_parser().parse_args(argv)
    try:
        if args.command == "import":
            return run_import(args)
        if args.command == "assess":
            return run_assess(args)
        if args.command == "report":
            return run_report(args)
    except KeyboardInterrupt:
        print("interrupted; progress is saved, re-run the same command to resume", file=sys.stderr)
        return EXIT_INTERRUPTED
    except TynError as exc:
        print(f"error: {Redactor([]).redact(str(exc))}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_ERROR
