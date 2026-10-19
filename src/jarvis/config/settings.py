"""Typed configuration model.

Every setting has a safe default, so JARVIS runs with no config file at all. Users override
values in `config.toml` (see `loader.py`) or with environment variables
`JARVIS__<SECTION>__<KEY>=value`. Secrets (API keys) never live here — see
`jarvis.security.secrets`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jarvis.common.enums import AgentId, Mode, RiskLevel


class _Section(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)


class GeneralSettings(_Section):
    assistant_name: str = "JARVIS"
    user_name: str = ""
    language: Literal["auto", "he", "en"] = "auto"
    default_mode: Mode = Mode.NORMAL
    resume_interrupted_on_start: bool = False
    # Start in the background (mini orb, listening for the wake word) when the user signs in to Windows.
    start_with_windows: bool = True


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
    # Server-side fallback: a request the model declines is retried on Anthropic's recommended
    # fallback model instead of failing.
    fallbacks: bool = True
    # Short progress notes between tool calls (shown in the activity log).
    progress_updates: bool = True
    prompt_caching: bool = True
    timeout_s: float = 300.0
    max_retries: int = 2
    base_url: str = ""
    use_server_web_search: bool = True
    web_search_max_uses: int = 8


class OpenAISettings(_Section):
    model: str = "gpt-5"
    timeout_s: float = 300.0
    max_retries: int = 2
    base_url: str = ""


class XaiSettings(_Section):
    """xAI's Grok models through their OpenAI-compatible API (key: XAI_API_KEY)."""

    model: str = "grok-4.7"
    timeout_s: float = 600.0      # Grok always reasons; long answers take a while
    max_retries: int = 2
    base_url: str = "https://api.x.ai/v1"


class LocalModelSettings(_Section):
    """The model JARVIS talks, plans and works with on this computer: free, and nothing leaves the computer. By
    default JARVIS runs its own (Gemma 4 12B through llama.cpp, installed with `jarvis chat install`); with
    `base_url` set it uses an OpenAI-compatible server that already runs (Ollama, LM Studio, another llama-server)."""

    enabled: bool = True
    # "" = JARVIS's own server; otherwise an OpenAI-compatible endpoint, e.g. http://127.0.0.1:11434/v1 (Ollama).
    base_url: str = ""
    model: str = "gemma-4-12b"
    timeout_s: float = 300.0
    # Where the model lives ("" = a "chat" folder beside computer control's models, else <cache>/models/chat).
    models_dir: str = ""
    port: int = 0                         # for JARVIS's own server (0 = any free port)
    gpu_layers: int = -1                  # model layers on the graphics card (-1 = as many as fit)
    context: int = 32768                  # tokens, shared by the requests answered at once
    parallel: int = 2                     # requests answered at once (the agents can ask together)
    idle_minutes: float = 30.0            # the server stops after this long unused (frees the graphics card)
    thinking: bool = False                # the model reasons privately before answering: better, much slower


PROVIDER_NAMES = ("anthropic", "xai", "openai", "local")


def _default_agent_providers() -> dict[str, str]:
    # The five agents work with Grok when an xAI key is set (and, with `cloud_only_when_told`, the user said
    # so); without one (or when Grok fails) they fall back to the usual order, Claude first.
    return {a.value: "xai" for a in AgentId}


class AISettings(_Section):
    provider: Literal["auto", "anthropic", "xai", "openai", "local", "none"] = "auto"
    # Grok, Claude and OpenAI cost money per request: use one only when the user says so, by naming it in the
    # request ("תשתמש בגרוק ...") or with a yes when JARVIS asks. A local model needs no word.
    cloud_only_when_told: bool = True
    roles: dict[str, AIRoleSettings] = Field(default_factory=_default_roles)
    # Preferred provider for everything an agent asks a model while it works ("auto" = the role's order).
    agent_providers: dict[str, str] = Field(default_factory=_default_agent_providers)
    anthropic: AnthropicSettings = Field(default_factory=AnthropicSettings)
    xai: XaiSettings = Field(default_factory=XaiSettings)
    openai: OpenAISettings = Field(default_factory=OpenAISettings)
    local: LocalModelSettings = Field(default_factory=LocalModelSettings)

    def role(self, name: str) -> AIRoleSettings:
        return self.roles.get(name) or self.roles.get("agent") or AIRoleSettings()

    @field_validator("agent_providers")
    @classmethod
    def _check_agent_providers(cls, value: dict[str, str]) -> dict[str, str]:
        agents = {a.value for a in AgentId}
        for key, provider in value.items():
            if key not in agents:
                raise ValueError(f"unknown agent in agent_providers: {key}")
            if provider not in ("auto", *PROVIDER_NAMES):
                raise ValueError(f"agent_providers.{key} must be auto or one of {', '.join(PROVIDER_NAMES)}")
        return value


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
    # CHAT mode refuses every non-read-only tool before this threshold is consulted.
    return {
        Mode.CHAT.value: int(RiskLevel.HIGH),
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
    # AUTONOMOUS mode may run HIGH-risk actions without asking when this is on; CRITICAL always asks.
    autonomous_preapprove_high: bool = False
    # Highest risk each agent may ever reach (0 SAFE .. 4 CRITICAL); above it the action is refused.
    agent_risk_ceilings: dict[str, int] = Field(
        default_factory=lambda: {"research": 3, "system": 4, "development": 4, "creative": 3, "planner": 2}
    )
    use_recycle_bin: bool = True
    # Network shares (\\server\share) the agents may use. Any other network path is refused before
    # Windows opens it, because opening one can hand your Windows sign-in to that server.
    allowed_network_shares: list[str] = Field(default_factory=list)

    @field_validator("confirm_thresholds")
    @classmethod
    def _check_thresholds(cls, value: dict[str, int]) -> dict[str, int]:
        modes = {m.value for m in Mode}
        for key, level in value.items():
            if key not in modes:
                raise ValueError(f"unknown mode in confirm_thresholds: {key}")
            if not int(RiskLevel.SAFE) <= int(level) <= int(RiskLevel.CRITICAL):
                raise ValueError(f"confirm_thresholds.{key} must be between 0 and 4")
        return value

    @field_validator("agent_risk_ceilings")
    @classmethod
    def _check_ceilings(cls, value: dict[str, int]) -> dict[str, int]:
        agents = {a.value for a in AgentId}
        for key, level in value.items():
            if key not in agents:
                raise ValueError(f"unknown agent in agent_risk_ceilings: {key}")
            if not int(RiskLevel.SAFE) <= int(level) <= int(RiskLevel.CRITICAL):
                raise ValueError(f"agent_risk_ceilings.{key} must be between 0 and 4")
        return value


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
    # On an NVIDIA graphics card (with `pip install jarvis-assistant[gpu]`): a larger model, much better in Hebrew.
    stt_model_gpu: str = "large-v3-turbo"
    stt_compute_type_gpu: str = "int8_float16"
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
    ctrl_to_talk: bool = True              # a short press of Ctrl on its own starts listening (Windows)


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
    desktop_notifications: bool = True     # Windows notifications while the JARVIS window is not on screen


class WebSettings(_Section):
    search_provider: Literal["auto", "duckduckgo", "brave", "tavily"] = "auto"
    timeout_s: float = 20.0
    max_fetch_bytes: int = 3_000_000
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0 Safari/537.36 JARVIS/1.0"
    )


