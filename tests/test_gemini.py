"""Gemini adapter against a mocked Google Gen AI client."""

import json
from pathlib import Path
from typing import Any

import pytest
from google.genai import errors, types

from tests.agent_support import AgentWorld, build_world
from tyn.agent.budget import TokenEstimator
from tyn.agent.investigate import Investigator, investigation_tools
from tyn.agent.llm import (
    PROVIDER_CONTENT_KEY,
    ContextTooLargeError,
    ModelError,
    ModelRateLimitedError,
    ModelTransientError,
    PrepaidCreditsDepletedError,
    RateLimitWaitTooLongError,
    ResilientModel,
    ToolCallGenerationError,
)
from tyn.agent.loop import AgentLoop, LoopLimits
from tyn.agent.models import Decision, RequirementTarget, TokenUsage
from tyn.agent.pacer import TokenPacer
from tyn.agent.prompts import DISCOVERY_SUBMIT, RECORD_FINDING, VERDICT_SUBMIT
from tyn.agent.providers.gemini import (
    GeminiChatModel,
    function_declaration,
    gemini_schema,
    to_contents,
    usage_from,
)
from tyn.agent.tools import SCHEMAS
from tyn.agent.trace import TraceRecorder

USAGE = {
    "prompt_token_count": 120,
    "candidates_token_count": 30,
    "thoughts_token_count": 12,
    "cached_content_token_count": 0,
    "total_token_count": 162,
}


def reply(parts: list[dict[str, Any]], finish: str = "STOP") -> types.GenerateContentResponse:
    return types.GenerateContentResponse.model_validate(
        {
            "candidates": [{"content": {"role": "model", "parts": parts}, "finish_reason": finish}],
            "usage_metadata": USAGE,
        }
    )


def fcall(
    name: str, args: dict[str, Any], call_id: str | None, sig: bytes | None = None
) -> dict[str, Any]:
    part: dict[str, Any] = {"function_call": {"id": call_id, "name": name, "args": args}}
    if sig is not None:
        part["thought_signature"] = sig
    return part


def client_error(
    code: int, details: list[dict[str, Any]] | None = None, message: str = "x"
) -> errors.ClientError:
    body = {"error": {"code": code, "message": message, "status": "S", "details": details or []}}
    return errors.ClientError(code, body)


class FakeModels:
    """Records generate_content calls and replays scripted replies or errors."""

    def __init__(self, replies: list[types.GenerateContentResponse | Exception]) -> None:
        self.replies = replies
        self.calls: list[dict[str, Any]] = []

    def generate_content(
        self, *, model: str, contents: list[types.Content], config: types.GenerateContentConfig
    ) -> types.GenerateContentResponse:
        self.calls.append({"model": model, "contents": list(contents), "config": config})
        if not self.replies:
            raise AssertionError("fake Gemini ran out of replies")
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def resilient(fake: FakeModels, **kwargs: Any) -> tuple[ResilientModel, list[float]]:
    sleeps: list[float] = []
    model = ResilientModel(
        GeminiChatModel(fake, "gemini-test"),
        pacer=TokenPacer(1_000_000, requests_per_minute=1_000, sleep=sleeps.append),
        daily=None,
        estimator=TokenEstimator(),
        reserve_output=False,
        sleep=sleeps.append,
        **kwargs,
    )
    return model, sleeps


MESSAGES = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
GET_ISSUE = [SCHEMAS["get_issue"]]


def test_every_agent_tool_schema_converts_to_a_declaration() -> None:
    for schema in [*SCHEMAS.values(), RECORD_FINDING, VERDICT_SUBMIT, DISCOVERY_SUBMIT]:
        declaration = function_declaration(schema)
        assert declaration.name == schema["function"]["name"]


def test_schema_is_reduced_to_the_supported_subset() -> None:
    reduced = gemini_schema(SCHEMAS["check_evidence_scope"]["function"]["parameters"])
    assert "additionalProperties" not in json.dumps(reduced)
    assert reduced["type"] == "OBJECT"
    assert reduced["properties"]["evidence_keys"] == {
        "type": "ARRAY",
        "items": {"type": "STRING"},
        "max_items": 5,
    }


def test_request_config_disables_auto_calling_and_sets_thinking() -> None:
    fake = FakeModels([reply([{"text": "done"}])])
    GeminiChatModel(fake, "gemini-test", thinking_level="low").complete(
        MESSAGES, GET_ISSUE, "auto", 256
    )
    config = fake.calls[0]["config"]
    assert config.system_instruction == "sys"
    assert config.automatic_function_calling.disable is True
    assert config.thinking_config.thinking_level == types.ThinkingLevel.LOW
    assert config.max_output_tokens == 256
    assert config.tool_config.function_calling_config.mode == types.FunctionCallingConfigMode.AUTO


