"""Provider-neutral conversation types.

A conversation is a list of `AIMessage`s. Each provider converts it to its own wire format.
Assistant turns keep the provider's original content (`native`) so the same provider can
replay them byte-for-byte (thinking blocks, server tool results, fallback markers); another
provider uses the neutral fields (`text`, `tool_calls`) instead.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

Effort = Literal["low", "medium", "high", "xhigh", "max"]
StopReason = Literal["end", "tool_use", "max_tokens", "refusal", "pause", "error"]


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]


class ToolCall(BaseModel):
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolOutput(BaseModel):
    tool_call_id: str
    content: str
    is_error: bool = False
    name: str = ""
    halt: bool = False         # the user said no to this action: no more tool calls, the model reports


class AIMessage(BaseModel):
    role: Literal["user", "assistant"]
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_results: list[ToolOutput] = Field(default_factory=list)
    # {"provider": "anthropic", "content": [...]} — the provider's own blocks, replayed unchanged.
    native: dict[str, Any] | None = None

    @classmethod
    def user(cls, text: str) -> AIMessage:
        return cls(role="user", text=text)

    @classmethod
    def results(cls, outputs: list[ToolOutput], note: str = "") -> AIMessage:
        """Tool results go back in ONE user message; an optional note follows them."""
        return cls(role="user", tool_results=outputs, text=note)

    def native_for(self, provider: str) -> list[Any] | None:
        if self.native and self.native.get("provider") == provider:
            return self.native.get("content")
        return None


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0           # the thinking part of output_tokens (billed as output)

    def add(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
        )


class Source(BaseModel):
    url: str
    title: str = ""
    page_age: str | None = None


class ServerAction(BaseModel):
    """A tool the provider ran on its own servers (e.g. web search); logged after the fact."""

    name: str
    input: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    error: str | None = None


class Refusal(BaseModel):
    category: str | None = None
    explanation: str | None = None


class CompletionRequest(BaseModel):
    messages: list[AIMessage]
    system: str = ""
    tools: list[ToolSpec] = Field(default_factory=list)
    effort: Effort = "medium"
    max_tokens: int = 16000
    # JSON schema the final answer must follow (structured output). Mutually exclusive with tools
    # for providers that cannot combine them.
    json_schema: dict[str, Any] | None = None
    schema_name: str = "result"
    # Let the provider search/fetch the web itself (Anthropic server tools); 0 = off.
    web_search_uses: int = 0
    web_fetch: bool = False
    timeout_s: float | None = None
    role: str = "agent"
    model: str | None = None   # per-role override of the provider's default model
    # Called with the reply text so far while it is being written (a provider that can stream; others ignore it),
    # so a spoken reply can start before the model has finished.
    on_text: Callable[[str], None] | None = Field(default=None, exclude=True, repr=False)


class AIResponse(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    stop_reason: StopReason = "end"
    message: AIMessage
    usage: Usage = Field(default_factory=Usage)
    provider: str = ""
    model: str = ""
    fallback_model: str | None = None   # set when a server-side fallback model served the reply
    refusal: Refusal | None = None
    sources: list[Source] = Field(default_factory=list)
    server_actions: list[ServerAction] = Field(default_factory=list)
    progress_notes: list[str] = Field(default_factory=list)
    continuations: int = 0

    def parse_json(self) -> Any:
        """Parse the reply text as JSON (structured output)."""
        import json

        return json.loads(_strip_fences(self.text))


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


_UNSUPPORTED_KEYS = {
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern", "minItems", "maxItems", "uniqueItems",
    "minProperties", "maxProperties", "default", "examples", "title",
}


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Make a (pydantic) JSON schema acceptable for structured outputs.

    Every object gets `additionalProperties: false` and lists all its properties as required;
    keywords the structured-output engines reject are removed (the caller validates the
    parsed result with pydantic anyway, so no constraint is lost).
    """
    import copy

    out = copy.deepcopy(schema)

    def walk(node: Any, *, in_properties: bool = False) -> None:
        if isinstance(node, dict):
            if not in_properties:
                for key in list(node):
                    if key in _UNSUPPORTED_KEYS and not (key == "title" and not isinstance(node[key], str)):
                        node.pop(key)
                if node.get("type") == "object" or "properties" in node:
                    props = node.get("properties", {})
                    node["additionalProperties"] = False
                    node["required"] = list(props)
            for key, value in node.items():
                walk(value, in_properties=(key in ("properties", "$defs", "definitions") and not in_properties))
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(out)
    return out