class ComputerSettings(_Section):
    """Computer control: a local vision model (Holo3.1 by H Company) looks at the screen and works the mouse and
    keyboard for the System Agent. Every task asks first; every click and keystroke passes the permission gate."""

    enabled: bool = True
    # Where the model and its llama.cpp server live ("" = <cache>/models/computer; `jarvis computer install`).
    models_dir: str = ""
    # An OpenAI-compatible server that already runs a Holo model ("" = JARVIS starts its own on this computer).
    # A server on another machine receives screenshots of your screen, so every task then says so first.
    server_url: str = ""
    port: int = 8091                      # for JARVIS's own server (0 = any free port)
    gpu_layers: int = 999                 # model layers on the graphics card (999 = all of them)
    context: int = 32768
    idle_minutes: float = 10.0            # the server stops after this long without a task (frees the graphics card)
    max_steps: int = 40                   # actions per task
    max_minutes: float = 15.0             # time per task
    settle_s: float = 0.5                 # wait after each action before looking at the screen again
    # The model reasons privately before each action. Off is about twice as fast, and was steadier in tests.
    thinking: bool = False
    # Off: only Esc twice (or "stop everything") stops a task, so you can use the mouse while JARVIS works.
    # On: a task also stops the moment you touch the mouse or keyboard.
    stop_on_user_input: bool = False

    @field_validator("max_steps")
    @classmethod
    def _check_steps(cls, value: int) -> int:
        if not 1 <= value <= 200:
            raise ValueError("computer.max_steps must be between 1 and 200")
        return value

    @field_validator("max_minutes", "idle_minutes")
    @classmethod
    def _check_minutes(cls, value: float) -> float:
        if not 0.5 <= value <= 240:
            raise ValueError("must be between 0.5 and 240 minutes")
        return value


class MediaSettings(_Section):
    """The "Image creation" and "Video creation" screens.

    "local" (the default): free open models on this computer's graphics card (Z-Image-Turbo for images, Wan 2.2
    for video, `jarvis media install`); nothing leaves the computer and nothing is paid for. "xai": Grok Imagine,
    paid per image and per second, used only when it is chosen here."""

    engine: Literal["local", "xai"] = "local"
    # Where the local models and their runtime live ("" = <cache>/models/media; `jarvis media install --dir`).
    models_dir: str = ""
    # The hungriest way a model may run; "auto" picks the fastest that fits the RAM and graphics memory free when it
    # loads. "gpu" all of it on the card; "seq8" the text encoder on the card only to read the description, then the
    # rest; "model" each part on the card only while it works, the rest in RAM; "group8" layers streamed in. An "8"
    # keeps the big parts in 8 bits: half the memory, for a little detail.
    offload: Literal["auto", "gpu", "gpu8", "seq8", "model", "model8", "group8"] = "auto"
    translate: bool = True              # a Hebrew description is translated to English on this computer first
    image_steps: int = 9                # Z-Image-Turbo is made for 8 steps (9 in diffusers' count)
    video_steps: int = 30
    idle_minutes: float = 5.0           # the models leave the graphics card after this long unused

    @field_validator("image_steps", "video_steps")
    @classmethod
    def _check_steps(cls, value: int) -> int:
        if not 1 <= value <= 100:
            raise ValueError("must be between 1 and 100 steps")
        return value

    @field_validator("idle_minutes")
    @classmethod
    def _check_idle(cls, value: float) -> float:
        if not 0.5 <= value <= 240:
            raise ValueError("must be between 0.5 and 240 minutes")
        return value


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
    computer: ComputerSettings = Field(default_factory=ComputerSettings)
    media: MediaSettings = Field(default_factory=MediaSettings)
