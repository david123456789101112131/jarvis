"""AI provider layer: provider-neutral conversations, Anthropic / OpenAI / local backends, routing."""

from jarvis.ai.anthropic_provider import AnthropicProvider
from jarvis.ai.base import AIProvider
from jarvis.ai.openai_provider import LocalModelProvider, OpenAICompatibleProvider, OpenAIProvider
from jarvis.ai.router import ProviderRouter
from jarvis.ai.types import (
    AIMessage,
    AIResponse,
    CompletionRequest,
    Refusal,
    ServerAction,
    Source,
    ToolCall,
    ToolOutput,
    ToolSpec,
    Usage,
    strict_schema,
)

__all__ = [
    "AIMessage",
    "AIProvider",
    "AIResponse",
    "AnthropicProvider",
    "CompletionRequest",
    "LocalModelProvider",
    "OpenAICompatibleProvider",
    "OpenAIProvider",
    "ProviderRouter",
    "Refusal",
    "ServerAction",
    "Source",
    "ToolCall",
    "ToolOutput",
    "ToolSpec",
    "Usage",
    "strict_schema",
]
