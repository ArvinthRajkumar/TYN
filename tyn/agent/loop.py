"""The bounded tool-use loop shared by discovery and investigation.

One loop runs in a fresh context: a system prompt, one seed message (rebuilt
each turn so recorded notes survive pruning) and the turns of this loop only.
Before each request the context is fitted to the input budget by replacing
the oldest tool results with short stubs. When a limit is reached the model
is forced to call the submit tool. A submit that fails validation gets one
repair round; after that the validator's fallback (or a failure) is used.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

from tyn.agent.arguments import ArgumentError, parse_arguments
from tyn.agent.budget import TokenEstimator
from tyn.agent.llm import (
    ChatModel,
    ChatResponse,
    ContextTooLargeError,
    ModelError,
    ToolCallGenerationError,
)
from tyn.agent.models import TokenUsage
from tyn.agent.tools import JiraToolbox, Ledger, ToolInfrastructureError, ToolInputError
from tyn.agent.trace import TraceRecorder

PRUNED_STUB: Final = (
    "[pruned to save context: earlier {tool} result. Your notes keep the key facts; "
    "call the tool again if you need the detail.]"
)
NOTE_MAX_CHARS: Final = 400


@dataclass(frozen=True)
class LoopLimits:
    """Bounds for one loop."""

    max_model_calls: int = 10
    max_tool_calls: int = 12
    max_seconds: float = 900.0
    input_budget: int = 5_000
    turn_completion_tokens: int = 512
    submit_completion_tokens: int = 1_024
    max_notes: int = 10
    max_repairs: int = 1


@dataclass
class SubmitCheck:
    """Result of validating a submit call."""

    ok: bool
    value: Any = None
    error: str = ""
    fallback: Any = None
    notes: list[str] = field(default_factory=list)
    force_resubmit: bool = True
    pending_reads: tuple[str, ...] = ()


@dataclass
class LoopResult:
    """What a loop produced."""

    value: Any = None
    failure: str | None = None
    validation_notes: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    model_calls: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    pruned_results: int = 0
    usage: TokenUsage = field(default_factory=TokenUsage)
    seconds: float = 0.0


Validator = Callable[[dict[str, Any], Ledger], SubmitCheck]


class AgentLoop:
    """Run one bounded conversation until the model submits."""

    def __init__(
        self,
        *,
        model: ChatModel,
        toolbox: JiraToolbox,
        estimator: TokenEstimator,
        trace: TraceRecorder,
        limits: LoopLimits,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._model = model
        self._toolbox = toolbox
        self._estimator = estimator
        self._trace = trace
        self._limits = limits
        self._monotonic = monotonic

    def run(
        self,
        *,
        scope: str,
        system: str,
        seed: Callable[[list[str]], str],
        tools: list[dict[str, Any]],
        submit_name: str,
        validate: Validator,
        ledger: Ledger,
    ) -> LoopResult:
        """Drive the conversation; never raises for model or tool mistakes."""
        state = _LoopState(started=self._monotonic(), scope=scope)
        limits = self._limits
        while True:
            if state.result.model_calls >= limits.max_model_calls + limits.max_repairs:
                return self._finish(state, failure=f"no valid {submit_name} within the call limit")
            self._maybe_force(state, submit_name)
            messages = self._fit(scope, system, seed(state.result.notes), state, tools)
            response = self._request(scope, messages, tools, state, submit_name)
            if response is None:
                return self._finish(state, failure=state.failure)
            if not response.tool_calls:
                self._after_text_reply(response, state, submit_name)
                continue
            state.turns.append(_assistant_message(response))
            for call in response.tool_calls:
                if call.name == submit_name:
                    done = self._handle_submit(call.id, call.arguments, validate, ledger, state)
                    if done:
                        return self._finish(state)
                else:
                    failure = self._handle_tool(scope, call, ledger, state)
                    if failure:
                        return self._finish(state, failure=failure)
            if state.pending_reads and set(state.pending_reads) <= ledger.opened:
                # Everything the rejection asked for has been read: decide now
                # instead of wandering into unrelated searches.
                state.pending_reads = ()
                state.forced = True

    def _after_text_reply(
        self, response: ChatResponse, state: "_LoopState", submit_name: str
    ) -> None:
        # A plain-text reply after evidence was read usually means the model
        # is ready to conclude, so the next call is forced to the submit tool.
        state.turns.append({"role": "assistant", "content": response.content or ""})
        if state.result.tool_calls:
            state.forced = True
            message = f"Call {submit_name} now with what the tools have shown."
        else:
            message = f"Reply with a tool call: use the tools, or call {submit_name}."
        state.turns.append({"role": "user", "content": message})

    def _maybe_force(self, state: "_LoopState", submit_name: str) -> None:
        limits = self._limits
        over = (
            state.result.model_calls >= limits.max_model_calls - 1
            or state.result.tool_calls >= limits.max_tool_calls
            or self._monotonic() - state.started > limits.max_seconds
        )
        if over and not state.forced:
            state.forced = True
            state.turns.append(
                {
                    "role": "user",
                    "content": f"Investigation limit reached. Call {submit_name} now using "
                    "only what the tools have shown.",
                }
            )

    def _fit(
        self, scope: str, system: str, seed: str, state: "_LoopState", tools: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        def build() -> list[dict[str, Any]]:
            head = [{"role": "system", "content": system}, {"role": "user", "content": seed}]
            return head + state.turns

        messages = build()
        tool_indexes = [i for i, m in enumerate(state.turns) if m.get("role") == "tool"]
        for index in tool_indexes[:-1]:
            if self._estimator.estimate(messages, tools) <= self._limits.input_budget:
                break
            if not state.turns[index].get("pruned"):
                self._prune(scope, state, index)
                messages = build()
        return [{k: v for k, v in m.items() if k != "pruned"} for m in messages]

    def _prune(self, scope: str, state: "_LoopState", index: int) -> None:
        message = state.turns[index]
        tool = state.tool_names.get(str(message.get("tool_call_id")), "tool")
        message["content"] = PRUNED_STUB.format(tool=tool)
        message["pruned"] = True
        state.result.pruned_results += 1
        self._trace.event("prune", scope, tool=tool, turn=state.result.model_calls)

    def _request(
        self,
        scope: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        state: "_LoopState",
        submit_name: str,
    ) -> ChatResponse | None:
        pruned_after_413 = False
        while True:
            choice: str | dict[str, Any] = (
                {"type": "function", "function": {"name": submit_name}} if state.forced else "auto"
            )
            cap = (
                self._limits.submit_completion_tokens
                if state.forced
                else self._limits.turn_completion_tokens
            )
            estimate = self._estimator.estimate(messages, tools)
            started = self._monotonic()
            try:
                response = self._model.complete(messages, tools, choice, cap)
            except ContextTooLargeError:
                if pruned_after_413:
                    state.failure = "request still too large after pruning (413)"
                    return None
                self._trace.event("prune_after_413", scope)
                for index, message in enumerate(state.turns[:-1]):
                    if message.get("role") == "tool" and not message.get("pruned"):
                        self._prune(scope, state, index)
                messages = [{k: v for k, v in m.items() if k != "pruned"} for m in messages[:2]]
                messages += [{k: v for k, v in m.items() if k != "pruned"} for m in state.turns]
                pruned_after_413 = True
                continue
            except ToolCallGenerationError as exc:
                if state.forced:
                    state.failure = f"model failure: {exc}"
                    self._trace.event("model_failure", scope, error=str(exc))
                    return None
                # Repeated malformed tool calls: ask once for the verdict itself,
                # a simpler output, before giving up on the requirement.
                self._trace.event("generation_fallback", scope, error=str(exc))
                state.forced = True
                note = {
                    "role": "user",
                    "content": f"Your last reply could not be parsed. Call {submit_name} "
                    "now with what the tools have shown.",
                }
                state.turns.append(note)
                messages = [*messages, note]
                continue
            except ModelError as exc:
                state.failure = f"model failure: {exc}"
                self._trace.event("model_failure", scope, error=str(exc))
                return None
            state.result.model_calls += 1
            state.result.usage = state.result.usage.add(response.usage)
            self._trace.model_call(
                scope,
                turn=state.result.model_calls,
                latency=self._monotonic() - started,
                usage=response.usage,
                estimate=estimate,
                tool_choice=submit_name if state.forced else "auto",
                tool_calls=[call.name for call in response.tool_calls],
                waited=response.waited_seconds,
                remaining_tokens=response.headers.get("x-ratelimit-remaining-tokens"),
            )
            return response

    def _handle_submit(
        self,
        call_id: str,
        raw: str,
        validate: Validator,
        ledger: Ledger,
        state: "_LoopState",
    ) -> bool:
        try:
            check = validate(parse_arguments(raw), ledger)
        except ArgumentError as exc:
            check = SubmitCheck(ok=False, error=str(exc))
        state.result.validation_notes.extend(check.notes)
        if check.ok:
            state.turns.append(_tool_message(call_id, "accepted"))
            state.result.value = check.value
            return True
        state.repairs += 1
        self._trace.event(
            "submit_rejected", state.scope, error=check.error[:400], repair=state.repairs
        )
        if state.repairs > self._limits.max_repairs:
            state.result.value = check.fallback
            state.result.validation_notes.append(f"submission rejected after repair: {check.error}")
            if check.fallback is None:
                state.failure = f"submission invalid: {check.error}"
            return True
        # Citation problems are fixed by resubmitting; unread linked records
        # need tool calls first, so only the former forces the submit tool.
        state.forced = check.force_resubmit
        state.pending_reads = check.pending_reads
        state.turns.append(_tool_message(call_id, f"rejected: {check.error}"))
        return False

    def _handle_tool(
        self, scope: str, call: Any, ledger: Ledger, state: "_LoopState"
    ) -> str | None:
        state.tool_names[call.id] = call.name
        started = self._monotonic()
        status = "ok"
        try:
            arguments = parse_arguments(call.arguments)
            if call.name == "record_finding":
                text = self._record_note(arguments, state)
            else:
                output = self._toolbox.run(call.name, arguments)
                ledger.record(output)
                text = output.text
        except (ArgumentError, ToolInputError) as exc:
            status = "error"
            state.result.tool_errors += 1
            text = f"error: {exc}"
            arguments = call.arguments
        except ToolInfrastructureError as exc:
            self._trace.event("tool_failure", scope, tool=call.name, error=str(exc))
            return f"Jira tool failure: {exc}"
        state.result.tool_calls += 1
        self._trace.tool_call(
            scope,
            turn=state.result.model_calls,
            tool=call.name,
            arguments=arguments,
            latency=self._monotonic() - started,
            result=text,
            status=status,
            estimated_tokens=round(self._estimator.raw_text(text) * self._estimator.factor),
        )
        state.turns.append(_tool_message(call.id, text))
        return None

    def _record_note(self, arguments: dict[str, Any], state: "_LoopState") -> str:
        note = " ".join(str(arguments.get("note", "")).split())[:NOTE_MAX_CHARS]
        if not note:
            raise ToolInputError("note must not be empty")
        if len(state.result.notes) >= self._limits.max_notes:
            return "note limit reached; submit your verdict"
        state.result.notes.append(note)
        return f"noted ({len(state.result.notes)} of {self._limits.max_notes})"

    def _finish(self, state: "_LoopState", failure: str | None = None) -> LoopResult:
        state.result.failure = failure or state.failure
        state.result.seconds = self._monotonic() - state.started
        return state.result


@dataclass
class _LoopState:
    started: float
    scope: str
    turns: list[dict[str, Any]] = field(default_factory=list)
    tool_names: dict[str, str] = field(default_factory=dict)
    forced: bool = False
    repairs: int = 0
    pending_reads: tuple[str, ...] = ()
    failure: str | None = None
    result: LoopResult = field(default_factory=LoopResult)


def _assistant_message(response: ChatResponse) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": response.content or "",
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments or "{}"},
            }
            for call in response.tool_calls
        ],
    }


def _tool_message(call_id: str, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": content}