def test_forced_verdict_uses_mode_any_with_one_allowed_name() -> None:
    fake = FakeModels([reply([fcall("submit_verdict", {"decision": "PASS"}, "v1")])])
    forced = {"type": "function", "function": {"name": "submit_verdict"}}
    GeminiChatModel(fake, "gemini-test").complete(MESSAGES, [VERDICT_SUBMIT], forced, 512)
    calling = fake.calls[0]["config"].tool_config.function_calling_config
    assert calling.mode == types.FunctionCallingConfigMode.ANY
    assert calling.allowed_function_names == ["submit_verdict"]


def test_usage_includes_tool_prompt_and_thinking_tokens() -> None:
    metadata = types.GenerateContentResponseUsageMetadata(
        prompt_token_count=100,
        tool_use_prompt_token_count=5,
        candidates_token_count=20,
        thoughts_token_count=7,
        cached_content_token_count=40,
    )
    assert usage_from(metadata) == TokenUsage(prompt=105, completion=27, reasoning=7, cached=40)


def test_multiple_calls_in_one_turn_are_parsed_and_all_answered() -> None:
    fake = FakeModels(
        [
            reply(
                [
                    fcall("get_issue", {"issue_key": "TYN-1"}, "c1", b"sig"),
                    fcall("get_issue", {"issue_key": "TYN-2"}, "c2"),
                ]
            )
        ]
    )
    response = GeminiChatModel(fake, "gemini-test").complete(MESSAGES, GET_ISSUE, "auto", 512)
    assert [(c.id, json.loads(c.arguments)) for c in response.tool_calls] == [
        ("c1", {"issue_key": "TYN-1"}),
        ("c2", {"issue_key": "TYN-2"}),
    ]
    history = [
        *MESSAGES,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [],
            PROVIDER_CONTENT_KEY: response.provider_content,
        },
        {"role": "tool", "tool_call_id": "c1", "name": "get_issue", "content": "one"},
        {"role": "tool", "tool_call_id": "c2", "name": "get_issue", "content": "error: two"},
        {"role": "user", "content": "now decide"},
    ]
    system, contents = to_contents(history)
    assert system == "sys"
    assert [c.role for c in contents] == ["user", "model", "user"]
    answers = contents[2].parts or []
    assert [
        (p.function_response.id, p.function_response.name)
        for p in answers[:2]
        if p.function_response
    ] == [
        ("c1", "get_issue"),
        ("c2", "get_issue"),
    ]
    assert answers[0].function_response.response == {"result": "one"}  # type: ignore[union-attr]
    assert answers[1].function_response.response == {"error": "two"}  # type: ignore[union-attr]
    assert answers[2].text == "now decide"


def test_model_turn_is_replayed_verbatim_with_its_thought_signature() -> None:
    fake = FakeModels([reply([fcall("get_issue", {"issue_key": "TYN-1"}, "c1", b"opaque-sig")])])
    response = GeminiChatModel(fake, "gemini-test").complete(MESSAGES, GET_ISSUE, "auto", 512)
    history = [
        *MESSAGES,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [],
            PROVIDER_CONTENT_KEY: response.provider_content,
        },
        {"role": "tool", "tool_call_id": "c1", "name": "get_issue", "content": "ok"},
    ]
    _, contents = to_contents(history)
    assert contents[1] is response.provider_content
    assert (contents[1].parts or [])[0].thought_signature == b"opaque-sig"


def test_generated_call_ids_are_never_sent_back() -> None:
    fake = FakeModels([reply([fcall("get_issue", {"issue_key": "TYN-1"}, None)])])
    response = GeminiChatModel(fake, "gemini-test").complete(MESSAGES, GET_ISSUE, "auto", 512)
    call_id = response.tool_calls[0].id
    assert call_id.startswith("gen-")
    _, contents = to_contents(
        [{"role": "tool", "tool_call_id": call_id, "name": "get_issue", "content": "ok"}]
    )
    assert (contents[0].parts or [])[0].function_response.id is None  # type: ignore[union-attr]


def test_malformed_function_call_is_retried() -> None:
    fake = FakeModels(
        [
            reply([], finish="MALFORMED_FUNCTION_CALL"),
            reply([fcall("get_issue", {"issue_key": "TYN-1"}, "c1")]),
        ]
    )
    model, sleeps = resilient(fake)
    response = model.complete(MESSAGES, GET_ISSUE, "auto", 512)
    assert response.tool_calls[0].name == "get_issue"
    assert model.stats.retries == 1
    assert len(fake.calls) == 2
    assert sleeps


def test_malformed_function_call_without_retries_raises_generation_error() -> None:
    fake = FakeModels([reply([], finish="MALFORMED_FUNCTION_CALL")])
    with pytest.raises(ToolCallGenerationError):
        GeminiChatModel(fake, "gemini-test").complete(MESSAGES, GET_ISSUE, "auto", 512)


