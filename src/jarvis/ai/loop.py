"""The agent tool loop: model → tool calls → results → model … until a final answer.

Guarantees:
- every tool call goes through `run_tool` (the agent binds it to the ToolExecutor gate);
- every tool_use gets exactly one result in the next user message (errors flagged);
- tools are never run from a reply that was cut off by the length limit;
- the history is append-only (nothing earlier is edited or removed);
- a step limit, a repeated-failure guard and cancellation stop runaway loops;
- after the user says no to an action (`ToolOutput.halt`) nothing more runs: the model only reports.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from jarvis.ai.router import ProviderRouter
from jarvis.ai.types import (
    AIMessage,
    AIResponse,
    Effort,
    ServerAction,
    Source,
    ToolCall,
    ToolOutput,
    ToolSpec,
    Usage,
)
from jarvis.common.cancel import CancelToken
from jarvis.common.errors import TaskCancelled

log = logging.getLogger(__name__)

ToolRunner = Callable[[ToolCall], Awaitable[ToolOutput]]
EventSink = Callable[[str, dict[str, Any]], Awaitable[None] | None]

MAX_LENGTH_CONTINUATIONS = 2
SAME_FAILURE_WARN = 3
SAME_FAILURE_STOP = 5
SAME_CALL_STOP = 8

STEP_LIMIT_NOTE = (
    "You have reached the step limit for this task. Do not call any more tools. "
    "Give your final report now: what you did, what you found or produced, and what is still missing."
)
REPEAT_NOTE = (
    "The same call has failed {n} times with the same error. Do not repeat it. Try a different approach, "
    "or stop and report the blocker."
)
CUT_OFF_NOTE = "Not executed: your reply hit the length limit before this call was complete. Send it again, smaller."
DECLINED_NOTE = (
    "The user did not approve that action. Do not try it another way and call no more tools: write your final "
    "report now, saying what was not approved."
)
DECLINED_CALL = "Not executed: the user did not approve an earlier action, so this task stops here."


@dataclass
class LoopResult:
    text: str
    messages: list[AIMessage]
    steps: int
    stop_reason: str                    # end | max_steps | loop_guard | max_tokens | declined
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_outputs: list[ToolOutput] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    sources: list[Source] = field(default_factory=list)
    server_actions: list[ServerAction] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    fallback_model: str | None = None

    @property
    def completed(self) -> bool:
        return self.stop_reason == "end"


async def _emit(sink: EventSink | None, kind: str, data: dict[str, Any]) -> None:
    if sink is None:
        return
    try:
        res = sink(kind, data)
        if inspect.isawaitable(res):
            await res
    except Exception:  # pragma: no cover - UI sinks must never break the loop
        log.exception("loop event sink failed")


async def _cancellable(awaitable: Awaitable[Any], cancel: CancelToken | None) -> Any:
    if cancel is None:
        return await awaitable
    cancel.raise_if_cancelled()
    task = asyncio.ensure_future(awaitable)
    waiter = asyncio.ensure_future(cancel.wait())
    try:
        done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            return task.result()
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        raise TaskCancelled(cancel.reason or "cancelled")
    finally:
        waiter.cancel()


def _signature(call: ToolCall) -> str:
    return call.name + ":" + json.dumps(call.input, sort_keys=True, ensure_ascii=False, default=str)


async def run_tool_loop(
    router: ProviderRouter,
    *,
    role: str,
    system: str,
    messages: list[AIMessage],
    tools: Sequence[ToolSpec],
    run_tool: ToolRunner,
    read_only: Callable[[str], bool] = lambda name: False,
    cancel: CancelToken | None = None,
    max_steps: int = 30,
    effort: Effort | None = None,
    max_tokens: int | None = None,
    web_search_uses: int = 0,
    web_fetch: bool = False,
    on_event: EventSink | None = None,
    timeout_s: float | None = None,
) -> LoopResult:
    history = list(messages)
    result = LoopResult(text="", messages=history, steps=0, stop_reason="end")
    failures: dict[str, int] = {}
    call_counts: dict[str, int] = {}
    pending_note = ""
    length_continuations = 0
    text_so_far: list[str] = []
    step_limit_announced = False
    declined = False

    while True:
        if cancel is not None:
            cancel.raise_if_cancelled()
        request = router.request(role, history, system=system, tools=tools, effort=effort, max_tokens=max_tokens,
                                 web_search_uses=web_search_uses, web_fetch=web_fetch, timeout_s=timeout_s)
        response: AIResponse = await _cancellable(router.complete(request), cancel)
        result.steps += 1
        result.usage = result.usage.add(response.usage)
        result.provider, result.model = response.provider, response.model
        result.fallback_model = response.fallback_model or result.fallback_model
        result.sources.extend(s for s in response.sources if all(s.url != x.url for x in result.sources))
        result.server_actions.extend(response.server_actions)
        history.append(response.message)
        for note in response.progress_notes:
            await _emit(on_event, "note", {"text": note})
        for action in response.server_actions:
            await _emit(on_event, "server_action", action.model_dump())
        if response.text:
            text_so_far.append(response.text)

        if response.stop_reason == "max_tokens" and not response.tool_calls:
            if length_continuations < MAX_LENGTH_CONTINUATIONS:
                length_continuations += 1
                history.append(AIMessage.user("Your reply was cut off by the length limit. "
                                              "Continue exactly where you stopped, without repeating."))
                continue
            result.stop_reason = "max_tokens"
            break

        if not response.tool_calls:
            result.stop_reason = "end"
            break

        if declined:
            # The user said no and the model still calls tools: answer them so the history stays valid, then stop.
            outputs = [ToolOutput(tool_call_id=c.id, name=c.name, content=DECLINED_CALL, is_error=True)
                       for c in response.tool_calls]
            history.append(AIMessage.results(outputs))
            result.stop_reason = "declined"
            break

        if result.steps >= max_steps and step_limit_announced:
            # The model ignored the step-limit note; answer its calls so the history stays valid, then stop.
            outputs = [ToolOutput(tool_call_id=c.id, name=c.name, content="Not executed: step limit reached.",
                                  is_error=True) for c in response.tool_calls]
            history.append(AIMessage.results(outputs))
            result.stop_reason = "max_steps"
            break

        cut_off = response.stop_reason == "max_tokens"
        outputs = await _run_calls(response.tool_calls, run_tool, read_only, cancel, cut_off, on_event)
        for call, out in zip(response.tool_calls, outputs, strict=True):
            result.tool_calls.append(call)
            result.tool_outputs.append(out)
            sig = _signature(call)
            call_counts[sig] = call_counts.get(sig, 0) + 1
            if out.is_error:
                key = sig + "|" + out.content[:200]
                failures[key] = failures.get(key, 0) + 1
                if failures[key] >= SAME_FAILURE_STOP:
                    result.stop_reason = "loop_guard"
                elif failures[key] >= SAME_FAILURE_WARN:
                    pending_note = REPEAT_NOTE.format(n=failures[key])
            if call_counts[sig] >= SAME_CALL_STOP:
                result.stop_reason = "loop_guard"

        note = pending_note
        pending_note = ""
        if result.stop_reason != "loop_guard" and result.steps >= max_steps - 1 and not step_limit_announced:
            note = (note + "\n\n" if note else "") + STEP_LIMIT_NOTE
            step_limit_announced = True
        if any(out.halt for out in outputs):
            declined = True
            note = DECLINED_NOTE
        history.append(AIMessage.results(outputs, note=note))
        if result.stop_reason == "loop_guard":
            await _emit(on_event, "loop_guard", {"calls": [c.name for c in response.tool_calls]})
            break

    final = text_so_far[-1] if text_so_far else ""
    if result.stop_reason == "max_tokens" and length_continuations:
        final = "".join(text_so_far[-(length_continuations + 1):])
    result.text = final.strip()
    return result


async def _run_calls(
    calls: list[ToolCall],
    run_tool: ToolRunner,
    read_only: Callable[[str], bool],
    cancel: CancelToken | None,
    cut_off: bool,
    on_event: EventSink | None,
) -> list[ToolOutput]:
    if cut_off:
        return [ToolOutput(tool_call_id=c.id, name=c.name, content=CUT_OFF_NOTE, is_error=True) for c in calls]

    async def one(call: ToolCall) -> ToolOutput:
        if cancel is not None:
            cancel.raise_if_cancelled()
        await _emit(on_event, "tool_call", {"id": call.id, "name": call.name, "input": call.input})
        out = await run_tool(call)
        out.tool_call_id = call.id
        out.name = out.name or call.name
        await _emit(on_event, "tool_result", {"id": call.id, "name": call.name, "is_error": out.is_error,
                                              "content": out.content[:500]})
        return out

    if len(calls) > 1 and all(read_only(c.name) for c in calls):
        try:
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(one(c)) for c in calls]
        except BaseExceptionGroup as eg:  # cancellation / kill switch from one call stops the others
            raise eg.exceptions[0] from None
        return [t.result() for t in tasks]
    outputs: list[ToolOutput] = []
    for call in calls:
        if any(out.halt for out in outputs):  # the user said no: the rest of this batch does not run either
            outputs.append(ToolOutput(tool_call_id=call.id, name=call.name, content=DECLINED_CALL, is_error=True))
        else:
            outputs.append(await one(call))
    return outputs
