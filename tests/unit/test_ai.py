"""Stage 4 (AI layer): providers, router failover, structured output, and the agent tool loop."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel

from fakes import FakeAnthropicClient, ScriptedProvider, api_error, beta_message, reply, tool
from jarvis.ai import (
    AIMessage,
    AnthropicProvider,
    CompletionRequest,
    LocalModelProvider,
    OpenAIProvider,
    ProviderRouter,
    ToolOutput,
    ToolSpec,
    strict_schema,
)
from jarvis.ai.anthropic_provider import FALLBACK_BETA, UPDATES_BETA, sanitize_after_fallback
from jarvis.ai.loop import CUT_OFF_NOTE, STEP_LIMIT_NOTE, run_tool_loop
from jarvis.common.cancel import CancelToken
from jarvis.common.errors import ProviderError, ProviderRefusal, ProviderUnavailable, TaskCancelled
from jarvis.config.settings import AISettings, AnthropicSettings, LocalModelSettings, OpenAISettings

SPEC = ToolSpec(name="list_files", description="List files.",
                input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})


def anthropic(*responses: Any, **settings: Any) -> tuple[AnthropicProvider, FakeAnthropicClient]:
    client = FakeAnthropicClient(*responses)
    provider = AnthropicProvider(AnthropicSettings(**settings), lambda: "sk-ant-test-key",
                                 client_factory=lambda key: client)
    return provider, client


def req(**kw: Any) -> CompletionRequest:
    kw.setdefault("messages", [AIMessage.user("hello")])
    return CompletionRequest(**kw)


class TestAnthropicRequest:
    async def test_request_shape(self) -> None:
        p, client = anthropic(beta_message([{"type": "text", "text": "hi"}]))
        res = await p.complete(req(system="You are JARVIS.", tools=[SPEC], effort="high", max_tokens=4000,
                                   web_search_uses=5))
        assert res.text == "hi" and res.stop_reason == "end"
        sent = client.calls[0]
        assert sent["model"] == "claude-opus-5-5"
        assert sent["thinking"] == {"type": "adaptive", "display": "updates"}
        assert sent["output_config"] == {"effort": "high"}
        assert sent["fallbacks"] == "default"
        assert set(sent["betas"]) == {FALLBACK_BETA, UPDATES_BETA}
        assert sent["system"] == [{"type": "text", "text": "You are JARVIS.", "cache_control": {"type": "ephemeral"}}]
        assert sent["cache_control"] == {"type": "ephemeral"}
        assert "tool_choice" not in sent and "temperature" not in sent
        names = [t.get("name") for t in sent["tools"]]
        assert names == ["list_files", "web_search"]
        assert sent["tools"][1] == {"type": "web_search_20260209", "name": "web_search", "max_uses": 5}

    async def test_structured_output_drops_web_tools(self) -> None:
        p, client = anthropic(beta_message([{"type": "text", "text": '{"a": 1}'}]))

        class Out(BaseModel):
            a: int
            note: str = ""

        res = await p.complete(req(json_schema=Out.model_json_schema(), web_search_uses=3))
        assert res.parse_json() == {"a": 1}
        fmt = client.calls[0]["output_config"]["format"]
        assert fmt["type"] == "json_schema"
        assert fmt["schema"]["additionalProperties"] is False
        assert fmt["schema"]["required"] == ["a", "note"]
        assert "tools" not in client.calls[0]

    async def test_settings_can_turn_features_off(self) -> None:
        p, client = anthropic(beta_message([{"type": "text", "text": "x"}]), fallbacks=False,
                              progress_updates=False, prompt_caching=False)
        await p.complete(req(system="s"))
        sent = client.calls[0]
        assert "fallbacks" not in sent and "betas" not in sent and "cache_control" not in sent
        assert sent["thinking"] == {"type": "adaptive"}


class TestAnthropicResponse:
    async def test_parse_blocks_and_replay_exactly(self) -> None:
        content = [
            {"type": "thinking", "thinking": "Checking the folder first.", "signature": "sig1"},
            {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "jarvis"}},
            {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": [
                {"type": "web_search_result", "url": "https://example.com/a", "title": "A",
                 "encrypted_content": "xyz", "page_age": None}]},
            {"type": "text", "text": "Found it. "},
            {"type": "tool_use", "id": "toolu_1", "name": "list_files", "input": {"path": "."}},
        ]
        p, client = anthropic(beta_message(content, "tool_use"), beta_message([{"type": "text", "text": "done"}]))
        res = await p.complete(req(tools=[SPEC]))
        assert res.stop_reason == "tool_use"
        assert [c.name for c in res.tool_calls] == ["list_files"]
        assert res.progress_notes == ["Checking the folder first."]
        assert res.sources[0].url == "https://example.com/a"
        assert res.server_actions[0].input == {"query": "jarvis"}
        history = [AIMessage.user("hello"), res.message,
                   AIMessage.results([ToolOutput(tool_call_id="toolu_1", content="a.txt")], note="reminder")]
        await p.complete(req(messages=history, tools=[SPEC]))
        sent = client.calls[1]["messages"]
        assert sent[1] == {"role": "assistant", "content": res.message.native["content"]}
        assert sent[1]["content"][0]["signature"] == "sig1"
        assert sent[2]["content"] == [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "a.txt"},
                                      {"type": "text", "text": "reminder"}]

    async def test_pause_turn_is_continued(self) -> None:
        first = beta_message([
            {"type": "server_tool_use", "id": "srvtoolu_9", "name": "web_search", "input": {"query": "x"}},
            {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_9", "content": []},
        ], "pause_turn")
        second = beta_message([{"type": "text", "text": "answer"}])
        p, client = anthropic(first, second)
        res = await p.complete(req())
        assert res.text == "answer" and res.continuations == 1
        resent = client.calls[1]["messages"]
        assert resent[-1]["role"] == "assistant" and resent[-1]["content"][0]["id"] == "srvtoolu_9"
        assert [b["type"] for b in res.message.native["content"]] == ["server_tool_use", "web_search_tool_result",
                                                                      "text"]

    async def test_refusal_raises(self) -> None:
        p, _ = anthropic(beta_message([], "refusal", stop_details={"type": "refusal", "category": "cyber",
                                                                    "explanation": "nope"}))
        with pytest.raises(ProviderRefusal) as info:
            await p.complete(req())
        assert info.value.details["category"] == "cyber"

    async def test_server_side_fallback_is_detected(self) -> None:
        msg = beta_message([{"type": "text", "text": "ok"}], iterations=[
            {"type": "message", "input_tokens": 10, "output_tokens": 0, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0},
            {"type": "fallback_message", "model": "claude-opus-4-8", "input_tokens": 10, "output_tokens": 3,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        ])
        p, _ = anthropic(msg)
        res = await p.complete(req())
        assert res.fallback_model == "claude-opus-4-8" and p.fallbacks_served == 1

    def test_echo_rules_after_mid_output_fallback(self) -> None:
        content = [
            {"type": "thinking", "thinking": "", "signature": "s"},
            {"type": "text", "text": "partial"},
            {"type": "tool_use", "id": "toolu_x", "name": "list_files", "input": {}},
            {"type": "server_tool_use", "id": "srv_lonely", "name": "web_search", "input": {}},
            {"type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-4-8"},
             "trigger": {"type": "refusal"}},
            {"type": "text", "text": "rest"},
        ]
        kept = sanitize_after_fallback(content)
        assert [b["type"] for b in kept] == ["text", "fallback", "text"]


class TestAnthropicErrors:
    async def test_optional_feature_is_dropped_after_400(self) -> None:
        p, client = anthropic(api_error("BadRequestError", 400, "fallbacks: unsupported for this account"),
                              beta_message([{"type": "text", "text": "ok"}]))
        res = await p.complete(req())
        assert res.text == "ok"
        assert "fallbacks" not in client.calls[1] and FALLBACK_BETA not in client.calls[1].get("betas", [])
        assert p.fallbacks_enabled is False

    async def test_other_400_is_not_retried(self) -> None:
        p, client = anthropic(api_error("BadRequestError", 400, "messages: roles must alternate"))
        with pytest.raises(ProviderError) as info:
            await p.complete(req())
        assert not info.value.retryable and len(client.calls) == 1

    async def test_auth_rate_limit_and_missing_key(self) -> None:
        p, _ = anthropic(api_error("AuthenticationError", 401, "invalid x-api-key"))
        with pytest.raises(ProviderUnavailable):
            await p.complete(req())
        p, _ = anthropic(api_error("RateLimitError", 429, "slow down"))
        with pytest.raises(ProviderError) as info:
            await p.complete(req())
        assert info.value.retryable
        nokey = AnthropicProvider(AnthropicSettings(), lambda: None)
        assert not nokey.available()
        with pytest.raises(ProviderUnavailable):
            await nokey.complete(req())


class _FakeOpenAICompletions:
    def __init__(self, owner: _FakeOpenAI) -> None:
        self.owner = owner

    async def create(self, **kwargs: Any) -> Any:
        from openai.types.chat import ChatCompletion

        self.owner.calls.append(kwargs)
        return ChatCompletion.model_validate(self.owner.queue.pop(0))


class _FakeOpenAI:
    def __init__(self, *payloads: dict) -> None:
        self.queue = list(payloads)
        self.calls: list[dict] = []
        self.chat = type("Chat", (), {})()
        self.chat.completions = _FakeOpenAICompletions(self)


def completion(content: str | None = None, tool_calls: list[dict] | None = None, finish: str = "stop") -> dict:
    return {"id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": "gpt-5",
            "choices": [{"index": 0, "finish_reason": finish,
                         "message": {"role": "assistant", "content": content, "tool_calls": tool_calls}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}}


class TestOpenAICompatible:
    async def test_openai_conversion_and_tool_calls(self) -> None:
        fake = _FakeOpenAI(completion(None, [{"id": "call_1", "type": "function",
                                              "function": {"name": "list_files", "arguments": '{"path": "C:/"}'}}],
                                      "tool_calls"))
        p = OpenAIProvider(OpenAISettings(), lambda: "sk-openai", client_factory=lambda k: fake)
        history = [AIMessage.user("hi"),
                   AIMessage(role="assistant", text="", tool_calls=[tool("list_files", {"path": "."}).tool_calls[0]]),
                   AIMessage.results([ToolOutput(tool_call_id="call_0", content="boom", is_error=True)], note="go on")]
        res = await p.complete(req(messages=history, system="sys", tools=[SPEC], effort="max"))
        assert res.stop_reason == "tool_use" and res.tool_calls[0].input == {"path": "C:/"}
        sent = fake.calls[0]
        assert [m["role"] for m in sent["messages"]] == ["system", "user", "assistant", "tool", "user"]
        assert sent["messages"][3]["content"].startswith("ERROR:")
        assert sent["reasoning_effort"] == "high" and sent["max_completion_tokens"] == 16000
        assert sent["tools"][0]["function"]["name"] == "list_files"

    async def test_openai_strict_json_and_local_json_mode(self) -> None:
        fake = _FakeOpenAI(completion('{"a": 1}'))
        p = OpenAIProvider(OpenAISettings(model="gpt-4.1"), lambda: "k", client_factory=lambda k: fake)
        schema = {"type": "object", "properties": {"a": {"type": "integer", "minimum": 0}}}
        await p.complete(req(json_schema=schema))
        rf = fake.calls[0]["response_format"]
        assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
        assert "minimum" not in rf["json_schema"]["schema"]["properties"]["a"]
        assert "reasoning_effort" not in fake.calls[0]

        local_fake = _FakeOpenAI(completion('{"a": 2}'))
        local = LocalModelProvider(LocalModelSettings(enabled=True), client_factory=lambda k: local_fake)
        res = await local.complete(req(json_schema=schema))
        assert res.parse_json() == {"a": 2}
        sent = local_fake.calls[0]
        assert sent["response_format"] == {"type": "json_object"} and "max_tokens" in sent
        assert "JSON schema" in sent["messages"][0]["content"]
        assert not LocalModelProvider(LocalModelSettings(enabled=False)).available()


def router_with(*providers: ScriptedProvider, **settings: Any) -> ProviderRouter:
    for p in providers:
        assert p.name in ("anthropic", "openai", "local")
    return ProviderRouter(AISettings(**settings), list(providers))


class TestRouter:
    async def test_order_and_failover(self) -> None:
        a = ScriptedProvider([ProviderUnavailable("network down", retryable=True)], name="anthropic")
        o = ScriptedProvider([reply("from openai")], name="openai")
        router = router_with(a, o)
        assert [p.name for p in router.chain()] == ["anthropic", "openai"]
        res = await router.complete(router.request("chat", [AIMessage.user("hi")]))
        assert res.text == "from openai" and router.failovers == 1 and router.last_provider == "openai"
        assert a.requests[0].effort == "low"  # chat role effort

    async def test_non_retryable_errors_do_not_fail_over(self) -> None:
        a = ScriptedProvider([ProviderError("bad request")], name="anthropic")
        o = ScriptedProvider([reply("x")], name="openai")
        with pytest.raises(ProviderError):
            await router_with(a, o).complete(router_with(a, o).request("agent", [AIMessage.user("hi")]))
        assert not o.requests

    async def test_role_preference_and_none(self) -> None:
        a = ScriptedProvider([], name="anthropic")
        o = ScriptedProvider([reply("x")], name="openai")
        settings = AISettings()
        settings.roles["planner"].provider = "openai"
        router = ProviderRouter(settings, [a, o])
        assert router.chain("planner")[0].name == "openai"
        assert router.chain("agent")[0].name == "anthropic"
        router.settings.provider = "none"
        with pytest.raises(ProviderUnavailable):
            await router.complete(router.request("agent", [AIMessage.user("x")]))

    async def test_structured_with_correction(self) -> None:
        class Plan(BaseModel):
            steps: list[str]

        a = ScriptedProvider([reply("not json"), reply('{"steps": ["a", "b"]}')], name="anthropic")
        plan = await router_with(a).structured("planner", "make a plan", Plan)
        assert plan.steps == ["a", "b"]
        assert len(a.requests[1].messages) == 3 and a.requests[1].json_schema is not None

    def test_status_reports_fallbacks(self) -> None:
        router = ProviderRouter.from_settings(AISettings(), None)
        assert router.status() == {"active": None, "anthropic": False, "openai": False, "local": False,
                                   "fallbacks": False}


class TestStrictSchema:
    def test_keeps_property_named_title_and_closes_objects(self) -> None:
        class Inner(BaseModel):
            title: str
            score: int = 0

        class Outer(BaseModel):
            items: list[Inner]
            name: str = "x"

        s = strict_schema(Outer.model_json_schema())
        inner = s["$defs"]["Inner"]
        assert set(inner["properties"]) == {"title", "score"}
        assert inner["additionalProperties"] is False and inner["required"] == ["title", "score"]
        assert "default" not in s["properties"]["name"]


class TestToolLoop:
    async def _loop(self, provider: ScriptedProvider, run_tool, **kw: Any):
        router = router_with(provider)
        return await run_tool_loop(router, role="agent", system="sys", messages=[AIMessage.user("task")],
                                   tools=[SPEC], run_tool=run_tool, **kw)

    async def test_runs_tools_until_final_answer(self) -> None:
        seen: list[dict] = []

        async def run(call):
            seen.append(call.input)
            return ToolOutput(tool_call_id=call.id, content="a.txt, b.txt")

        events: list[str] = []
        p = ScriptedProvider([tool("list_files", {"path": "."}), reply("There are 2 files.")], name="anthropic")
        res = await self._loop(p, run, on_event=lambda k, d: events.append(k))
        assert res.completed and res.text == "There are 2 files." and res.steps == 2
        assert seen == [{"path": "."}]
        roles = [m.role for m in res.messages]
        assert roles == ["user", "assistant", "user", "assistant"]
        assert res.messages[2].tool_results[0].content == "a.txt, b.txt"
        assert events == ["tool_call", "tool_result"]

    async def test_parallel_read_only_calls(self) -> None:
        running = 0
        peak = 0

        async def run(call):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.05)
            running -= 1
            return ToolOutput(tool_call_id=call.id, content=call.input["path"])

        p = ScriptedProvider([tool("list_files", {"path": "a"}, more=[("list_files", {"path": "b"})]), reply("ok")],
                             name="anthropic")
        res = await self._loop(p, run, read_only=lambda n: True)
        assert peak == 2
        assert [o.content for o in res.messages[2].tool_results] == ["a", "b"]

    async def test_step_limit(self) -> None:
        async def run(call):
            return ToolOutput(tool_call_id=call.id, content="more")

        p = ScriptedProvider(default=lambda r: tool("list_files", {"path": str(len(r.messages))}), name="anthropic")
        res = await self._loop(p, run, max_steps=4)
        assert res.stop_reason == "max_steps" and res.steps == 4
        assert STEP_LIMIT_NOTE in res.messages[-3].text
        last = res.messages[-1]
        assert last.role == "user" and all(o.is_error for o in last.tool_results)

    async def test_repeated_failure_guard(self) -> None:
        async def run(call):
            return ToolOutput(tool_call_id=call.id, content="Access denied", is_error=True)

        p = ScriptedProvider(default=lambda r: tool("list_files", {"path": "C:/secret"}), name="anthropic")
        res = await self._loop(p, run)
        assert res.stop_reason == "loop_guard" and res.steps == 5
        assert "failed 3 times" in res.messages[6].text

    async def test_cut_off_calls_are_not_run(self) -> None:
        ran: list[str] = []

        async def run(call):
            ran.append(call.name)
            return ToolOutput(tool_call_id=call.id, content="x")

        p = ScriptedProvider([tool("list_files", {"path": "."}, stop="max_tokens"), reply("fine")], name="anthropic")
        res = await self._loop(p, run)
        assert ran == [] and res.messages[2].tool_results[0].content == CUT_OFF_NOTE

    async def test_length_continuation(self) -> None:
        cut = reply("Part one, ")
        cut.stop_reason = "max_tokens"
        p = ScriptedProvider([cut, reply("part two.")], name="anthropic")
        res = await self._loop(p, None)
        assert res.completed and res.text == "part two."
        assert "cut off" in res.messages[2].text

    async def test_cancel_during_model_call(self) -> None:
        token = CancelToken()

        class Slow(ScriptedProvider):
            async def complete(self, request):
                await asyncio.sleep(10)

        p = Slow([], name="anthropic")
        task = asyncio.create_task(self._loop(p, None, cancel=token))
        await asyncio.sleep(0.05)
        token.cancel("user stop")
        with pytest.raises(TaskCancelled):
            await task
