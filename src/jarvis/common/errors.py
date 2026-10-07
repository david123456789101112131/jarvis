"""Error hierarchy. Each error carries a stable `code` so the UI and logs can classify it."""

from __future__ import annotations


class JarvisError(Exception):
    code = "jarvis_error"

    def __init__(self, message: str = "", *, details: dict | None = None) -> None:
        super().__init__(message or self.__class__.__name__)
        self.message = message or self.__class__.__name__
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


class ConfigError(JarvisError):
    code = "config_error"


class PermissionDenied(JarvisError):
    code = "permission_denied"


class ConfirmationDenied(PermissionDenied):
    code = "confirmation_denied"


class KillSwitchEngaged(JarvisError):
    code = "kill_switch_engaged"


class TaskCancelled(JarvisError):
    code = "task_cancelled"


class TaskTimeout(JarvisError):
    code = "task_timeout"


class ToolError(JarvisError):
    code = "tool_error"


class ToolNotFound(ToolError):
    code = "tool_not_found"


class InvalidToolInput(ToolError):
    code = "invalid_tool_input"


class AgentError(JarvisError):
    code = "agent_error"


class AgentUnavailable(AgentError):
    code = "agent_unavailable"


class PlanError(JarvisError):
    code = "plan_error"


class TaskRefused(JarvisError):
    """A task request JARVIS will not start (or resume) in the current state, e.g. in chat mode."""

    code = "task_refused"


class ProviderError(JarvisError):
    """An AI provider failed. `retryable` tells the router whether another attempt can help."""

    code = "provider_error"

    def __init__(self, message: str = "", *, retryable: bool = False, details: dict | None = None) -> None:
        super().__init__(message, details=details)
        self.retryable = retryable


class ProviderUnavailable(ProviderError):
    code = "provider_unavailable"


class ProviderRefusal(ProviderError):
    code = "provider_refusal"


class MemoryError_(JarvisError):
    code = "memory_error"


class VoiceError(JarvisError):
    code = "voice_error"


class SecurityViolation(JarvisError):
    code = "security_violation"
