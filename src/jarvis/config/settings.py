"""Typed configuration model.

Every setting has a safe default, so JARVIS runs with no config file at all. Users override
values in `config.toml` (see `loader.py`) or with environment variables
`JARVIS__<SECTION>__<KEY>=value`. Secrets (API keys) never live here — see
`jarvis.security.secrets`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from jarvis.common.enums import AgentId, Mode, RiskLevel


class _Section(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)


class GeneralSettings(_Section):
    assistant_name: str = "JARVIS"
    user_name: str = ""
    language: Literal["auto", "he", "en"] = "auto"
    default_mode: Mode = Mode.NORMAL
    resume_interrupted_on_start: bool = False


class PathsSettings(_Section):
    """Empty strings mean "use the platform default" (resolved in `jarvis.config.paths`)."""

    home_dir: str = ""
    data_dir: str = ""
    log_dir: str = ""
    cache_dir: str = ""
    workspace_dir: str = ""
    projects_dir: str = ""
    extra_write_roots: list[str] = Field(default_factory=list)
    extra_read_roots: list[str] = Field(default_factory=list)


class ServerSettings(_Section):
    host: str = "127.0.0.1"
    port: int = 8765
    port_fallback_range: int = 20


class AIRoleSettings(_Section):
    provider: str = "auto"
    model: str = ""
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    max_tokens: int = 16000


def _default_roles() -> dict[str, AIRoleSettings]:
    return {
        "chat": AIRoleSettings(effort="low", max_tokens=8000),
        "intent": AIRoleSettings(effort="low", max_tokens=2000),
        "planner": AIRoleSettings(effort="high", max_tokens=16000),
        "agent": AIRoleSettings(effort="medium", max_tokens=16000),
        "verifier": AIRoleSettings(effort="medium", max_tokens=8000),
        "synthesis": AIRoleSettings(effort="medium", max_tokens=8000),
    }


class AnthropicSettings(_Section):
    model: str = "claude-opus-5-5"
    fallbacks: bool = True
    timeout_s: float = 300.0
    max_retries: int = 2
    base_url: str = ""
    use_server_web_search: bool = True


class OpenAISettings(_Section):
    model: str = "gpt-5"
    timeout_s: float = 300.0
    max_retries: int = 2
    base_url: str = ""


class LocalModelSettings(_Section):
    enabled: bool = False
    base_url: str = "http://127.0.0.1:11434/v1"
    model: str = "llama3.1"
    timeout_s: float = 300.0


class AISettings(_Section):
    provider: Literal["auto", "anthropic", "openai", "local", "none"] = "auto"
    roles: dict[str, AIRoleSettings] = Field(default_factory=_default_roles)
    anthropic: AnthropicSettings = Field(default_factory=AnthropicSettings)
    openai: OpenAISettings = Field(default_factory=OpenAISettings)
    local: LocalModelSettings = Field(default_factory=LocalModelSettings)

    def role(self, name: str) -> AIRoleSettings:
        return self.roles.get(name) or self.roles.get("agent") or AIRoleSettings()


def _default_agent_timeouts() -> dict[str, float]:
    return {
        AgentId.RESEARCH.value: 300.0,
        AgentId.SYSTEM.value: 180.0,
        AgentId.DEVELOPMENT.value: 900.0,
        AgentId.CREATIVE.value: 300.0,
        AgentId.PLANNER.value: 240.0,
    }


class AgentSettings(_Section):
    concurrency: dict[str, int] = Field(
        default_factory=lambda: {a.value: (2 if a in (AgentId.RESEARCH, AgentId.SYSTEM) else 1) for a in AgentId}
    )
    timeouts_s: dict[str, float] = Field(default_factory=_default_agent_timeouts)
    llm_max_steps: int = 30
    autostart: bool = True


class OrchestratorSettings(_Section):
    max_parallel_subtasks: int = 5
    max_retries: int = 2
    retry_backoff_s: float = 1.0
    retry_backoff_max_s: float = 20.0
    max_followups: int = 3
    max_same_error: int = 3
    task_timeout_s: float = 3600.0
    autonomous_task_timeout_s: float = 4 * 3600.0
    verify_results: bool = True


class MemorySettings(_Section):
    backend: Literal["sqlite"] = "sqlite"
    db_path: str = ""
    short_term_turns: int = 30
    recall_limit: int = 8


def _default_thresholds() -> dict[str, int]:
    return {
        Mode.CHAT.value: int(RiskLevel.SAFE),
        Mode.NORMAL.value: int(RiskLevel.HIGH),
        Mode.TASK.value: int(RiskLevel.HIGH),
        Mode.AUTONOMOUS.value: int(RiskLevel.HIGH),
        Mode.DEBUG.value: int(RiskLevel.MEDIUM),
    }


class SecuritySettings(_Section):
    confirm_timeout_s: float = 120.0
    confirm_thresholds: dict[str, int] = Field(default_factory=_default_thresholds)
    protected_paths: list[str] = Field(default_factory=list)
    network_enabled: bool = True
    network_allowlist: list[str] = Field(default_factory=list)
    shell_enabled: bool = True
    max_output_chars: int = 20000
    command_timeout_s: float = 300.0
    broad_delete_threshold: int = 20


class VoiceSettings(_Section):
    enabled: bool = True
    wake_word_engine: Literal["auto", "openwakeword", "porcupine", "transcript", "none"] = "auto"
    wake_word_model: str = "hey_jarvis"
    wake_threshold: float = 0.5
    input_device: str = ""
    output_device: str = ""
    stt_engine: Literal["auto", "faster_whisper", "openai", "none"] = "auto"
    stt_model: str = "small"
    stt_language: Literal["auto", "he", "en"] = "auto"
    stt_device: Literal["auto", "cpu", "cuda"] = "auto"
    stt_compute_type: str = "int8"
    tts_engine: Literal["auto", "edge", "sapi", "none"] = "auto"
    tts_voice_he: str = "he-IL-AvriNeural"
    tts_voice_en: str = "en-GB-RyanNeural"
    tts_rate: str = "+5%"
    speak_responses: bool = True
    follow_up_window_s: float = 6.0
    max_utterance_s: float = 15.0
    end_silence_ms: int = 700
    partial_transcripts: bool = True
    always_listening: bool = True


class UISettings(_Section):
    theme_color: Literal["blue", "green", "orange", "red", "purple", "white"] = "blue"
    orb_size: float = 1.0
    orb_opacity: float = 0.92
    follow_mouse: bool = False
    always_active: bool = True
    mini_orb: bool = True
    window_width: int = 1480
    window_height: int = 920
    debug_devtools: bool = False


class WebSettings(_Section):
    search_provider: Literal["auto", "duckduckgo", "brave", "tavily"] = "auto"
    timeout_s: float = 20.0
    max_fetch_bytes: int = 3_000_000
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0 Safari/537.36 JARVIS/1.0"
    )


class Settings(_Section):
    general: GeneralSettings = Field(default_factory=GeneralSettings)
    paths: PathsSettings = Field(default_factory=PathsSettings)
    server: ServerSettings = Field(default_factory=ServerSettings)
    ai: AISettings = Field(default_factory=AISettings)
    agents: AgentSettings = Field(default_factory=AgentSettings)
    orchestrator: OrchestratorSettings = Field(default_factory=OrchestratorSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    ui: UISettings = Field(default_factory=UISettings)
    web: WebSettings = Field(default_factory=WebSettings)
