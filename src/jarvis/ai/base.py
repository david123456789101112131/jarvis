"""AIProvider: the interface every model backend implements (Anthropic, OpenAI, local models)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from jarvis.ai.types import AIResponse, CompletionRequest, Usage


class AIProvider(ABC):
    """One model backend. Implementations must be safe to call concurrently."""

    name: str = "provider"
    #: The provider can run web search / fetch on its own servers.
    supports_server_web: bool = False
    #: The provider can force the final answer into a JSON schema.
    supports_json_schema: bool = True

    def __init__(self) -> None:
        self.usage = Usage()
        self.calls = 0
        self.errors = 0
        self.last_error: str | None = None

    @property
    @abstractmethod
    def model(self) -> str: ...

    @abstractmethod
    def available(self) -> bool:
        """True when the provider is configured (credentials / endpoint present)."""

    @abstractmethod
    async def complete(self, request: CompletionRequest) -> AIResponse:
        """Run one model turn. Raises ProviderError subclasses on failure."""

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        return None

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "model": self.model,
            "available": self.available(),
            "calls": self.calls,
            "errors": self.errors,
            "last_error": self.last_error,
            "usage": self.usage.model_dump(),
        }

    def _account(self, response: AIResponse) -> AIResponse:
        self.calls += 1
        self.usage = self.usage.add(response.usage)
        response.provider = response.provider or self.name
        return response
