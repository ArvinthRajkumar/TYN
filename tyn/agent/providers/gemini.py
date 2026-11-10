"""Gemini adapter over the official Google Gen AI SDK (``google-genai``).

Behaviour follows the Gemini API documentation for ``generateContent``:

* Function calling is manual: automatic function calling is disabled and the
  agent's loop executes tools. A verdict is forced with
  ``FunctionCallingConfig(mode=ANY, allowed_function_names=[...])``.
* The model may return several function calls in one turn. All results are
  returned together in the next turn, matched by call id when the API
  provides one.
* Function-call parts can carry an opaque ``thought_signature`` that must be
  sent back unchanged, so the model's previous turn is replayed verbatim
  from ``_provider_content`` rather than rebuilt.
* Tool schemas are reduced to the OpenAPI subset Gemini supports
  (``additionalProperties`` and other unsupported keys are dropped).
* Thinking tokens are reported separately and billed as output; the
  thinking level is set low to control cost.
* 429 (rate limit or daily quota) and 402 (prepaid credits depleted) map to
  distinct errors; 408 and 5xx are transient.
"""

import json
from typing import Any, Final, Protocol

import httpx
from google.genai import errors, types

from tyn.agent.llm import (
    PROVIDER_CONTENT_KEY,
    ChatResponse,
    ContextTooLargeError,
    ModelError,
    ModelRateLimitedError,
    ModelTransientError,
    PrepaidCreditsDepletedError,
    ToolCall,
    ToolCallGenerationError,
)
from tyn.agent.models import TokenUsage
from tyn.errors import TynError
from tyn.retry import parse_retry_after

GENERATED_ID_PREFIX: Final = "gen-"
_SCHEMA_KEYS: Final = frozenset(
    {
        "type",
        "description",
        "enum",
        "items",
        "properties",
        "required",
        "nullable",
        "format",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
    }
)
_KEY_NAMES: Final = {"minItems": "min_items", "maxItems": "max_items"}
_GENERATION_FAILURES: Final = frozenset({"MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL"})
_MESSAGE_CHARS: Final = 200


class GenerateContentClient(Protocol):
    """The part of ``genai.Client.models`` the adapter uses."""

    def generate_content(
        self, *, model: str, contents: list[types.Content], config: types.GenerateContentConfig
    ) -> types.GenerateContentResponse:
        """Send one generateContent request."""
        ...


def gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Reduce a JSON schema to the subset Gemini's function declarations accept."""
    reduced: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _SCHEMA_KEYS:
            continue
        if key == "type":
            reduced["type"] = str(value).upper()
        elif key == "properties":
            reduced["properties"] = {name: gemini_schema(sub) for name, sub in value.items()}
        elif key == "items":
            reduced["items"] = gemini_schema(value)
        else:
            reduced[_KEY_NAMES.get(key, key)] = value
    return reduced


def function_declaration(tool: dict[str, Any]) -> types.FunctionDeclaration:
    """Convert one tool schema (OpenAI function shape) to a Gemini declaration."""
    function = tool["function"]
    parameters = gemini_schema(function.get("parameters") or {"type": "object"})
    return types.FunctionDeclaration(
        name=function["name"],
        description=function.get("description", ""),
        parameters=types.Schema.model_validate(parameters),
    )


def tool_config(choice: str | dict[str, Any]) -> types.ToolConfig:
    """Translate the neutral tool choice into a Gemini function-calling config."""
    if isinstance(choice, dict):
        config = types.FunctionCallingConfig(
            mode=types.FunctionCallingConfigMode.ANY,
            allowed_function_names=[choice["function"]["name"]],
        )
    else:
        mode = {
            "auto": types.FunctionCallingConfigMode.AUTO,
            "none": types.FunctionCallingConfigMode.NONE,
            "required": types.FunctionCallingConfigMode.ANY,
        }[choice]
        config = types.FunctionCallingConfig(mode=mode)
    return types.ToolConfig(function_calling_config=config)


def _response_payload(content: str) -> dict[str, Any]:
    if content.startswith("error: "):
        return {"error": content.removeprefix("error: ")}
    return {"result": content}


def _rebuilt_model_content(message: dict[str, Any]) -> types.Content:
    # Only used when no verbatim provider content exists (for example a
    # history written by another adapter); real Gemini turns are replayed.
    parts: list[types.Part] = []
    if message.get("content"):
        parts.append(types.Part(text=str(message["content"])))
    for call in message.get("tool_calls") or []:
        function = call.get("function", {})
        call_id = str(call.get("id", ""))
        parts.append(
            types.Part(
                function_call=types.FunctionCall(
                    id=None if call_id.startswith(GENERATED_ID_PREFIX) else call_id or None,
                    name=function.get("name", ""),
                    args=json.loads(function.get("arguments") or "{}"),
                )
            )
        )
    return types.Content(role="model", parts=parts)


