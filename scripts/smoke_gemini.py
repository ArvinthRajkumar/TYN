"""Gemini smoke test: function-call round trips through the real adapter.

Run from the repository root:

    python -m scripts.smoke_gemini

Steps:

1. A forced function call (mode ANY, one allowed function name).
2. The tool result is returned with the model's previous turn replayed
   verbatim (including any thought signatures), and the model answers.
3. A request that invites two calls in one turn; every result is returned
   together and the model answers.

It prints the model id, finish reasons, function calls, whether thought
signatures were present, and token usage (prompt, output, thinking, cached).
It never prints the API key. Total cost is a few thousand tokens.
"""

import json
import time
from typing import Any, Final

from google import genai
from google.genai import types

from tyn.agent.llm import PROVIDER_CONTENT_KEY, ChatResponse
from tyn.agent.providers.gemini import GeminiChatModel
from tyn.config import load_gemini_settings
from tyn.errors import TynError
from tyn.redact import redactor_from_settings

TOOL: Final = {
    "type": "function",
    "function": {
        "name": "lookup_build",
        "description": "Return the status of one build by id.",
        "parameters": {
            "type": "object",
            "properties": {"build_id": {"type": "string", "description": "Build id, e.g. B-7"}},
            "required": ["build_id"],
            "additionalProperties": False,
        },
    },
}
BUILDS: Final = {"B-7": "passed", "B-8": "failed"}
SYSTEM: Final = "Answer only from tool results. Be brief."


def signatures(response: ChatResponse) -> int:
    """Count parts in the model turn that carry a thought signature."""
    content = response.provider_content
    parts = content.parts if isinstance(content, types.Content) and content.parts else []
    return sum(1 for part in parts if part.thought_signature)


def show(label: str, response: ChatResponse, seconds: float) -> None:
    """Print one response's measurements."""
    usage = response.usage
    print(f"--- {label} ({seconds:.2f}s) finish={response.finish_reason}")
    print(
        f"usage: prompt={usage.prompt} output={usage.completion} "
        f"(thinking={usage.reasoning}) cached={usage.cached}"
    )
    print(f"function calls: {[(c.name, c.arguments) for c in response.tool_calls]}")
    print(f"thought signatures in model turn: {signatures(response)}")
    if response.content:
        print(f"text: {response.content.strip()[:200]}")


def assistant(response: ChatResponse) -> dict[str, Any]:
    """Build the neutral assistant message, keeping the verbatim model turn."""
    return {
        "role": "assistant",
        "content": response.content or "",
        "tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
            for c in response.tool_calls
        ],
        PROVIDER_CONTENT_KEY: response.provider_content,
    }


def results(response: ChatResponse) -> list[dict[str, Any]]:
    """Return one tool message per call, answered from the fake build table."""
    messages = []
    for call in response.tool_calls:
        build_id = json.loads(call.arguments).get("build_id", "")
        content = json.dumps({"build_id": build_id, "status": BUILDS.get(build_id)})
        messages.append(
            {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": content}
        )
    return messages


def timed(
    model: GeminiChatModel, messages: list[dict[str, Any]], choice: Any
) -> tuple[ChatResponse, float]:
    """Send one request and return the response with its latency."""
    started = time.monotonic()
    response = model.complete(messages, [TOOL], choice, 2048)
    return response, time.monotonic() - started


def main() -> int:
    """Run the smoke test and return a process exit code."""
    settings = load_gemini_settings()
    redactor = redactor_from_settings(gemini=settings)
    client = genai.Client(
        api_key=settings.gemini_api_key.get_secret_value(),
        http_options=types.HttpOptions(timeout=60_000),
    )
    model = GeminiChatModel(
        client.models, settings.gemini_model, thinking_level=settings.gemini_thinking_level
    )
    print(f"model: {settings.gemini_model} thinking_level: {settings.gemini_thinking_level}")
    try:
        forced = {"type": "function", "function": {"name": "lookup_build"}}
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": "What is the status of build B-7?"},
        ]
        first, seconds = timed(model, messages, forced)
        show("1. forced call (mode ANY, allowed lookup_build)", first, seconds)
        messages += [assistant(first), *results(first)]
        second, seconds = timed(model, messages, "auto")
        show("2. answer after verbatim replay of the model turn", second, seconds)

        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": "Give the status of builds B-7 and B-8."},
        ]
        third, seconds = timed(model, messages, "auto")
        show("3. two builds in one question (mode AUTO)", third, seconds)
        print(f"calls in one turn: {len(third.tool_calls)}")
        messages += [assistant(third), *results(third)]
        fourth, seconds = timed(model, messages, "auto")
        show("4. answer after returning every result in one turn", fourth, seconds)
    except TynError as exc:
        print(f"smoke test failed: {redactor.redact(str(exc))}")
        return 1
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
