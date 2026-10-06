"""Deterministic stand-ins for external services, used only by tests.

`ScriptedProvider` replays prepared model turns so agent / orchestrator logic can be tested
without network access or API keys. `FakeAnthropicClient` returns real SDK response objects,
so the Anthropic provider's request building and parsing are tested against the actual types.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from typing import Any

from jarvis.ai.base import AIProvider
from jarvis.ai.types import AIMessage, AIResponse, CompletionRequest, ToolCall, Usage

_ids = itertools.count(1)

Step = AIResponse | Callable[[CompletionRequest], AIResponse] | Exception


def reply(text: str, **kw: Any) -> AIResponse:
    return AIResponse(text=text, stop_reason="end", message=AIMessage(role="assistant", text=text),
                      usage=Usage(input_tokens=10, output_tokens=5), **kw)


def tool(name: str, input: dict[str, Any] | None = None, *, text: str = "", more: list[tuple[str, dict]] | None = None,
         stop: str = "tool_use") -> AIResponse:
    calls = [ToolCall(id=f"call_{next(_ids)}", name=name, input=input or {})]
    for n, i in more or []:
        calls.append(ToolCall(id=f"call_{next(_ids)}", name=n, input=i))
    return AIResponse(text=text, tool_calls=calls, stop_reason=stop,  # type: ignore[arg-type]
                      message=AIMessage(role="assistant", text=text, tool_calls=calls),
                      usage=Usage(input_tokens=10, output_tokens=5))


class ScriptedProvider(AIProvider):
    """Returns the next scripted step for every call; a callable step can inspect the request."""

    def __init__(self, steps: list[Step] | None = None, *, name: str = "scripted",
                 default: Callable[[CompletionRequest], AIResponse] | None = None) -> None:
        super().__init__()
        self.name = name
        self.steps: list[Step] = list(steps or [])
        self.default = default
        self.requests: list[CompletionRequest] = []
        self.is_available = True

    @property
    def model(self) -> str:
        return "scripted-model"

    def available(self) -> bool:
        return self.is_available

    def push(self, *steps: Step) -> None:
        self.steps.extend(steps)

    async def complete(self, request: CompletionRequest) -> AIResponse:
        self.requests.append(request.model_copy(deep=True))
        if self.steps:
            step = self.steps.pop(0)
        elif self.default is not None:
            step = self.default
        else:
            raise AssertionError(f"ScriptedProvider ran out of steps (role={request.role})")
        if isinstance(step, Exception):
            raise step
        response = step(request) if callable(step) else step.model_copy(deep=True)
        response.provider = self.name
        return self._account(response)


class _FakeMessages:
    def __init__(self, owner: FakeAnthropicClient) -> None:
        self.owner = owner

    async def create(self, **kwargs: Any) -> Any:
        import copy

        self.owner.calls.append(copy.deepcopy(kwargs))
        if not self.owner.queue:
            raise AssertionError("FakeAnthropicClient: no queued response")
        item = self.owner.queue.pop(0)
        if callable(item) and not isinstance(item, Exception):
            item = item(kwargs)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeBeta:
    def __init__(self, owner: FakeAnthropicClient) -> None:
        self.messages = _FakeMessages(owner)


class FakeAnthropicClient:
    """Mimics `AsyncAnthropic().beta.messages.create` and returns real `BetaMessage` objects."""

    def __init__(self, *responses: Any) -> None:
        self.queue: list[Any] = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.beta = _FakeBeta(self)

    async def close(self) -> None:
        return None


def beta_message(content: list[dict[str, Any]], stop_reason: str = "end_turn", *, stop_details: dict | None = None,
                 iterations: list[dict] | None = None, model: str = "claude-opus-5-5") -> Any:
    from anthropic.types.beta import BetaMessage

    usage: dict[str, Any] = {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 50,
                             "cache_creation_input_tokens": 0}
    if iterations is not None:
        usage["iterations"] = iterations
    data: dict[str, Any] = {
        "id": f"msg_{next(_ids)}", "type": "message", "role": "assistant", "model": model,
        "content": content, "stop_reason": stop_reason, "stop_sequence": None, "usage": usage,
    }
    if stop_details is not None:
        data["stop_details"] = stop_details
    return BetaMessage.model_validate(data)


def api_error(cls_name: str, status: int, message: str) -> Exception:
    import anthropic
    import httpx2

    response = httpx2.Response(status, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    cls = getattr(anthropic, cls_name)
    return cls(message, response=response,
               body={"type": "error", "error": {"type": "invalid_request_error", "message": message}})


class FakeHost:
    """In-memory HostAdapter: records every call instead of touching the desktop."""

    platform = "fake"

    def __init__(self, apps: dict[str, str] | None = None) -> None:
        from jarvis.host.base import InstalledApp

        self.calls: list[tuple] = []
        self.volume = 50
        self.brightness = 70
        self.muted = False
        self.clipboard = ""
        self.running: dict[str, list[str]] = {}
        self._apps = {
            name: InstalledApp(name=display, target=f"C:/Apps/{name}.exe", kind="exe", id=name,
                               process_names=(f"{name}.exe",))
            for name, display in (apps or {"chrome": "Google Chrome", "notepad": "Notepad",
                                           "calculator": "Calculator"}).items()
        }

    def special_folder(self, key: str):
        return None

    async def installed_apps(self, refresh: bool = False):
        return list(self._apps.values())

    async def find_app(self, name: str):
        from jarvis.host import catalog

        entry = catalog.find_app(name)
        key = entry.id if entry else name.lower()
        return self._apps.get(key)

    async def launch_app(self, app, args=()):
        from jarvis.host.base import LaunchResult

        self.calls.append(("launch", app.name, tuple(args)))
        self.running.setdefault(app.id or app.name, []).append(app.process_names[0] if app.process_names else app.name)
        return LaunchResult(ok=True, app=app.name, method="fake", pid=4242)

    async def open_url(self, url: str) -> None:
        self.calls.append(("open_url", url))

    async def open_path(self, path) -> None:
        self.calls.append(("open_path", str(path)))

    async def reveal(self, path) -> None:
        self.calls.append(("reveal", str(path)))

    async def close_app(self, name: str, *, force: bool = False) -> list[str]:
        from jarvis.host import catalog

        entry = catalog.find_app(name)
        key = entry.id if entry else name.lower()
        self.calls.append(("close", key, force))
        return self.running.pop(key, [])

    async def running_apps(self):
        return [{"name": k, "pid": 1, "memory_mb": 10.0} for k in self.running]

    async def get_volume(self):
        return self.volume

    async def set_volume(self, level: int):
        self.volume = level
        self.calls.append(("volume", level))
        return level

    async def change_volume(self, delta: int):
        return await self.set_volume(max(0, min(100, self.volume + delta)))

    async def set_mute(self, mute: bool) -> None:
        self.muted = mute
        self.calls.append(("mute", mute))

    async def get_brightness(self):
        return self.brightness

    async def set_brightness(self, level: int):
        self.brightness = level
        self.calls.append(("brightness", level))
        return level

    async def media(self, action: str) -> None:
        self.calls.append(("media", action))

    async def lock_screen(self) -> None:
        self.calls.append(("lock",))

    async def power(self, action: str, delay_s: int = 0) -> None:
        self.calls.append(("power", action, delay_s))

    async def cancel_power(self) -> None:
        self.calls.append(("cancel_power",))

    async def screenshot(self, path):
        import asyncio
        from pathlib import Path

        await asyncio.to_thread(Path(path).write_bytes, b"\x89PNG\r\n\x1a\n")
        self.calls.append(("screenshot", str(path)))
        return Path(path)

    async def notify(self, title: str, message: str) -> None:
        self.calls.append(("notify", title, message))

    async def get_clipboard(self) -> str:
        return self.clipboard

    async def set_clipboard(self, text: str) -> None:
        self.clipboard = text

    def shell_argv(self, command: str, shell: str | None = None) -> list[str]:
        import sys

        if sys.platform == "win32":
            return ["powershell", "-NoProfile", "-Command", command]
        return ["bash", "-c", command]

    @property
    def default_shell(self) -> str:
        return "bash"
