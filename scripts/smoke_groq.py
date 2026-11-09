"""Groq smoke test: one tool-call round trip and a rate-limit accounting probe.

Run from the repository root:

    python -m scripts.smoke_groq

It prints token usage (including reasoning and cached tokens when the API
reports them), latency and the ``x-ratelimit-*`` response headers. It never
prints the API key. Total cost is roughly 1,500 tokens.

The probe answers an undocumented question: does ``max_completion_tokens``
count against the tokens-per-minute budget up front, or only the tokens
actually generated? Before each probe request it waits until the token bucket
is full again (``x-ratelimit-reset-tokens``), so ``limit - remaining`` in the
response is the charge for that request alone. That charge is compared with
``prompt + cap`` and ``prompt + actual completion`` for several caps.
"""

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Final

from groq import Groq

from tyn.config import load_groq_settings
from tyn.redact import redactor_from_settings

PROBE_CAPS: Final = (64, 256, 1024, 4096)
_DURATION: Final = re.compile(r"^(?:(?P<m>\d+)m)?(?:(?P<s>[\d.]+)s)?$|^(?P<ms>[\d.]+)ms$")

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
FAKE_BUILDS: Final = {"B-7": "passed", "B-8": "failed"}


@dataclass
class CallResult:
    """What one request returned, minus anything sensitive."""

    label: str
    seconds: float
    usage: dict[str, Any]
    headers: dict[str, str]
    message: Any


def call(client: Groq, label: str, **kwargs: Any) -> CallResult:
    """Send one chat completion and capture usage and rate-limit headers."""
    started = time.monotonic()
    raw = client.chat.completions.with_raw_response.create(**kwargs)
    seconds = time.monotonic() - started
    completion = raw.parse()
    headers = {
        k: v for k, v in raw.headers.items() if k.startswith("x-ratelimit-") or k == "retry-after"
    }
    usage = completion.usage.model_dump(exclude_none=True) if completion.usage else {}
    return CallResult(label, seconds, usage, headers, completion.choices[0].message)


def show(result: CallResult) -> None:
    """Print one call's measurements."""
    print(f"--- {result.label} ({result.seconds:.2f}s)")
    print("usage:", json.dumps(result.usage, sort_keys=True))
    print("headers:", json.dumps(dict(sorted(result.headers.items()))))


def round_trip(client: Groq, model: str) -> None:
    """Force a tool call, return a tool result, and read the final answer."""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "Answer only from tool results."},
        {"role": "user", "content": "What is the status of build B-7?"},
    ]
    first = call(
        client,
        "forced tool call (tool_choice names lookup_build)",
        model=model,
        messages=messages,
        tools=[TOOL],
        tool_choice={"type": "function", "function": {"name": "lookup_build"}},
        reasoning_effort="low",
        include_reasoning=False,
        max_completion_tokens=256,
    )
    show(first)
    calls = first.message.tool_calls or []
    print("tool calls:", [(c.function.name, c.function.arguments) for c in calls])
    if not calls:
        print("no tool call returned; stopping round trip")
        return
    messages.append(first.message.model_dump(exclude_none=True))
    for tool_call in calls:
        try:
            build_id = json.loads(tool_call.function.arguments).get("build_id", "")
            content = json.dumps({"build_id": build_id, "status": FAKE_BUILDS.get(build_id)})
        except json.JSONDecodeError:
            content = json.dumps({"error": "arguments were not valid JSON"})
        messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": content})
    second = call(
        client,
        "final answer after tool result",
        model=model,
        messages=messages,
        tools=[TOOL],
        tool_choice="none",
        reasoning_effort="low",
        include_reasoning=False,
        max_completion_tokens=256,
    )
    show(second)
    print("answer:", (second.message.content or "").strip()[:200])


def parallel_check(client: Groq, model: str) -> None:
    """Ask for two independent lookups and count tool calls in one reply."""
    result = call(
        client,
        "parallel tool call check (two builds, tool_choice auto)",
        model=model,
        messages=[
            {"role": "system", "content": "Answer only from tool results."},
            {"role": "user", "content": "Give the status of builds B-7 and B-8."},
        ],
        tools=[TOOL],
        tool_choice="auto",
        reasoning_effort="low",
        include_reasoning=False,
        max_completion_tokens=256,
    )
    show(result)
    count = len(result.message.tool_calls or [])
    print(f"tool calls in one reply: {count} ({'parallel' if count > 1 else 'sequential'})")


def parse_duration(text: str) -> float:
    """Parse Groq reset durations such as ``7.66s``, ``2m59.56s`` or ``450ms``."""
    match = _DURATION.match(text.strip())
    if match is None:
        return 10.0
    if match["ms"]:
        return float(match["ms"]) / 1000
    return int(match["m"] or 0) * 60 + float(match["s"] or 0)


def reservation_probe(client: Groq, model: str) -> None:
    """Measure the admission charge of one request at several completion caps."""
    time.sleep(10.0)
    rows: list[tuple[int, int, int, int]] = []
    for cap in PROBE_CAPS:
        result = call(
            client,
            f"probe max_completion_tokens={cap}",
            model=model,
            messages=[{"role": "user", "content": "Reply with the single word OK."}],
            reasoning_effort="low",
            include_reasoning=False,
            max_completion_tokens=cap,
        )
        show(result)
        limit = int(result.headers.get("x-ratelimit-limit-tokens", "-1"))
        remaining = int(result.headers.get("x-ratelimit-remaining-tokens", "-1"))
        if min(limit, remaining) < 0:
            print("probe inconclusive: rate-limit headers missing")
            return
        prompt = int(result.usage.get("prompt_tokens", 0))
        completion = int(result.usage.get("completion_tokens", 0))
        rows.append((cap, prompt, completion, limit - remaining))
        time.sleep(parse_duration(result.headers.get("x-ratelimit-reset-tokens", "10s")) + 1.0)
    print("cap | prompt | completion | admission charge | prompt+cap | prompt+actual")
    for cap, prompt, completion, charge in rows:
        print(
            f"{cap} | {prompt} | {completion} | {charge} | {prompt + cap} | {prompt + completion}"
        )
    for cap, prompt, completion, charge in rows:
        near_cap = abs(charge - (prompt + cap)) <= abs(charge - (prompt + completion))
        verdict = "prompt + cap" if near_cap else "below prompt + cap"
        print(f"cap {cap}: admission charge {charge} is closest to {verdict}")


def main() -> int:
    """Run all checks and return a process exit code."""
    settings = load_groq_settings()
    redactor = redactor_from_settings(groq=settings)
    client = Groq(api_key=settings.groq_api_key.get_secret_value(), max_retries=0, timeout=60.0)
    print(f"model: {settings.groq_model}")
    try:
        round_trip(client, settings.groq_model)
        parallel_check(client, settings.groq_model)
        reservation_probe(client, settings.groq_model)
    except Exception as exc:  # noqa: BLE001 - report any SDK failure cleanly
        print(f"smoke test failed: {redactor.redact(type(exc).__name__ + ': ' + str(exc))}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