def to_contents(messages: list[dict[str, Any]]) -> tuple[str | None, list[types.Content]]:
    """Convert neutral messages to a system instruction and Gemini contents."""
    system: list[str] = []
    contents: list[types.Content] = []
    user_parts: list[types.Part] = []

    def close_user_turn() -> None:
        # Function responses and any follow-up instruction form one user turn,
        # so roles keep alternating between user and model.
        if user_parts:
            contents.append(types.Content(role="user", parts=list(user_parts)))
            user_parts.clear()

    for message in messages:
        role = message.get("role")
        if role == "system":
            system.append(str(message.get("content") or ""))
            continue
        if role == "assistant":
            close_user_turn()
            verbatim = message.get(PROVIDER_CONTENT_KEY)
            content = verbatim if isinstance(verbatim, types.Content) else None
            contents.append(content or _rebuilt_model_content(message))
            continue
        if role == "tool":
            call_id = str(message.get("tool_call_id", ""))
            part = types.Part(
                function_response=types.FunctionResponse(
                    id=None if call_id.startswith(GENERATED_ID_PREFIX) else call_id or None,
                    name=str(message.get("name") or "tool"),
                    response=_response_payload(str(message.get("content") or "")),
                )
            )
        else:
            part = types.Part(text=str(message.get("content") or ""))
        user_parts.append(part)
    close_user_turn()
    return ("\n\n".join(system) or None), contents


def usage_from(metadata: types.GenerateContentResponseUsageMetadata | None) -> TokenUsage:
    """Map Gemini usage metadata; output includes thinking tokens."""
    if metadata is None:
        return TokenUsage()
    thoughts = metadata.thoughts_token_count or 0
    return TokenUsage(
        prompt=(metadata.prompt_token_count or 0) + (metadata.tool_use_prompt_token_count or 0),
        completion=(metadata.candidates_token_count or 0) + thoughts,
        reasoning=thoughts,
        cached=metadata.cached_content_token_count or 0,
    )


def _retry_info(details: Any) -> tuple[float | None, bool]:
    """Return the server's retry delay and whether a daily quota is exhausted."""
    error = details.get("error", details) if isinstance(details, dict) else {}
    items = error.get("details", []) if isinstance(error, dict) else []
    delay: float | None = None
    daily = False
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("@type", ""))
        if kind.endswith("RetryInfo"):
            delay = parse_retry_after(str(item.get("retryDelay", "")).removesuffix("s"))
        if kind.endswith("QuotaFailure"):
            for violation in item.get("violations", []) or []:
                quota = str(violation.get("quotaId", "")) if isinstance(violation, dict) else ""
                daily = daily or "perday" in quota.lower().replace("_", "")
    return delay, daily


def api_error(exc: errors.APIError) -> TynError:
    """Map a Gemini API error to this package's exceptions."""
    code = exc.code
    message = " ".join(str(exc.message or "").split())[:_MESSAGE_CHARS]
    if code == 429:
        delay, daily = _retry_info(exc.details)
        return ModelRateLimitedError(delay, daily_quota=daily)
    if code == 402:
        return PrepaidCreditsDepletedError()
    lowered = message.lower()
    if code == 413 or (code == 400 and "token" in lowered and "exceed" in lowered):
        return ContextTooLargeError(f"request too large for the model (HTTP {code})")
    if code == 408 or code >= 500:
        return ModelTransientError(f"model provider error HTTP {code}")
    return ModelError(f"model request rejected: HTTP {code} {exc.status or ''}: {message}")


class GeminiChatModel:
    """Single-request adapter over the Google Gen AI SDK."""

    def __init__(
        self, models: GenerateContentClient, model: str, *, thinking_level: str | None = "low"
    ) -> None:
        self._models = models
        self.model_name = model
        self._thinking_level = thinking_level
        self._generated = 0

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: str | dict[str, Any],
        max_completion_tokens: int,
    ) -> ChatResponse:
        """Send one request; map provider failures to typed errors."""
        system, contents = to_contents(messages)
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=[function_declaration(t) for t in tools])]
            if tools
            else None,
            tool_config=tool_config(tool_choice) if tools else None,
            max_output_tokens=max_completion_tokens,
            thinking_config=types.ThinkingConfig(thinking_level=self._thinking_level.upper())  # type: ignore[arg-type]
            if self._thinking_level
            else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        try:
            response = self._models.generate_content(
                model=self.model_name, contents=contents, config=config
            )
        except errors.APIError as exc:
            raise api_error(exc) from None
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ModelTransientError(f"model request failed: {type(exc).__name__}") from None
        return self._parse(response)

    def _parse(self, response: types.GenerateContentResponse) -> ChatResponse:
        candidates = response.candidates or []
        if not candidates:
            feedback = response.prompt_feedback
            reason = getattr(feedback, "block_reason", None)
            raise ModelError(f"the model returned no candidate (block reason {reason})")
        candidate = candidates[0]
        finish = candidate.finish_reason.value if candidate.finish_reason else None
        content = candidate.content
        calls: list[ToolCall] = []
        texts: list[str] = []
        for part in content.parts if content and content.parts else []:
            if part.function_call is not None:
                calls.append(self._tool_call(part.function_call))
            elif part.text and not part.thought:
                texts.append(part.text)
        if not calls and finish in _GENERATION_FAILURES:
            raise ToolCallGenerationError(f"the model produced an invalid function call ({finish})")
        if not calls and not texts and finish == "MAX_TOKENS":
            raise ToolCallGenerationError("the output limit was reached before any reply")
        return ChatResponse(
            content="\n".join(texts) or None,
            tool_calls=calls,
            usage=usage_from(response.usage_metadata),
            finish_reason=finish,
            provider_content=content,
        )

    def _tool_call(self, call: types.FunctionCall) -> ToolCall:
        call_id = call.id
        if not call_id:
            # The API does not always assign ids; generated ones are never sent
            # back, the response is matched by position and name instead.
            self._generated += 1
            call_id = f"{GENERATED_ID_PREFIX}{self._generated}"
        return ToolCall(call_id, call.name or "", json.dumps(call.args or {}))
