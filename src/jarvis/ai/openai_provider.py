"""OpenAI, xAI (Grok) and OpenAI-compatible local models (Ollama, LM Studio, llama.cpp server).

All of them use the Chat Completions API, which xAI and every OpenAI-compatible local server implement.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from jarvis.ai.base import AIProvider
from jarvis.ai.types import AIMessage, AIResponse, CompletionRequest, Refusal, ToolCall, Usage, strict_schema
from jarvis.common.errors import ProviderError, ProviderRefusal, ProviderUnavailable
from jarvis.config.settings import LocalModelSettings, OpenAISettings, XaiSettings
from jarvis.security.redaction import default_redactor

if TYPE_CHECKING:
    from jarvis.ai.local_server import ChatServer

log = logging.getLogger(__name__)

_REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")
_EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}
_XAI_EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "xhigh"}
_FINISH = {"stop": "end", "tool_calls": "tool_use", "function_call": "tool_use", "length": "max_tokens",
           "content_filter": "refusal"}


def _said(exc: Exception) -> str:
    """The provider's own reason from an error response, short and with secrets removed (" " + text, or "").

    The SDK keeps the JSON "error" field as the body: xAI's is the text itself, OpenAI's an object."""
    reason: Any = getattr(exc, "body", None)
    if isinstance(reason, dict):
        reason = reason.get("error") or reason.get("message")
        if isinstance(reason, dict):
            reason = reason.get("message")
    if not isinstance(reason, str) or not reason.strip():
        return ""
    return " " + default_redactor().redact_text(" ".join(reason.split()))[:300]


class OpenAICompatibleProvider(AIProvider):
    name = "openai"
    supports_server_web = False
    #: "strict" = response_format json_schema; "json_object" = JSON mode + schema in the prompt.
    json_mode = "strict"
    max_tokens_param = "max_completion_tokens"

    def __init__(self, *, model: str, base_url: str | None, api_key: Callable[[], str | None],
                 timeout_s: float, max_retries: int = 2, client_factory: Callable[[str], Any] | None = None) -> None:
        super().__init__()
        self._model = model
        self._base_url = base_url or None
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._client_factory = client_factory
        self._client: Any = None
        self._fingerprint: str | None = None

    @property
    def model(self) -> str:
        return self._model

    def available(self) -> bool:
        return bool(self._api_key())

    def _get_client(self) -> Any:
        key = self._api_key()
        if not key:
            raise ProviderUnavailable(f"{self.name}: no API key configured.")
        fingerprint = hashlib.sha256(f"{key}|{self._base_url}".encode()).hexdigest()
        if self._client is None or fingerprint != self._fingerprint:
            if self._client_factory is not None:
                self._client = self._client_factory(key)
            else:
                import openai

                self._client = openai.AsyncOpenAI(api_key=key, base_url=self._base_url, timeout=self._timeout_s,
                                                  max_retries=self._max_retries)
            self._fingerprint = fingerprint
        return self._client

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None and hasattr(client, "close"):
            try:
                await client.close()
            except Exception:  # pragma: no cover
                log.debug("closing client failed", exc_info=True)

    # -- request building ----------------------------------------------------------------------
    def json_mode_for(self, model: str) -> str:
        return self.json_mode

    def max_tokens_param_for(self, model: str) -> str:
        return self.max_tokens_param

    def build_messages(self, req: CompletionRequest) -> list[dict[str, Any]]:
        system = req.system
        if req.json_schema is not None and self.json_mode_for(req.model or self.model) == "json_object":
            system = (system + "\n\n" if system else "") + (
                "Reply with one JSON object only (no prose, no code fences) that follows this JSON schema:\n"
                + json.dumps(strict_schema(req.json_schema), ensure_ascii=False)
            )
        out: list[dict[str, Any]] = [{"role": "system", "content": system}] if system else []
        for m in req.messages:
            if m.role == "assistant":
                entry: dict[str, Any] = {"role": "assistant", "content": m.text or None}
                if m.tool_calls:
                    entry["tool_calls"] = [
                        {"id": c.id, "type": "function",
                         "function": {"name": c.name, "arguments": json.dumps(c.input, ensure_ascii=False)}}
                        for c in m.tool_calls
                    ]
                if entry["content"] is None and "tool_calls" not in entry:
                    continue
                out.append(entry)
            else:
                for r in m.tool_results:
                    content = r.content or "(empty)"
                    out.append({"role": "tool", "tool_call_id": r.tool_call_id,
                                "content": f"ERROR: {content}" if r.is_error else content})
                if m.text or not m.tool_results:
                    out.append({"role": "user", "content": m.text or "(empty)"})
        return out

    def build_request(self, req: CompletionRequest) -> dict[str, Any]:
        model = req.model or self.model
        kwargs: dict[str, Any] = {"model": model, "messages": self.build_messages(req)}
        kwargs[self.max_tokens_param_for(model)] = req.max_tokens
        if req.tools:
            kwargs["tools"] = [
                {"type": "function",
                 "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}}
                for t in req.tools
            ]
        if req.json_schema is not None:
            if self.json_mode_for(model) == "strict":
                kwargs["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": req.schema_name or "result", "schema": strict_schema(req.json_schema),
                                    "strict": True},
                }
            else:
                kwargs["response_format"] = {"type": "json_object"}
        if model.startswith(_REASONING_PREFIXES):
            kwargs["reasoning_effort"] = _EFFORT.get(req.effort, "medium")
        if req.timeout_s:
            kwargs["timeout"] = req.timeout_s
        return kwargs

    # -- call ------------------------------------------------------------------------------------
    async def complete(self, request: CompletionRequest) -> AIResponse:
        client = self._get_client()
        kwargs = self.build_request(request)
        try:
            completion = await client.chat.completions.create(**kwargs)
        except ProviderError:
            raise
        except Exception as exc:
            err = self._map_error(exc)
            self.errors += 1
            self.last_error = err.message
            raise err from exc
        response = self._to_response(completion)
        if response.stop_reason == "refusal":
            self.errors += 1
            raise ProviderRefusal(f"{self.name} declined this request.", details={"provider": self.name})
        self.last_error = None
        return self._account(response)

    def _map_error(self, exc: Exception) -> ProviderError:
        try:
            import openai
        except ImportError:  # pragma: no cover
            return ProviderError(str(exc))
        if isinstance(exc, openai.AuthenticationError):
            return ProviderUnavailable(f"{self.name} rejected the API key (401).{_said(exc)}", details={"status": 401})
        if isinstance(exc, openai.PermissionDeniedError):
            # e.g. xAI: "Your newly created team doesn't have any credits or licenses yet." Say why, not just 403.
            return ProviderUnavailable(f"{self.name} denied access (403).{_said(exc)}", details={"status": 403})
        if isinstance(exc, openai.NotFoundError):
            return ProviderError(f"{self.name}: model {self.model!r} not found (404).", details={"status": 404})
        if isinstance(exc, openai.RateLimitError):
            return ProviderError(f"{self.name} rate limit reached (429).", retryable=True, details={"status": 429})
        if isinstance(exc, openai.BadRequestError):
            return ProviderError(f"{self.name} rejected the request (400): {str(exc)[:400]}", details={"status": 400})
        if isinstance(exc, openai.APITimeoutError):
            return ProviderError(f"{self.name} request timed out.", retryable=True, details={"status": "timeout"})
        if isinstance(exc, openai.APIConnectionError):
            return ProviderUnavailable(f"Cannot reach {self.name} ({self._base_url or 'api.openai.com'}).",
                                       retryable=True, details={"status": "connection"})
        if isinstance(exc, openai.APIStatusError):
            status = getattr(exc, "status_code", 0) or 0
            return ProviderError(f"{self.name} API error ({status}).", retryable=status >= 500,
                                 details={"status": status})
        if isinstance(exc, UnicodeEncodeError):
            # Only plain ASCII can go in the request headers: a key typed with the keyboard in Hebrew ends up here.
            return ProviderUnavailable(f"{self.name}: the saved API key has characters keys never contain; enter it "
                                       "again in Settings > API keys.", details={"status": "bad_key"})
        return ProviderError(f"{self.name} failed: {type(exc).__name__}: {exc}")

    def _to_response(self, completion: Any) -> AIResponse:
        choice = completion.choices[0]
        message = choice.message
        text = (message.content or "").strip()
        calls: list[ToolCall] = []
        for tc in getattr(message, "tool_calls", None) or []:
            fn = getattr(tc, "function", None)
            if fn is None:
                continue
            try:
                args = json.loads(fn.arguments or "{}")
                if not isinstance(args, dict):
                    args = {"value": args}
            except json.JSONDecodeError:
                args = {"_raw_arguments": fn.arguments}
            calls.append(ToolCall(id=tc.id, name=fn.name, input=args))
        stop = _FINISH.get(str(choice.finish_reason), "end")
        if calls and stop == "end":
            stop = "tool_use"
        refusal_text = getattr(message, "refusal", None)
        if refusal_text:
            stop = "refusal"
        usage = getattr(completion, "usage", None)
        cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        output = int(getattr(usage, "completion_tokens", 0) or 0)
        thinking = int(getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", 0) or 0)
        total = int(getattr(usage, "total_tokens", 0) or 0)
        # xAI counts the thinking apart from the answer (total = prompt + answer + thinking), OpenAI inside it;
        # both bill it as output, so output_tokens always includes it.
        if thinking and total >= prompt + output + thinking:
            output += thinking
        return AIResponse(
            text=text,
            tool_calls=calls,
            stop_reason=stop,  # type: ignore[arg-type]
            message=AIMessage(role="assistant", text=text, tool_calls=calls),
            usage=Usage(input_tokens=prompt, output_tokens=output, cache_read_tokens=int(cached),
                        reasoning_tokens=thinking),
            provider=self.name,
            model=str(getattr(completion, "model", self.model)),
            refusal=Refusal(explanation=refusal_text) if refusal_text else None,
        )


class OpenAIProvider(OpenAICompatibleProvider):
    name = "openai"

    def __init__(self, settings: OpenAISettings, api_key: Callable[[], str | None], *,
                 client_factory: Callable[[str], Any] | None = None) -> None:
        super().__init__(model=settings.model, base_url=settings.base_url or None, api_key=api_key,
                         timeout_s=settings.timeout_s, max_retries=settings.max_retries,
                         client_factory=client_factory)


class XaiProvider(OpenAICompatibleProvider):
    """xAI's Grok models (https://api.x.ai/v1, XAI_API_KEY).

    Grok models always reason; `reasoning_effort` follows the request's effort. A model that does
    not take a parameter JARVIS sends (an older Grok without effort levels, a model without strict
    JSON schemas) answers 400 naming it: the request is repeated without it, and the provider
    remembers that for the rest of the session.
    """

    name = "xai"

    def __init__(self, settings: XaiSettings, api_key: Callable[[], str | None], *,
                 client_factory: Callable[[str], Any] | None = None) -> None:
        super().__init__(model=settings.model, base_url=settings.base_url or "https://api.x.ai/v1", api_key=api_key,
                         timeout_s=settings.timeout_s, max_retries=settings.max_retries,
                         client_factory=client_factory)
        self._no_effort: set[str] = set()
        self._json_object: set[str] = set()
        self._max_tokens: dict[str, str] = {}

    def json_mode_for(self, model: str) -> str:
        return "json_object" if model in self._json_object else self.json_mode

    def max_tokens_param_for(self, model: str) -> str:
        return self._max_tokens.get(model, self.max_tokens_param)

    def build_request(self, req: CompletionRequest) -> dict[str, Any]:
        kwargs = super().build_request(req)
        kwargs.pop("reasoning_effort", None)
        if kwargs["model"] not in self._no_effort:
            kwargs["reasoning_effort"] = _XAI_EFFORT.get(req.effort, "medium")
        return kwargs

    async def complete(self, request: CompletionRequest) -> AIResponse:
        model = request.model or self.model
        while True:
            try:
                return await super().complete(request)
            except ProviderError as exc:
                if not self._adapt(model, exc):
                    raise
                self.errors -= 1  # not a failure: the request is repeated in the form this model takes
                log.info("xAI %s: %s; repeating the request without it", model, exc.message[:200])

    def _adapt(self, model: str, exc: ProviderError) -> bool:
        """Learn from a 400 that names a parameter this model does not take. True = try again.

        Each parameter is dropped at most once per model, so this ends after a few attempts."""
        if exc.details.get("status") != 400:
            return False
        text = exc.message.lower()
        if "reasoning" in text and model not in self._no_effort:
            self._no_effort.add(model)
            return True
        if "max_completion_tokens" in text and model not in self._max_tokens:
            self._max_tokens[model] = "max_tokens"
            return True
        if ("response_format" in text or "json_schema" in text) and model not in self._json_object:
            self._json_object.add(model)
            return True
        return False


HEBREW = re.compile(r"[\u0590-\u05FF]")
# Gemma writing Hebrew slips into English ("סינון ו-sorting", "(Decluttering)", even "סorting"); Claude and Grok don't.
HEBREW_ONLY = ("When you write in Hebrew, use Hebrew words only: no English word or English gloss in parentheses, and "
               "never Hebrew and Latin letters in one word. Names of apps, products, files and code stay as they are.")


class LocalModelProvider(OpenAICompatibleProvider):
    """A model on this computer: JARVIS's own llama-server (`server`, see `jarvis.ai.local_server`), started for
    the first request; or, with `base_url` set, an OpenAI-compatible server that already runs (e.g. Ollama).

    Free, so the router uses it without asking. JARVIS's own server constrains a JSON reply to the schema itself
    (llama.cpp grammars); another server gets JSON mode with the schema in the prompt."""

    name = "local"
    json_mode = "json_object"
    max_tokens_param = "max_tokens"

    def __init__(self, settings: LocalModelSettings, *, client_factory: Callable[[str], Any] | None = None,
                 server: ChatServer | None = None) -> None:
        self.settings = settings
        self.server = server
        self.in_flight = 0                    # requests under way: the graphics card is not taken from them
        self._loose_json = False              # the own server refused a schema once: JSON mode from then on
        super().__init__(model=settings.model, base_url=settings.base_url or None, api_key=self._local_key,
                         timeout_s=settings.timeout_s, max_retries=1, client_factory=client_factory)

    @property
    def own(self) -> bool:
        """JARVIS runs the server itself (no `base_url`)."""
        return not self.settings.base_url and self.server is not None

    def _local_key(self) -> str | None:
        if not self.settings.enabled:
            return None
        if self.own:
            assert self.server is not None
            return self.server.api_key or None
        return "local"        # other local servers ignore the key, but the client library requires one

    def available(self) -> bool:
        if not self.settings.enabled:
            return False
        if self.settings.base_url:
            return True
        return self.server is not None and self.server.problem() is None

    def json_mode_for(self, model: str) -> str:
        return "strict" if self.own and not self._loose_json else self.json_mode

    def busy(self) -> bool:
        return self.in_flight > 0

    async def release_gpu(self) -> None:
        """Another local model needs the graphics card: stop the idle server (the next request starts it)."""
        if self.server is not None and not self.busy():
            await self.server.stop()

    @staticmethod
    def in_hebrew(request: CompletionRequest) -> CompletionRequest:
        """A Hebrew request (its instructions or what the user wrote) also gets the Hebrew-only rule."""
        said = [request.system, *(m.text for m in request.messages if m.role == "user")]
        if HEBREW_ONLY in request.system or not any(HEBREW.search(text or "") for text in said):
            return request
        return request.model_copy(update={"system": f"{request.system}\n\n{HEBREW_ONLY}".strip()})

    async def complete(self, request: CompletionRequest) -> AIResponse:
        request = self.in_hebrew(request)
        if not self.own:
            return await super().complete(request)
        assert self.server is not None
        self.in_flight += 1
        try:
            try:
                await self.server.ensure_running()
            except RuntimeError as exc:
                blocked = "Smart App Control" in str(exc)
                raise ProviderUnavailable(f"The local model could not start: {exc}",
                                          details={"code": "local_blocked" if blocked else "local_down"}) from exc
            self._base_url = f"{self.server.base_url}/v1"
            try:
                return await super().complete(request)
            except ProviderError as exc:
                schema_refused = (request.json_schema is not None and not self._loose_json
                                  and exc.details.get("status") in (400, 500)
                                  and any(w in exc.message.lower() for w in ("schema", "grammar")))
                if not schema_refused:
                    raise
                self._loose_json = True
                self.errors -= 1
                log.info("The local model refused a JSON schema; using JSON mode instead")
                return await super().complete(request)
        finally:
            self.in_flight -= 1
            self.server.touch()
