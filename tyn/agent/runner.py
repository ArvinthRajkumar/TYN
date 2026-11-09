"""Assessment orchestration.

Discovery, then one investigation per requirement, then deterministic
aggregation, with a checkpoint saved after every step.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from tyn.agent.checkpoint import Checkpoint, CheckpointStore, SessionStats, now_iso
from tyn.agent.decision import OverallDecision, decide
from tyn.agent.discovery import DiscoveryStage
from tyn.agent.investigate import Investigator
from tyn.agent.llm import RateLimitWaitTooLongError, ResilientModel
from tyn.agent.loop import AgentLoop, LoopLimits
from tyn.agent.models import Discovery, RequirementResult, RequirementTarget
from tyn.agent.pacer import DailyBudgetExhaustedError, TokenPacer
from tyn.agent.tools import JiraToolbox
from tyn.agent.trace import TraceRecorder
from tyn.errors import TynError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AssessConfig:
    """User-facing options for one assessment run."""

    release: str
    resume: bool = False
    only: str | None = None
    discovery_limits: LoopLimits = field(
        default_factory=lambda: LoopLimits(max_model_calls=6, max_tool_calls=6)
    )
    investigation_limits: LoopLimits = field(default_factory=LoopLimits)
    overall_seconds: float = 5_400.0


@dataclass
class AssessmentOutcome:
    """The checkpoint plus what the report needs."""

    checkpoint: Checkpoint
    overall: OverallDecision
    assessed: list[RequirementResult]
    unassessed: list[RequirementTarget]
    stopped_reason: str | None


class AssessmentRunner:
    """Run or resume one assessment."""

    def __init__(
        self,
        *,
        toolbox: JiraToolbox,
        model: ResilientModel,
        pacer: TokenPacer,
        loop_factory: Callable[[LoopLimits], AgentLoop],
        trace: TraceRecorder,
        store: CheckpointStore,
        config: AssessConfig,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._toolbox = toolbox
        self._model = model
        self._pacer = pacer
        self._loop_factory = loop_factory
        self._trace = trace
        self._store = store
        self._config = config
        self._monotonic = monotonic

    def run(self) -> AssessmentOutcome:
        """Execute the assessment and always leave a saved checkpoint."""
        checkpoint = self._open()
        session = SessionStats(started_at=now_iso())
        checkpoint.sessions.append(session)
        started = self._monotonic()
        try:
            self._discover(checkpoint)
            self._investigate_all(checkpoint, session, started)
        except (DailyBudgetExhaustedError, RateLimitWaitTooLongError) as exc:
            session.stopped_reason = str(exc)
            logger.warning("assessment_stopped reason=%s", exc)
        finally:
            self._close_session(session, started)
            self._store.save(checkpoint)
        return self._outcome(checkpoint, session.stopped_reason)

    def _open(self) -> Checkpoint:
        existing = self._store.load() if (self._config.resume or self._config.only) else None
        if existing is None:
            return self._store.new(self._model.model_name)
        if self._model.model_name not in existing.models:
            existing.models.append(self._model.model_name)
        return existing

    def _discover(self, checkpoint: Checkpoint) -> None:
        if checkpoint.discovery is not None:
            return
        stage = DiscoveryStage(
            self._loop_factory(self._config.discovery_limits),
            self._toolbox,
            release=self._config.release,
        )
        checkpoint.discovery = stage.run()
        checkpoint.traces["discovery"] = self._trace.slice("discovery")
        self._store.save(checkpoint)

    def _targets(self, checkpoint: Checkpoint) -> list[RequirementTarget]:
        targets = _discovery(checkpoint).requirements
        only = self._config.only
        if only is None:
            return targets
        wanted = only.strip().upper()
        chosen = [
            t for t in targets if wanted in (t.issue_key.upper(), (t.source_id or "").upper())
        ]
        if not chosen:
            available = ", ".join(f"{t.issue_key} ({t.source_id})" for t in targets)
            raise TynError(f"--only {only} is not a discovered requirement; available: {available}")
        return chosen

    def _investigate_all(
        self, checkpoint: Checkpoint, session: SessionStats, started: float
    ) -> None:
        discovery = _discovery(checkpoint)
        investigator = Investigator(
            self._loop_factory(self._config.investigation_limits), release=self._config.release
        )
        for target in self._targets(checkpoint):
            previous = checkpoint.results.get(target.issue_key)
            rerun = self._config.only is not None
            if previous is not None and previous.status == "completed" and not rerun:
                logger.info("requirement_skipped key=%s reason=completed", target.issue_key)
                continue
            if self._monotonic() - started > self._config.overall_seconds:
                session.stopped_reason = "overall time limit reached"
                return
            requirement = self._toolbox.view(target.issue_key)
            result = investigator.investigate(
                target,
                requirement_text=requirement.description,
                manifest_key=discovery.manifest_key,
                manifest_rules=discovery.manifest_rules,
            )
            checkpoint.results[target.issue_key] = result
            checkpoint.traces[target.issue_key] = self._trace.slice(target.issue_key)
            self._store.save(checkpoint)
            logger.info(
                "requirement_done key=%s decision=%s status=%s",
                target.issue_key,
                result.verdict.decision.value,
                result.status,
            )

    def _close_session(self, session: SessionStats, started: float) -> None:
        stats = self._model.stats
        session.seconds = round(self._monotonic() - started, 2)
        session.model_calls = stats.calls
        session.model_retries = stats.retries
        session.rate_limit_waits = stats.rate_limit_waits
        session.rate_limit_wait_seconds = round(stats.rate_limit_wait_seconds, 2)
        session.pacer_sleeps = self._pacer.stats.sleeps
        session.pacer_sleep_seconds = round(self._pacer.stats.slept_seconds, 2)
        session.jira_calls = self._toolbox.jira_calls
        session.usage = stats.usage

    def _outcome(self, checkpoint: Checkpoint, stopped: str | None) -> AssessmentOutcome:
        return outcome_from_checkpoint(checkpoint, stopped)


def outcome_from_checkpoint(
    checkpoint: Checkpoint, stopped: str | None = None
) -> AssessmentOutcome:
    """Compute the outcome from saved results; the decision is always made in code."""
    if stopped is None and checkpoint.sessions:
        stopped = checkpoint.sessions[-1].stopped_reason
    targets = checkpoint.discovery.requirements if checkpoint.discovery else []
    assessed = [
        checkpoint.results[t.issue_key] for t in targets if t.issue_key in checkpoint.results
    ]
    unassessed = [t for t in targets if t.issue_key not in checkpoint.results]
    overall = decide(assessed, unassessed_mandatory=sum(1 for t in unassessed if t.mandatory))
    if checkpoint.discovery is None:
        overall = OverallDecision(overall.decision, ("discovery did not complete",), True)
    return AssessmentOutcome(checkpoint, overall, assessed, unassessed, stopped)


def _discovery(checkpoint: Checkpoint) -> Discovery:
    if checkpoint.discovery is None:
        raise TynError("discovery has not run for this checkpoint")
    return checkpoint.discovery
