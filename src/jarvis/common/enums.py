"""Shared enumerations used across every layer.

Kept dependency-free so any module can import them without import cycles.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum


class Priority(IntEnum):
    """Task priority. Lower value = more urgent, so it sorts first in priority queues."""

    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3

    @classmethod
    def parse(cls, value: str | int | Priority | None, default: Priority | None = None) -> Priority:
        if value is None:
            return default if default is not None else cls.NORMAL
        if isinstance(value, Priority):
            return value
        if isinstance(value, int):
            return cls(value)
        key = str(value).strip().upper()
        aliases = {"URGENT": "CRITICAL", "MEDIUM": "NORMAL", "DEFAULT": "NORMAL"}
        key = aliases.get(key, key)
        try:
            return cls[key]
        except KeyError:
            return default if default is not None else cls.NORMAL


class AgentStatus(StrEnum):
    OFFLINE = "offline"
    IDLE = "idle"
    THINKING = "thinking"
    WORKING = "working"
    WAITING = "waiting"
    COMPLETED = "completed"
    ERROR = "error"


class TaskStatus(StrEnum):
    PENDING = "pending"
    PLANNING = "planning"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    SKIPPED = "skipped"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_TASK_STATUSES


TERMINAL_TASK_STATUSES = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.SKIPPED}
)


class ResultStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Mode(StrEnum):
    CHAT = "chat"
    NORMAL = "normal"
    TASK = "task"
    AUTONOMOUS = "autonomous"
    DEBUG = "debug"


class RiskLevel(IntEnum):
    SAFE = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

    def bump(self, steps: int = 1) -> RiskLevel:
        return RiskLevel(min(int(self) + steps, int(RiskLevel.CRITICAL)))


class AgentId(StrEnum):
    RESEARCH = "research"
    SYSTEM = "system"
    DEVELOPMENT = "development"
    CREATIVE = "creative"
    PLANNER = "planner"

    @classmethod
    def parse(cls, value: str | int | AgentId) -> AgentId:
        """Accept ids, display names ("Research Agent"), numbers ("3", "Agent 3") and Hebrew names."""
        if isinstance(value, AgentId):
            return value
        if isinstance(value, int):
            return AGENT_NUMBERS[value]
        text = str(value).strip().lower()
        for prefix in ("agent ", "agent#", "agent-", "סוכן "):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
        text = text.replace(" agent", "").replace("-agent", "").replace("_agent", "").strip()
        if text.isdigit():
            return AGENT_NUMBERS[int(text)]
        if text in _AGENT_ALIASES:
            return _AGENT_ALIASES[text]
        return cls(text)


AGENT_NUMBERS: dict[int, AgentId] = {
    1: AgentId.RESEARCH,
    2: AgentId.SYSTEM,
    3: AgentId.DEVELOPMENT,
    4: AgentId.CREATIVE,
    5: AgentId.PLANNER,
}

_AGENT_ALIASES: dict[str, AgentId] = {
    "research": AgentId.RESEARCH,
    "researcher": AgentId.RESEARCH,
    "מחקר": AgentId.RESEARCH,
    "המחקר": AgentId.RESEARCH,
    "system": AgentId.SYSTEM,
    "מערכת": AgentId.SYSTEM,
    "המערכת": AgentId.SYSTEM,
    "development": AgentId.DEVELOPMENT,
    "dev": AgentId.DEVELOPMENT,
    "developer": AgentId.DEVELOPMENT,
    "פיתוח": AgentId.DEVELOPMENT,
    "הפיתוח": AgentId.DEVELOPMENT,
    "creative": AgentId.CREATIVE,
    "design": AgentId.CREATIVE,
    "designer": AgentId.CREATIVE,
    "קריאייטיב": AgentId.CREATIVE,
    "יצירה": AgentId.CREATIVE,
    "עיצוב": AgentId.CREATIVE,
    "העיצוב": AgentId.CREATIVE,
    "planner": AgentId.PLANNER,
    "planning": AgentId.PLANNER,
    "תכנון": AgentId.PLANNER,
    "התכנון": AgentId.PLANNER,
    "מתכנן": AgentId.PLANNER,
}


class MessageType(StrEnum):
    TASK_ASSIGN = "task.assign"
    TASK_PROGRESS = "task.progress"
    TASK_RESULT = "task.result"
    TASK_CANCEL = "task.cancel"
    AGENT_STATUS = "agent.status"
    AGENT_COMMAND = "agent.command"
    CONFIRM_REQUEST = "confirm.request"
    CONFIRM_RESPONSE = "confirm.response"
    LOG = "log"
    EVENT = "event"


class InputSource(StrEnum):
    TEXT = "text"
    VOICE = "voice"
    API = "api"
    SYSTEM = "system"


class Capability(StrEnum):
    FS_READ = "fs.read"
    FS_WRITE = "fs.write"
    FS_DELETE = "fs.delete"
    PROCESS_LIST = "process.list"
    PROCESS_LAUNCH = "process.launch"
    PROCESS_KILL = "process.kill"
    SHELL_EXECUTE = "shell.execute"
    SYSTEM_INFO = "system.info"
    SYSTEM_SETTINGS = "system.settings"
    SYSTEM_POWER = "system.power"
    NET_SEARCH = "net.search"
    NET_FETCH = "net.fetch"
    NET_API = "net.api"
    CODE_EXECUTE = "code.execute"
    CODE_INSTALL = "code.install"
    GIT = "git"
    MEMORY_READ = "memory.read"
    MEMORY_WRITE = "memory.write"
    AI_GENERATE = "ai.generate"
    NOTIFY = "notify"
    CLIPBOARD = "clipboard"
