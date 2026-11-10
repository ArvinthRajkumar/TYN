"""Wiring for ``python -m tyn assess``: build real clients and run."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import groq
from google import genai
from google.genai import types as genai_types

from tyn.agent.budget import TokenEstimator
from tyn.agent.checkpoint import CheckpointStore, Provenance
from tyn.agent.fingerprint import prompt_fingerprint
from tyn.agent.llm import ChatModel, ResilientModel
from tyn.agent.loop import AgentLoop, LoopLimits
from tyn.agent.pacer import DailyBudget, TokenPacer
from tyn.agent.providers.gemini import GeminiChatModel
from tyn.agent.providers.groq import GroqChatModel
from tyn.agent.runner import AssessConfig, AssessmentOutcome, AssessmentRunner
from tyn.agent.tools import JiraToolbox
from tyn.agent.trace import TraceRecorder
from tyn.config import (
    GeminiSettings,
    GroqSettings,
    JiraSettings,
    load_gemini_settings,
    load_groq_settings,
    load_model_settings,
)
from tyn.errors import TynError
from tyn.jira.client import JiraHttp, JiraReader, build_http_client
from tyn.paths import STATE_DIR, safe_child

_SCOPE_VALUE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_REQUEST_TIMEOUT_SECONDS: Final = 60.0


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


@dataclass(frozen=True)
class ProviderConfig:
    """What the run needs to know about the chosen model provider.

    Attributes:
        name: ``gemini`` or ``groq``.
        model: Provider model id.
        tokens_per_minute: Per-minute token limit used by the pacer.
        requests_per_minute: Per-minute request limit, if the provider has one.
        tokens_per_day: Daily token cap; None means usage is recorded only.
        reserve_output: Whether output tokens count toward the minute limit.
        turn_tokens: Completion cap for a tool-calling turn.
        submit_tokens: Completion cap for a forced submit.
        max_run_tokens: Hard cap on tokens for one invocation.
    """

    name: str
    model: str
    tokens_per_minute: int
    requests_per_minute: int | None
    tokens_per_day: int | None
    reserve_output: bool
    turn_tokens: int
    submit_tokens: int
    max_run_tokens: int
    gemini: GeminiSettings | None = None
    groq: GroqSettings | None = None


def load_provider_config() -> ProviderConfig:
    """Read LLM_PROVIDER and the matching provider settings."""
    settings = load_model_settings()
    if settings.llm_provider == "gemini":
        gemini = load_gemini_settings()
        # Thinking tokens count toward Gemini's output limit, so the caps are
        # larger than Groq's; Gemini's per-minute token limit counts input only.
        return ProviderConfig(
            name="gemini",
            model=gemini.gemini_model,
            tokens_per_minute=gemini.gemini_tpm_limit,
            requests_per_minute=gemini.gemini_rpm_limit,
            tokens_per_day=gemini.gemini_tpd_limit,
            reserve_output=False,
            turn_tokens=2_048,
            submit_tokens=4_096,
            max_run_tokens=settings.max_run_tokens,
            gemini=gemini,
        )
    groq_settings = load_groq_settings()
    return ProviderConfig(
        name="groq",
        model=groq_settings.groq_model,
        tokens_per_minute=groq_settings.groq_tpm_limit,
        requests_per_minute=None,
        tokens_per_day=groq_settings.groq_tpd_limit,
        reserve_output=True,
        turn_tokens=512,
        submit_tokens=1_024,
        max_run_tokens=settings.max_run_tokens,
        groq=groq_settings,
    )


def build_chat_model(provider: ProviderConfig) -> tuple[ChatModel, Callable[[], None]]:
    """Create the provider adapter and a function that closes its client."""
    if provider.gemini is not None:
        gemini_client = genai.Client(
            api_key=provider.gemini.gemini_api_key.get_secret_value(),
            http_options=genai_types.HttpOptions(timeout=int(_REQUEST_TIMEOUT_SECONDS * 1000)),
        )
        model = GeminiChatModel(
            gemini_client.models,
            provider.model,
            thinking_level=provider.gemini.gemini_thinking_level,
        )
        return model, gemini_client.close
    if provider.groq is not None:
        groq_client = groq.Groq(
            api_key=provider.groq.groq_api_key.get_secret_value(),
            max_retries=0,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        return GroqChatModel(groq_client, provider.model), groq_client.close
    raise TynError(f"no settings loaded for provider {provider.name}")


def validate_scope_value(name: str, value: str) -> str:
    """Validate a label or release value used in JQL and file names."""
    if not _SCOPE_VALUE.fullmatch(value):
        raise TynError(f"--{name} must be letters, digits, dot, dash or underscore")
    return value


def run_assessment(
    jira: JiraSettings, provider: ProviderConfig, options: AssessOptions
) -> AssessmentOutcome:
    """Build every dependency and run the assessment."""
    estimator = TokenEstimator()
    pacer = TokenPacer(provider.tokens_per_minute, requests_per_minute=provider.requests_per_minute)
    daily = DailyBudget(
        safe_child(STATE_DIR, "usage.json"), provider.model, provider.tokens_per_day
    )
    trace = TraceRecorder()
    store = CheckpointStore(options.project, options.label, options.release)
    investigation = LoopLimits(
        max_model_calls=options.max_model_calls,
        input_budget=options.input_budget,
        turn_completion_tokens=provider.turn_tokens,
        submit_completion_tokens=provider.submit_tokens,
    )
    discovery = LoopLimits(
        max_model_calls=6,
        max_tool_calls=6,
        input_budget=max(options.input_budget, 4_000),
        turn_completion_tokens=provider.turn_tokens,
        submit_completion_tokens=provider.submit_tokens,
    )
    config = AssessConfig(
        release=options.release,
        resume=options.resume,
        only=options.only,
        discovery_limits=discovery,
        investigation_limits=investigation,
        overall_seconds=options.time_limit_minutes * 60,
    )
    provenance = Provenance(provider.name, provider.model, prompt_fingerprint())
    inner, close = build_chat_model(provider)
    try:
        with build_http_client(jira) as http_client:
            toolbox = JiraToolbox(JiraReader(JiraHttp(http_client)), options.project, options.label)
            model = ResilientModel(
                inner,
                pacer=pacer,
                daily=daily,
                estimator=estimator,
                reserve_output=provider.reserve_output,
                max_run_tokens=provider.max_run_tokens,
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
                provenance=provenance,
            )
            return runner.run()
    finally:
        close()
