"""ProviderRouter: picks the model backend for each role and fails over between providers.

Roles (`chat`, `intent`, `planner`, `agent`, `verifier`, `synthesis`) carry their own effort
and token limits from `AISettings.roles`. With `provider = "auto"` the order is
Anthropic → xAI (Grok) → OpenAI → local model; a provider that is unreachable or overloaded is
skipped for that call. A refusal is not routed to another vendor: it is reported as it is.

While one of the five agents works (`jarvis.ai.context.agent_scope`), the provider chosen for it
in `ai.agent_providers` goes first and JARVIS's usual order follows as its fallback: when the
agent's provider fails for any reason but a refusal, the call moves on down that order.

Cloud AI costs money per request. With `ai.allow_paid` off (the default) Anthropic, xAI and OpenAI are never
used, whatever keys are saved: only the free model on this computer answers. With it on and
`ai.cloud_only_when_told` (the default) they are left out of every order unless the user allowed one for the
work in progress (`jarvis.ai.context.allow_provider`): then that one goes first and only free (local) models
follow it. `asks_first()` names the cloud AI that would do some work once the user says so.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel, ValidationError

from jarvis.ai.anthropic_provider import AnthropicProvider
from jarvis.ai.base import AIProvider
from jarvis.ai.context import allowed_provider, current_agent
from jarvis.ai.openai_provider import LocalModelProvider, OpenAIProvider, XaiProvider
from jarvis.ai.types import AIMessage, AIResponse, CompletionRequest, Effort, ToolSpec
from jarvis.common.enums import AgentId
from jarvis.common.errors import ProviderError, ProviderRefusal, ProviderUnavailable
from jarvis.config.settings import AISettings

if TYPE_CHECKING:
    from jarvis.metrics import Metrics

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)
AUTO_ORDER = ("anthropic", "xai", "openai", "local")
CLOUD = ("anthropic", "xai", "openai")          # paid per request: used only when the user says so
CLOUD_NAMES = {"anthropic": "Claude", "xai": "Grok", "openai": "ChatGPT"}
CLOUD_SAY_HE = {"anthropic": "בקלוד", "xai": "בגרוק", "openai": "בצ'אט ג'י-פי-טי"}   # "תשתמש בגרוק"
# Who makes a model, for a role whose model name is set while its provider is "auto".
_MODEL_FAMILIES = (("claude", "anthropic"), ("grok", "xai"), ("gpt", "openai"), ("chatgpt", "openai"),
                   ("o1", "openai"), ("o3", "openai"), ("o4", "openai"))
NO_PROVIDER = ("No AI provider is configured. Add an Anthropic, xAI (Grok) or OpenAI API key in Settings, "
               "or enable a local model.")
NO_FREE_MODEL = "No AI can answer: paid AI (Grok, Claude, ChatGPT) is off, and the free model on this computer {why}"


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
        self.metrics: Metrics | None = None      # how long each call takes (provider and role, never the text)

    @classmethod
    def from_settings(cls, settings: AISettings, secrets: Any | None) -> ProviderRouter:
        def reader(name: str) -> _KeyCache:
            return _KeyCache(lambda: secrets.get(name) if secrets is not None else None)

        anthropic_key, xai_key = reader("ANTHROPIC_API_KEY"), reader("XAI_API_KEY")
        openai_key = reader("OPENAI_API_KEY")
        router = cls(settings, [
            AnthropicProvider(settings.anthropic, anthropic_key),
            XaiProvider(settings.xai, xai_key),
            OpenAIProvider(settings.openai, openai_key),
            LocalModelProvider(settings.local),
        ])
        router._key_caches = [anthropic_key, xai_key, openai_key]
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
        """Available providers for a role, in the order they are tried.

        Inside an agent's work (`agent_scope`) that agent's provider comes first."""
        return self.chain_for(role, current_agent())

    def chain_for(self, role: str, agent: str | None) -> list[AIProvider]:
        """The order for a role as `agent` would get it (None: JARVIS's own calls), for the work in progress:
        a cloud AI only when the user allowed it (`allow_provider`), or always with `cloud_only_when_told` off."""
        if self.settings.provider == "none":
            return []
        order = self._preferred(role, agent)
        told = allowed_provider()
        if told in self.providers:
            order = [told, *(n for n in order if n != told and n not in CLOUD)]
        elif self.settings.cloud_only_when_told:
            order = [n for n in order if n not in CLOUD]
        return self._usable(order)

    def asks_first(self, role: str = "agent", agent: str | None = None) -> str | None:
        """The cloud AI that would do this work once the user says so; None when it can go ahead now, or when
        no AI could do it anyway."""
        if self.settings.provider == "none" or self.chain_for(role, agent):
            return None
        usable = self._usable(self._preferred(role, agent))
        return usable[0].name if usable and usable[0].name in CLOUD else None

    def _preferred(self, role: str, agent: str | None) -> list[str]:
        order = list(AUTO_ORDER)
        for preferred in (self._role_preference(role), self.agent_preference(agent)):
            if preferred in order:
                order.remove(preferred)
                order.insert(0, preferred)
        return order

    def _usable(self, order: Sequence[str]) -> list[AIProvider]:
        return [self.providers[n] for n in order
                if n in self.providers and not self.paid_off(n) and self.providers[n].available()]

    def paid_off(self, name: str) -> bool:
        """Whether `name` is a paid AI while paid AI is switched off (`ai.allow_paid`)."""
        return name in CLOUD and not self.settings.allow_paid

    def _role_preference(self, role: str) -> str | None:
        preferred = self.settings.role(role).provider
        if preferred in ("", "auto"):
            preferred = self.settings.provider
        return preferred if preferred in self.providers else None

    def agent_preference(self, agent: str | None) -> str | None:
        """The provider chosen for an agent in `ai.agent_providers`; None = JARVIS's usual order."""
        preferred = self.settings.agent_providers.get(agent, "auto") if agent else "auto"
        return preferred if preferred in self.providers else None

    def has(self, name: str) -> bool:
        """Whether provider `name` can be used: set up (has its key, or is switched on), and not a paid AI while
        paid AI is off."""
        provider = self.providers.get(name)
        return bool(provider and not self.paid_off(name) and provider.available())

    def active_name(self) -> str | None:
        chain = self.chain_for("agent", None)
        return chain[0].name if chain else None

    def agent_info(self, agent: str) -> dict[str, Any]:
        """Which provider and model an agent works with (for its card in the UI); `when_told`: only when the
        user says so."""
        chain = self.chain_for("agent", agent)
        told = self.asks_first("agent", agent) if not chain else None
        first = chain[0] if chain else self.providers.get(told) if told else None
        model = self.settings.role("agent").model
        if first is not None and not (model and self._model_owner("agent", model) == first.name):
            model = first.model
        return {"preferred": self.settings.agent_providers.get(agent, "auto"),
                "provider": first.name if first else None,
                "model": model if first else None,
                "when_told": told is not None}

    def status(self) -> dict[str, Any]:
        on = self.has
        anthropic = self.providers.get("anthropic")
        return {
            "active": self.active_name(),
            "when_told": self.asks_first("agent", None),     # the cloud AI JARVIS uses once the user says so
            "paid": self.settings.allow_paid,                # False: Grok, Claude and OpenAI are never called
            "anthropic": on("anthropic"),
            "xai": on("xai"),
            "openai": on("openai"),
            "local": on("local"),
            "fallbacks": bool(isinstance(anthropic, AnthropicProvider) and on("anthropic")
                              and anthropic.fallbacks_enabled),
            "agents": {a.value: self.agent_info(a.value)["provider"] for a in AgentId},
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
        agent = current_agent()
        chain = self.chain_for(request.role, agent)
        if provider is not None:
            chain = [p for p in chain if p.name == provider] or chain
        if not chain:
            told = self.asks_first(request.role, agent)
            if told is not None:
                name = CLOUD_NAMES[told]
                raise ProviderUnavailable(f"{name} is used only when you ask for it: say \"use {name}\" in the "
                                          "request.", details={"code": "ai_needs_ok", "provider": told})
            raise ProviderUnavailable(self.no_provider_text(), details={"code": "no_ai"})
        agents_choice = self.agent_preference(agent)
        owner = self._model_owner(request.role, request.model) if request.model else None
        last: ProviderError | None = None
        for index, p in enumerate(chain):
            req = request
            if request.model and p.name != owner:
                req = request.model_copy(update={"model": None})  # a role's model name belongs to one provider
            started = time.perf_counter()
            try:
                response = await p.complete(req)
            except ProviderError as exc:
                self._measure(p.name, request.role, started, ok=False)
                retry_elsewhere = (isinstance(exc, ProviderUnavailable) or exc.retryable
                                   or (p.name == agents_choice and not isinstance(exc, ProviderRefusal)))
                if not retry_elsewhere or index == len(chain) - 1:
                    raise
                last = exc
                self.failovers += 1
                log.warning("%s failed (%s); trying %s", p.name, exc.message, chain[index + 1].name)
                continue
            self._measure(p.name, request.role, started)
            self.last_provider = p.name
            return response
        raise last or ProviderUnavailable("No AI provider answered.")  # pragma: no cover

    def no_provider_text(self) -> str:
        """Why no AI can answer, and what fixes it."""
        if self.settings.allow_paid or self.settings.provider == "none":
            return NO_PROVIDER
        local = self.providers.get("local")
        server = getattr(local, "server", None)
        if not isinstance(local, LocalModelProvider) or not local.settings.enabled:
            why = "is switched off in Settings."
        elif server is None or server.problem() is None:
            why = "is not set up (run: jarvis chat install)." if server is None else "does not answer."
        else:
            why = f"is not ready: {server.problem()}"
        return NO_FREE_MODEL.format(why=why)

    def _measure(self, provider: str, role: str, started: float, *, ok: bool = True) -> None:
        if self.metrics is not None:
            self.metrics.record(f"ai.{provider}.{role}", (time.perf_counter() - started) * 1000, ok=ok)

    def _model_owner(self, role: str, model: str) -> str | None:
        """The provider a role's model name was chosen for: the role's provider, else the maker of that
        model, else the first provider in the role's own order."""
        preferred = self._role_preference(role)
        if preferred is not None:
            return preferred
        name = model.lower()
        for prefix, maker in _MODEL_FAMILIES:
            if name.startswith(prefix):
                return maker
        chain = self.chain_for(role, None)
        return chain[0].name if chain else None

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
