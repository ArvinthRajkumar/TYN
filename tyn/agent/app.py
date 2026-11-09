"""Wiring for ``python -m tyn assess``: build real clients and run."""

import re
from dataclasses import dataclass
from typing import Final

import groq

from tyn.agent.budget import TokenEstimator
from tyn.agent.checkpoint import CheckpointStore
from tyn.agent.llm import GroqChatModel, ResilientModel
from tyn.agent.loop import AgentLoop, LoopLimits
from tyn.agent.pacer import DailyBudget, TokenPacer
from tyn.agent.runner import AssessConfig, AssessmentOutcome, AssessmentRunner
from tyn.agent.tools import JiraToolbox
from tyn.agent.trace import TraceRecorder
from tyn.config import GroqSettings, JiraSettings
from tyn.errors import TynError
from tyn.jira.client import JiraHttp, JiraReader, build_http_client
from tyn.paths import STATE_DIR, safe_child

_SCOPE_VALUE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


@dataclass(frozen=True)
class AssessOptions:
    """Validated CLI options for an assessment."""

    project: str
    label: str
    release: str
    resume: bool
    only: str | None
    input_budget: int
    max_model_calls: int
    time_limit_minutes: float


def validate_scope_value(name: str, value: str) -> str:
    """Validate a label or release value used in JQL and file names."""
    if not _SCOPE_VALUE.fullmatch(value):
        raise TynError(f"--{name} must be letters, digits, dot, dash or underscore")
    return value


def run_assessment(
    jira: JiraSettings, groq_settings: GroqSettings, options: AssessOptions
) -> AssessmentOutcome:
    """Build every dependency and run the assessment."""
    estimator = TokenEstimator()
    pacer = TokenPacer(groq_settings.groq_tpm_limit)
    daily = DailyBudget(
        safe_child(STATE_DIR, "usage.json"), groq_settings.groq_model, groq_settings.groq_tpd_limit
    )
    trace = TraceRecorder()
    store = CheckpointStore(options.project, options.label, options.release)
    investigation = LoopLimits(
        max_model_calls=options.max_model_calls, input_budget=options.input_budget
    )
    discovery = LoopLimits(
        max_model_calls=6, max_tool_calls=6, input_budget=max(options.input_budget, 4_000)
    )
    config = AssessConfig(
        release=options.release,
        resume=options.resume,
        only=options.only,
        discovery_limits=discovery,
        investigation_limits=investigation,
        overall_seconds=options.time_limit_minutes * 60,
    )
    client = groq.Groq(
        api_key=groq_settings.groq_api_key.get_secret_value(), max_retries=0, timeout=60.0
    )
    try:
        with build_http_client(jira) as http_client:
            toolbox = JiraToolbox(JiraReader(JiraHttp(http_client)), options.project, options.label)
            model = ResilientModel(
                GroqChatModel(client, groq_settings.groq_model),
                pacer=pacer,
                daily=daily,
                estimator=estimator,
            )

            def loop_factory(limits: LoopLimits) -> AgentLoop:
                return AgentLoop(
                    model=model, toolbox=toolbox, estimator=estimator, trace=trace, limits=limits
                )

            runner = AssessmentRunner(
                toolbox=toolbox,
                model=model,
                pacer=pacer,
                loop_factory=loop_factory,
                trace=trace,
                store=store,
                config=config,
            )
            return runner.run()
    finally:
        client.close()
