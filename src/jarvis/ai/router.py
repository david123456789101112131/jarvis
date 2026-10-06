"""ProviderRouter: picks the model backend for each role and fails over between providers.

Roles (`chat`, `intent`, `planner`, `agent`, `verifier`, `synthesis`) carry their own effort
and token limits from `AISettings.roles`. With `provider = "auto"` the order is
Anthropic → OpenAI → local model; a provider that is unreachable or overloaded is skipped
for that call. A refusal is not routed to another vendor: it is reported as it is.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from jarvis.ai.anthropic_provider import AnthropicProvider
from jarvis.ai.base import AIProvider
from jarvis.ai.openai_provider import LocalModelProvider, OpenAIProvider
from jarvis.ai.types import AIMessage, AIResponse, CompletionRequest, Effort, ToolSpec
from jarvis.common.errors import ProviderError, ProviderUnavailable
from jarvis.config.settings import AISettings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)
AUTO_ORDER = ("anthropic", "openai", "local")


class _KeyCache:
    """Reads an API key from the secret store at most every `ttl` seconds."""

    def __init__(self, read: Callable[[], str | None], ttl: float = 15.0) -> None:
        self._read = read
        self._ttl = ttl
        self._value: str | None = None
        self._at = -1e9

    def __call__(self) -> str | None:
        now = time.monotonic()
        if now - self._at > self._ttl:
            try:
                self._value = self._read()
            except Exception:  # pragma: no cover - keyring backend errors
                log.warning("Reading an API key failed", exc_info=True)
                self._value = None
            self._at = now
        return self._value

    def invalidate(self) -> None:
        self._at = -1e9


class ProviderRouter:
    def __init__(self, settings: AISettings, providers: Sequence[AIProvider]) -> None:
        self.settings = settings
        self.providers: dict[str, AIProvider] = {p.name: p for p in providers}
        self._key_caches: list[_KeyCache] = []
        self.last_provider: str | None = None
        self.failovers = 0

    @classmethod
    def from_settings(cls, settings: AISettings, secrets: Any | None) -> ProviderRouter:
        def reader(name: str) -> _KeyCache:
            return _KeyCache(lambda: secrets.get(name) if secrets is not None else None)

        anthropic_key, openai_key = reader("ANTHROPIC_API_KEY"), reader("OPENAI_API_KEY")
        router = cls(settings, [
            AnthropicProvider(settings.anthropic, anthropic_key),
            OpenAIProvider(settings.openai, openai_key),
            LocalModelProvider(settings.local),
        ])
        router._key_caches = [anthropic_key, openai_key]
        return router

    # -- status ------------------------------------------------------------------------------
    def refresh(self) -> None:
        """Call after an API key was added or removed."""
        for cache in self._key_caches:
            cache.invalidate()

    @property
    def available(self) -> bool:
        return bool(self.chain())

    def chain(self, role: str = "agent") -> list[AIProvider]:
        """Available providers for a role, in the order they are tried."""
        if self.settings.provider == "none":
            return []
        preferred = self.settings.role(role).provider
        if preferred in ("", "auto"):
            preferred = self.settings.provider
        order = list(AUTO_ORDER)
        if preferred in self.providers:
            order.remove(preferred)
            order.insert(0, preferred)
        return [self.providers[n] for n in order if n in self.providers and self.providers[n].available()]

    def active_name(self) -> str | None:
        chain = self.chain("agent")
        return chain[0].name if chain else None

    def status(self) -> dict[str, Any]:
        anthropic = self.providers.get("anthropic")
        return {
            "active": self.active_name(),
            "anthropic": bool(anthropic and anthropic.available()),
            "openai": bool(self.providers.get("openai") and self.providers["openai"].available()),
            "local": bool(self.providers.get("local") and self.providers["local"].available()),
            "fallbacks": bool(isinstance(anthropic, AnthropicProvider) and anthropic.available()
                              and anthropic.fallbacks_enabled),
        }

    def describe(self) -> list[dict[str, Any]]:
        return [p.describe() for p in self.providers.values()]

    async def aclose(self) -> None:
        for p in self.providers.values():
            await p.aclose()

    # -- calls -------------------------------------------------------------------------------
    def request(
        self,
        role: str,
        messages: list[AIMessage],
        *,
        system: str = "",
        tools: Sequence[ToolSpec] = (),
        json_schema: dict[str, Any] | None = None,
        schema_name: str = "result",
        web_search_uses: int = 0,
        web_fetch: bool = False,
        effort: Effort | None = None,
        max_tokens: int | None = None,
        timeout_s: float | None = None,
    ) -> CompletionRequest:
        cfg = self.settings.role(role)
        return CompletionRequest(
            messages=list(messages), system=system, tools=list(tools), json_schema=json_schema,
            schema_name=schema_name, web_search_uses=web_search_uses, web_fetch=web_fetch,
            effort=effort or cfg.effort, max_tokens=max_tokens or cfg.max_tokens, timeout_s=timeout_s,
            role=role, model=cfg.model or None,
        )

    async def complete(self, request: CompletionRequest, *, provider: str | None = None) -> AIResponse:
        chain = self.chain(request.role)
        if provider is not None:
            chain = [p for p in chain if p.name == provider] or chain
        if not chain:
            raise ProviderUnavailable(
                "No AI provider is configured. Add an Anthropic or OpenAI API key in Settings, "
                "or enable a local model."
            )
        last: ProviderError | None = None
        for index, p in enumerate(chain):
            req = request
            if request.model and index > 0:
                req = request.model_copy(update={"model": None})  # a role's model name belongs to its first provider
            try:
                response = await p.complete(req)
            except ProviderError as exc:
                retry_elsewhere = isinstance(exc, ProviderUnavailable) or exc.retryable
                if not retry_elsewhere or index == len(chain) - 1:
                    raise
                last = exc
                self.failovers += 1
                log.warning("%s failed (%s); trying %s", p.name, exc.message, chain[index + 1].name)
                continue
            self.last_provider = p.name
            return response
        raise last or ProviderUnavailable("No AI provider answered.")  # pragma: no cover

    async def ask(self, role: str, prompt: str, *, system: str = "", effort: Effort | None = None,
                  max_tokens: int | None = None) -> str:
        response = await self.complete(self.request(role, [AIMessage.user(prompt)], system=system, effort=effort,
                                                    max_tokens=max_tokens))
        return response.text

    async def structured(
        self,
        role: str,
        prompt: str | list[AIMessage],
        output: type[T],
        *,
        system: str = "",
        effort: Effort | None = None,
        max_tokens: int | None = None,
        attempts: int = 2,
    ) -> T:
        """Ask for a reply that validates as `output`; one corrective retry on invalid JSON."""
        messages = [AIMessage.user(prompt)] if isinstance(prompt, str) else list(prompt)
        schema = output.model_json_schema()
        error: str | None = None
        for _ in range(max(1, attempts)):
            req = self.request(role, messages, system=system, json_schema=schema, schema_name=output.__name__,
                               effort=effort, max_tokens=max_tokens)
            response = await self.complete(req)
            try:
                return output.model_validate(response.parse_json())
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                error = str(exc)[:800]
                messages = [*messages, response.message,
                            AIMessage.user(f"That reply was not valid for the required JSON schema: {error}\n"
                                           "Reply again with only the corrected JSON object.")]
        raise ProviderError(f"The model did not return valid {output.__name__} JSON: {error}")