def test_rate_limit_honours_the_server_retry_delay() -> None:
    retry = [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"}]
    fake = FakeModels([client_error(429, retry), reply([{"text": "ok"}])])
    model, sleeps = resilient(fake)
    model.complete(MESSAGES, GET_ISSUE, "auto", 512)
    assert max(sleeps) >= 7.0
    assert model.stats.rate_limit_waits == 1


def test_rate_limit_error_carries_delay_and_kind() -> None:
    retry = [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "12.5s"}]
    fake = FakeModels([client_error(429, retry)])
    with pytest.raises(ModelRateLimitedError) as caught:
        GeminiChatModel(fake, "gemini-test").complete(MESSAGES, GET_ISSUE, "auto", 512)
    assert caught.value.retry_after == 12.5
    assert caught.value.daily_quota is False


def test_daily_quota_exhaustion_stops_cleanly() -> None:
    quota = [
        {
            "@type": "type.googleapis.com/google.rpc.QuotaFailure",
            "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}],
        }
    ]
    model, _ = resilient(FakeModels([client_error(429, quota)]))
    with pytest.raises(RateLimitWaitTooLongError, match="daily quota"):
        model.complete(MESSAGES, GET_ISSUE, "auto", 512)


def test_depleted_prepaid_credits_are_not_retried() -> None:
    fake = FakeModels([client_error(402, message="Your Prepay credit balance is depleted.")])
    model, _ = resilient(fake)
    with pytest.raises(PrepaidCreditsDepletedError, match="402"):
        model.complete(MESSAGES, GET_ISSUE, "auto", 512)
    assert len(fake.calls) == 1


@pytest.mark.parametrize(
    ("code", "message", "expected"),
    [
        (500, "internal", ModelTransientError),
        (503, "overloaded", ModelTransientError),
        (408, "timeout", ModelTransientError),
        (400, "The input token count exceeds the maximum", ContextTooLargeError),
        (400, "Invalid JSON payload", ModelError),
        (403, "Permission denied", ModelError),
    ],
)
def test_other_errors_are_classified(code: int, message: str, expected: type) -> None:
    error: Exception = (
        errors.ServerError(code, {"error": {"code": code, "message": message}})
        if code >= 500
        else client_error(code, message=message)
    )
    fake = FakeModels([error])
    with pytest.raises(expected) as caught:
        GeminiChatModel(fake, "gemini-test").complete(MESSAGES, GET_ISSUE, "auto", 512)
    assert type(caught.value) is expected


def test_empty_candidates_are_a_model_error() -> None:
    blocked = types.GenerateContentResponse.model_validate(
        {"candidates": [], "prompt_feedback": {"block_reason": "SAFETY"}}
    )
    with pytest.raises(ModelError, match="no candidate"):
        GeminiChatModel(FakeModels([blocked]), "gemini-test").complete(
            MESSAGES, GET_ISSUE, "auto", 512
        )


@pytest.fixture
def world(tmp_path: Path) -> AgentWorld:
    return build_world(tmp_path)


def test_loop_answers_parallel_calls_and_forces_the_verdict(world: AgentWorld) -> None:
    passed = next(
        str(c["id"])
        for c in world.fake.comments[world.key("EV-10")]
        if "10 of 10" in json.dumps(c["body"])
    )
    verdict_args = {
        "decision": "PASS",
        "explanation": "Target-scope run passes.",
        "supporting_evidence": [
            {"issue_key": world.key("EV-10"), "comment_id": passed, "note": "10 of 10"}
        ],
        "rejected_evidence": [],
    }
    fake = FakeModels(
        [
            reply(
                [
                    fcall("get_issue", {"issue_key": world.key("EV-10")}, "p1", b"s1"),
                    fcall("get_issue", {"issue_key": world.key("EV-11")}, "p2"),
                ]
            ),
            reply([fcall("submit_verdict", verdict_args, "v1")]),
        ]
    )
    model, _ = resilient(fake)
    loop = AgentLoop(
        model=model,
        toolbox=world.toolbox,
        estimator=TokenEstimator(),
        trace=TraceRecorder(),
        limits=LoopLimits(),
    )
    key = world.key("RQ-1")
    result = Investigator(loop, world.toolbox, release="R5.0").investigate(
        RequirementTarget(issue_key=key, source_id="RQ-1", summary="Requirement RQ-1"),
        requirement_text=world.toolbox.view(key).description,
        manifest_key=world.key("MAN-1"),
        manifest_rules="Assess the linked requirements.",
    )
    assert result.verdict.decision is Decision.PASS
    assert result.usage.reasoning == 24
    second = fake.calls[1]
    contents: list[types.Content] = second["contents"]
    assert contents[1].parts[0].thought_signature == b"s1"  # type: ignore[index]
    answers = [p.function_response for p in contents[2].parts or [] if p.function_response]
    assert [(a.id, a.name) for a in answers] == [("p1", "get_issue"), ("p2", "get_issue")]
    assert "<jira_data" in str(answers[0].response)
    assert len(investigation_tools()) == len(second["config"].tools[0].function_declarations)
