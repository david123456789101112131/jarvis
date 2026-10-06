"""Anthropic (Claude) provider.

- Model `claude-opus-5-5` by default, adaptive thinking (it cannot be turned off on this model),
  effort set explicitly per role.
- Server-side fallbacks (`fallbacks: "default"`): a request the model declines for policy
  reasons is retried on Anthropic's recommended fallback model instead of failing.
- Assistant turns are replayed exactly as received (thinking blocks, server tool results,
  fallback markers), and the history is append-only, which keeps preserved thinking valid
  and the prompt cache warm.
- `pause_turn` is continued automatically; `refusal` raises ProviderRefusal.
- Optional features that an account may not have (fallbacks, progress updates) are switched
  off for the session if the API rejects them, and the request is retried once without them.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from typing import Any

from jarvis.ai.base import AIProvider
from jarvis.ai.types import (
    AIMessage,
    AIResponse,
    CompletionRequest,
    Refusal,
    ServerAction,
    Source,
    ToolCall,
    Usage,
    strict_schema,
)
from jarvis.common.errors import ProviderError, ProviderRefusal, ProviderUnavailable
from jarvis.config.settings import AnthropicSettings

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
UPDATES_BETA = "thinking-display-updates-2026-08-18"
MAX_CONTINUATIONS = 5

_STOP = {
    "end_turn": "end",
    "stop_sequence": "end",
    "compaction": "end",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "model_context_window_exceeded": "max_tokens",
    "refusal": "refusal",
    "pause_turn": "pause",
}

# Feature -> words that identify it in a 400 error message.
_FEATURE_MARKERS = {
    "fallbacks": ("fallback", FALLBACK_BETA),
    "updates": ("display", UPDATES_BETA, "updates"),
    "cache": ("cache_control",),
}


def dump_block(block: Any) -> dict[str, Any]:
    """Serialize a response block exactly the way the SDK sends it back (unset fields omitted)."""
    if isinstance(block, dict):
        return block
    return block.model_dump(mode="json", exclude_unset=True, by_alias=True,
                            exclude=getattr(block, "__api_exclude__", None))


class AnthropicProvider(AIProvider):
    name = "anthropic"
    supports_server_web = True
    supports_json_schema = True

    def __init__(
        self,
        settings: AnthropicSettings,
        api_key: Callable[[], str | None],
        *,
        client_factory: Callable[[str], Any] | None = None,
    ) -> None:
        super().__init__()
        self.settings = settings
        self._api_key = api_key
        self._client_factory = client_factory
        self._client: Any = None
        self._client_fingerprint: str | None = None
        self.features = {
            "fallbacks": settings.fallbacks,
            "updates": settings.progress_updates,
            "cache": settings.prompt_caching,
        }
        self.fallbacks_served = 0

    # -- setup -------------------------------------------------------------------------------
    @property
    def model(self) -> str:
        return self.settings.model or "claude-opus-5-5"

    def available(self) -> bool:
        return bool(self._api_key())

    @property
    def fallbacks_enabled(self) -> bool:
        return bool(self.features.get("fallbacks"))

    def _get_client(self) -> Any:
        key = self._api_key()
        if not key:
            raise ProviderUnavailable("Anthropic API key is not configured (ANTHROPIC_API_KEY).")
        fingerprint = hashlib.sha256(key.encode()).hexdigest()
        if self._client is None or fingerprint != self._client_fingerprint:
            if self._client_factory is not None:
                self._client = self._client_factory(key)
            else:
                import anthropic

                self._client = anthropic.AsyncAnthropic(
                    api_key=key,
                    base_url=self.settings.base_url or None,
                    timeout=self.settings.timeout_s,
                    max_retries=self.settings.max_retries,
                )
            self._client_fingerprint = fingerprint
        return self._client

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None and hasattr(client, "close"):
            try:
                await client.close()
            except Exception:  # pragma: no cover
                log.debug("closing Anthropic client failed", exc_info=True)

    def describe(self) -> dict[str, Any]:
        info = super().describe()
        info.update({"fallbacks": self.fallbacks_enabled, "fallbacks_served": self.fallbacks_served,
                     "features": dict(self.features)})
        return info

    # -- request building ----------------------------------------------------------------------
    def build_messages(self, messages: list[AIMessage]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "assistant":
                native = m.native_for(self.name)
                if native is not None:
                    content: Any = native
                else:
                    content = []
                    if m.text.strip():
                        content.append({"type": "text", "text": m.text})
                    for c in m.tool_calls:
                        content.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.input})
                    if not content:
                        continue
                out.append({"role": "assistant", "content": content})
            else:
                if m.tool_results:
                    blocks: list[dict[str, Any]] = []
                    for r in m.tool_results:
                        block: dict[str, Any] = {"type": "tool_result", "tool_use_id": r.tool_call_id,
                                                 "content": r.content or "(empty)"}
                        if r.is_error:
                            block["is_error"] = True
                        blocks.append(block)
                    if m.text.strip():
                        blocks.append({"type": "text", "text": m.text})
                    out.append({"role": "user", "content": blocks})
                else:
                    out.append({"role": "user", "content": m.text or "(empty)"})
        return out

    def build_request(self, req: CompletionRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": req.model or self.model,
            "max_tokens": req.max_tokens,
            "messages": self.build_messages(req.messages),
        }
        thinking: dict[str, Any] = {"type": "adaptive"}
        betas: list[str] = []
        if self.features["updates"]:
            thinking["display"] = "updates"
            betas.append(UPDATES_BETA)
        kwargs["thinking"] = thinking

        output_config: dict[str, Any] = {"effort": req.effort}
        web_search = req.web_search_uses if self.settings.use_server_web_search else 0
        web_fetch = req.web_fetch and self.settings.use_server_web_search
        if req.json_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": strict_schema(req.json_schema)}
            # Web results carry citations, which cannot be combined with a JSON output format.
            web_search, web_fetch = 0, False
        kwargs["output_config"] = output_config

        if req.system:
            system_block: dict[str, Any] = {"type": "text", "text": req.system}
            if self.features["cache"]:
                system_block["cache_control"] = {"type": "ephemeral"}
            kwargs["system"] = [system_block]
        if self.features["cache"]:
            kwargs["cache_control"] = {"type": "ephemeral"}

        tools: list[dict[str, Any]] = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in req.tools
        ]
        if web_search:
            tools.append({"type": "web_search_20260209", "name": "web_search", "max_uses": web_search})
        if web_fetch:
            tools.append({"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": max(3, web_search or 3)})
        if tools:
            kwargs["tools"] = tools

        if self.features["fallbacks"]:
            kwargs["fallbacks"] = "default"
            betas.append(FALLBACK_BETA)
        if betas:
            kwargs["betas"] = betas
        if req.timeout_s:
            kwargs["timeout"] = req.timeout_s
        return kwargs

    # -- call ----------------------------------------------------------------------------------
    async def complete(self, request: CompletionRequest) -> AIResponse:
        client = self._get_client()
        downgraded: set[str] = set()
        while True:
            kwargs = self.build_request(request)
            try:
                response = await self._call_with_continuations(client, kwargs)
            except ProviderError as exc:
                feature = self._feature_to_drop(exc, downgraded)
                if feature is None:
                    self.errors += 1
                    self.last_error = exc.message
                    raise
                log.warning("Anthropic rejected optional feature %r; continuing without it", feature)
                self.features[feature] = False
                downgraded.add(feature)
                continue
            if response.fallback_model:
                self.fallbacks_served += 1
            self.last_error = None
            if response.stop_reason == "refusal":
                self.errors += 1
                r = response.refusal or Refusal()
                detail = f" ({r.category})" if r.category else ""
                raise ProviderRefusal(
                    f"The model declined this request{detail}." + (f" {r.explanation}" if r.explanation else ""),
                    retryable=False,
                    details={"category": r.category, "provider": self.name},
                )
            return self._account(response)

    def _feature_to_drop(self, exc: ProviderError, already: set[str]) -> str | None:
        if exc.details.get("status") != 400:
            return None
        text = exc.message.lower()
        for feature, markers in _FEATURE_MARKERS.items():
            if self.features.get(feature) and feature not in already and any(m.lower() in text for m in markers):
                return feature
        return None

    async def _call_with_continuations(self, client: Any, kwargs: dict[str, Any]) -> AIResponse:
        base_messages = list(kwargs["messages"])
        content: list[dict[str, Any]] = []
        usage = Usage()
        msg: Any = None
        continuations = 0
        while True:
            if content:
                kwargs["messages"] = [*base_messages, {"role": "assistant", "content": content}]
            msg = await self._create(client, kwargs)
            content = content + [dump_block(b) for b in msg.content]
            usage = usage.add(_usage(msg))
            if msg.stop_reason != "pause_turn" or continuations >= MAX_CONTINUATIONS:
                break
            continuations += 1
            _rstrip_final_text(content)
        return self._to_response(msg, content, usage, continuations)

    async def _create(self, client: Any, kwargs: dict[str, Any]) -> Any:
        import anthropic

        try:
            return await client.beta.messages.create(**kwargs)
        except anthropic.AuthenticationError as exc:
            raise ProviderUnavailable("Anthropic rejected the API key (401). Update ANTHROPIC_API_KEY.",
                                      details={"status": 401}) from exc
        except anthropic.PermissionDeniedError as exc:
            raise ProviderUnavailable(f"Anthropic denied access (403): {_short(exc)}", details={"status": 403}) from exc
        except anthropic.NotFoundError as exc:
            raise ProviderError(f"Model {self.model!r} was not found (404).", details={"status": 404}) from exc
        except anthropic.RateLimitError as exc:
            raise ProviderError("Anthropic rate limit reached (429).", retryable=True, details={"status": 429}) from exc
        except anthropic.BadRequestError as exc:
            raise ProviderError(f"Anthropic rejected the request (400): {_short(exc)}",
                                details={"status": 400}) from exc
        except anthropic.APITimeoutError as exc:
            raise ProviderError("Anthropic request timed out.", retryable=True, details={"status": "timeout"}) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderUnavailable("Cannot reach the Anthropic API (network).", retryable=True,
                                      details={"status": "connection"}) from exc
        except anthropic.APIStatusError as exc:
            status = getattr(exc, "status_code", 0) or 0
            raise ProviderError(f"Anthropic API error ({status}): {_short(exc)}", retryable=status >= 500,
                                details={"status": status}) from exc

    # -- response parsing ----------------------------------------------------------------------
    def _to_response(self, msg: Any, content: list[dict[str, Any]], usage: Usage, continuations: int) -> AIResponse:
        content = sanitize_after_fallback(content)
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        sources: list[Source] = []
        actions: list[ServerAction] = []
        notes: list[str] = []
        fallback_model: str | None = None
        pending_server: dict[str, ServerAction] = {}
        for block in content:
            kind = block.get("type")
            if kind == "text":
                text_parts.append(block.get("text", ""))
            elif kind == "tool_use":
                calls.append(ToolCall(id=block["id"], name=block["name"], input=block.get("input") or {}))
            elif kind == "thinking":
                note = (block.get("thinking") or "").strip()
                if note:
                    notes.append(note)
            elif kind == "server_tool_use":
                action = ServerAction(name=block.get("name", "server_tool"), input=block.get("input") or {})
                actions.append(action)
                pending_server[block.get("id", "")] = action
            elif kind in ("web_search_tool_result", "web_fetch_tool_result"):
                action = pending_server.get(block.get("tool_use_id", ""))
                result = block.get("content")
                if isinstance(result, list):
                    for item in result:
                        if isinstance(item, dict) and item.get("url"):
                            sources.append(Source(url=item["url"], title=item.get("title", ""),
                                                  page_age=item.get("page_age")))
                elif isinstance(result, dict):
                    if result.get("error_code") or str(result.get("type", "")).endswith("error"):
                        if action is not None:
                            action.ok = False
                            action.error = str(result.get("error_code") or "error")
                    elif result.get("url"):
                        sources.append(Source(url=result["url"], title=_fetch_title(result)))
            elif kind == "fallback":
                to = block.get("to") or {}
                fallback_model = to.get("model") or fallback_model

        iterations = getattr(getattr(msg, "usage", None), "iterations", None) or []
        for it in iterations:
            if getattr(it, "type", None) == "fallback_message":
                fallback_model = str(getattr(it, "model", "") or fallback_model or "fallback")

        stop = _STOP.get(str(msg.stop_reason), "end")
        refusal = None
        if stop == "refusal":
            details = getattr(msg, "stop_details", None)
            refusal = Refusal(category=getattr(details, "category", None),
                              explanation=getattr(details, "explanation", None))
        if stop == "end" and calls:
            stop = "tool_use"
        text = "".join(text_parts).strip()
        message = AIMessage(role="assistant", text=text, tool_calls=calls,
                            native={"provider": self.name, "content": content})
        return AIResponse(
            text=text,
            tool_calls=calls,
            stop_reason=stop,  # type: ignore[arg-type]
            message=message,
            usage=usage,
            provider=self.name,
            model=str(getattr(msg, "model", self.model)),
            fallback_model=fallback_model,
            refusal=refusal,
            sources=_dedupe_sources(sources),
            server_actions=actions,
            progress_notes=notes,
            continuations=continuations,
        )


_ECHO_SAFE_BEFORE_FALLBACK = {"text", "fallback", "web_search_tool_result", "web_fetch_tool_result"}


def sanitize_after_fallback(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prepare a turn that switched models mid-output for echoing back.

    Before the last `fallback` marker, only text blocks and paired server-tool blocks are kept;
    thinking, tool_use (never executed), unpaired server_tool_use and unknown blocks are dropped.
    """
    last = max((i for i, b in enumerate(content) if b.get("type") == "fallback"), default=-1)
    if last < 0:
        return content
    head, tail = content[:last], content[last:]
    result_ids = {b.get("tool_use_id") for b in head if str(b.get("type", "")).endswith("_tool_result")}
    kept: list[dict[str, Any]] = []
    for b in head:
        kind = b.get("type")
        if kind == "server_tool_use":
            if b.get("id") in result_ids:
                kept.append(b)
        elif kind in _ECHO_SAFE_BEFORE_FALLBACK:
            kept.append(b)
    return kept + tail


