"""Shared test setup: the five agents and the Agent Manager on the real bus, permission gate and tools.

Model calls use `ScriptedProvider`; the desktop is `FakeHost`; web services are `httpx.MockTransport`.
Everything else (files, projects, Git, memory) is real.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

import jarvis.agents.manager as manager_module
from fakes import FakeHost, ScriptedProvider
from jarvis.agents import (
    AgentManager,
    AgentResult,
    AgentRuntime,
    AgentTask,
    BaseAgent,
    CreativeAgent,
    DevelopmentAgent,
    PlannerAgent,
    ResearchAgent,
    RunContext,
    SystemAgent,
    skill,
)
from jarvis.ai.router import ProviderRouter
from jarvis.common.enums import AgentId
from jarvis.config.settings import AISettings
from jarvis.memory import create_memory
from jarvis.tools.builtin import create_registry


class ProbeSystem(SystemAgent):
    """The real System Agent plus test skills that block, crash or note the order they ran in."""

    @skill(description="test: wait for the gate (stubborn = ignore the cancel token)")
    async def hold(self, task: AgentTask, run: RunContext, stubborn: bool = False) -> AgentResult:
        gate: asyncio.Event = self.runtime.extras.setdefault("gate", asyncio.Event())
        if stubborn:
            await asyncio.sleep(30)
        else:
            await run.guard(asyncio.wait_for(gate.wait(), 30))
        return run.result(summary="released")

    @skill(description="test: note the order assignments run in")
    async def note(self, task: AgentTask, run: RunContext) -> AgentResult:
        self.runtime.extras.setdefault("order", []).append(task.title)
        return run.result(summary=f"noted {task.title}")

    @skill(description="test: crash")
    async def crash(self, task: AgentTask, run: RunContext) -> AgentResult:
        raise RuntimeError("boom")


async def until(check: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


class Env:
    def __init__(self, kit: Any, runtime: AgentRuntime, manager: AgentManager, memory: Any, host: FakeHost) -> None:
        self.kit, self.runtime, self.manager, self.memory, self.host = kit, runtime, manager, memory, host

    def task(self, agent: str, instruction: str = "", **kw: Any) -> AgentTask:
        return AgentTask(agent=AgentId.parse(agent), instruction=instruction or kw.get("skill") or "test", **kw)

    async def run(self, agent: str, instruction: str = "", **kw: Any) -> AgentResult:
        return await asyncio.wait_for(self.manager.run(self.task(agent, instruction, **kw)), 120)

    async def pending(self) -> Any:
        await until(lambda: bool(self.kit.broker.pending()))
        return self.kit.broker.pending()[0]

    def use_ai(self, *steps: Any, default: Any = None, when_told: bool = False) -> ScriptedProvider:
        """A scripted model as Claude, with paid AI on. It answers freely, as if the user had said so;
        `when_told=True` keeps the default rule that a cloud AI needs the user's word (`ai.cloud_only_when_told`)."""
        provider = ScriptedProvider(list(steps), name="anthropic", default=default)
        self.runtime.router = ProviderRouter(AISettings(allow_paid=True, cloud_only_when_told=when_told), [provider])
        return provider

    def mock_http(self, handler: Callable[[httpx.Request], httpx.Response]) -> list[httpx.Request]:
        seen: list[httpx.Request] = []

        def wrapped(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        self.kit.services._http = httpx.AsyncClient(transport=httpx.MockTransport(wrapped), follow_redirects=True)
        return seen


def make_runtime(kit: Any, memory: Any = None) -> AgentRuntime:
    return AgentRuntime(settings=kit.settings, executor=kit.executor, registry=kit.registry,
                        permissions=kit.permissions, bus=kit.bus, paths=kit.paths, memory=memory, activity=kit.activity)


AgentFactory = Callable[[AgentRuntime], list[BaseAgent]]


def default_agents(runtime: AgentRuntime) -> list[BaseAgent]:
    return [ResearchAgent(runtime), ProbeSystem(runtime), DevelopmentAgent(runtime), CreativeAgent(runtime),
            PlannerAgent(runtime)]


@contextlib.asynccontextmanager
async def agent_env(kit: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                    agents: AgentFactory = default_agents) -> AsyncIterator[Env]:
    monkeypatch.setattr(manager_module, "GRACE_S", 0.5)
    monkeypatch.setattr(manager_module, "COMPLETED_HOLD_S", 0.2)
    monkeypatch.setattr(manager_module, "ERROR_HOLD_S", 0.2)
    kit.registry = create_registry()
    kit.executor.registry = kit.registry
    host = FakeHost()
    kit.services.host = host
    memory = create_memory(tmp_path / "memory.db")
    await memory.start()
    kit.services.memory = memory
    kit.settings.agents.concurrency["system"] = 1      # one regular slot (+ the CRITICAL one) keeps queues visible
    runtime = make_runtime(kit, memory)
    manager = AgentManager(runtime, kit.kill_switch, broker=kit.broker, agents=agents(runtime))
    await manager.start()
    try:
        yield Env(kit, runtime, manager, memory, host)
    finally:
        gate = runtime.extras.get("gate")
        if gate is not None:
            gate.set()
        await manager.shutdown()
        await asyncio.to_thread(kit.services.processes.kill)
        await kit.services.aclose()
        await memory.close()