def _usage(msg: Any) -> Usage:
    u = getattr(msg, "usage", None)
    if u is None:
        return Usage()
    return Usage(
        input_tokens=int(getattr(u, "input_tokens", 0) or 0),
        output_tokens=int(getattr(u, "output_tokens", 0) or 0),
        cache_read_tokens=int(getattr(u, "cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(getattr(u, "cache_creation_input_tokens", 0) or 0),
    )


def _rstrip_final_text(content: list[dict[str, Any]]) -> None:
    """A re-sent assistant turn must not end in whitespace."""
    if content and content[-1].get("type") == "text":
        content[-1] = {**content[-1], "text": content[-1].get("text", "").rstrip()}
        if not content[-1]["text"]:
            content.pop()


def _fetch_title(result: dict[str, Any]) -> str:
    doc = result.get("content")
    if isinstance(doc, dict):
        return str(doc.get("title") or "")
    return ""


def _dedupe_sources(sources: list[Source]) -> list[Source]:
    seen: set[str] = set()
    out: list[Source] = []
    for s in sources:
        if s.url not in seen:
            seen.add(s.url)
            out.append(s)
    return out


def _short(exc: Exception, limit: int = 400) -> str:
    body = getattr(exc, "body", None)
    message = None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            message = err.get("message")
    text = str(message or getattr(exc, "message", None) or exc)
    return text if len(text) <= limit else text[:limit] + "…"
