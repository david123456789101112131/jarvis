"""Default least-privilege permission profiles for the five agents.

Each agent only gets the capabilities its job needs. Filesystem scopes use placeholders that
the PermissionManager resolves: {workspace}, {projects}, {user} (Desktop/Documents/…),
{any} (no extra scope beyond the global path rules). Anything outside a profile must be
granted explicitly (by the user, or by JARVIS for one task), and every grant is logged.
"""

from __future__ import annotations

from jarvis.common.enums import AgentId, Capability, RiskLevel

C = Capability

# capability -> roots (empty list = not path-scoped)
AGENT_PROFILES: dict[AgentId, dict[Capability, list[str]]] = {
    AgentId.RESEARCH: {
        C.NET_SEARCH: [],
        C.NET_FETCH: [],
        C.NET_API: [],
        C.FS_READ: ["{workspace}", "{projects}", "{user}"],
        C.FS_WRITE: ["{workspace}", "{projects}"],
        C.MEMORY_READ: [],
        C.MEMORY_WRITE: [],
        C.AI_GENERATE: [],
        C.SYSTEM_INFO: [],
    },
    AgentId.SYSTEM: {
        C.SYSTEM_INFO: [],
        C.PROCESS_LIST: [],
        C.PROCESS_LAUNCH: [],
        C.PROCESS_KILL: [],
        C.SHELL_EXECUTE: [],
        C.SYSTEM_SETTINGS: [],
        C.SYSTEM_POWER: [],
        C.FS_READ: ["{any}"],
        C.FS_WRITE: ["{workspace}", "{projects}", "{user}"],
        C.FS_DELETE: ["{workspace}", "{projects}", "{user}"],
        C.NOTIFY: [],
        C.CLIPBOARD: [],
        C.SCREEN_READ: [],
        C.INPUT_CONTROL: [],
        C.MEMORY_READ: [],
        C.MEMORY_WRITE: [],
        C.AI_GENERATE: [],
    },
    AgentId.DEVELOPMENT: {
        C.FS_READ: ["{workspace}", "{projects}", "{user}"],
        C.FS_WRITE: ["{workspace}", "{projects}"],
        C.FS_DELETE: ["{workspace}", "{projects}"],
        C.CODE_EXECUTE: [],
        C.CODE_INSTALL: [],
        C.SHELL_EXECUTE: [],
        C.PROCESS_LAUNCH: [],
        C.PROCESS_LIST: [],
        C.GIT: [],
        C.NET_FETCH: [],
        C.NET_SEARCH: [],
        C.MEMORY_READ: [],
        C.MEMORY_WRITE: [],
        C.AI_GENERATE: [],
        C.SYSTEM_INFO: [],
    },
    AgentId.CREATIVE: {
        C.FS_READ: ["{workspace}", "{projects}", "{user}"],
        C.FS_WRITE: ["{workspace}", "{projects}"],
        C.AI_GENERATE: [],
        C.NET_FETCH: [],
        C.MEMORY_READ: [],
        C.MEMORY_WRITE: [],
        C.PROCESS_LAUNCH: [],
    },
    AgentId.PLANNER: {
        C.FS_READ: ["{workspace}", "{projects}"],
        C.FS_WRITE: ["{workspace}", "{projects}"],
        C.MEMORY_READ: [],
        C.MEMORY_WRITE: [],
        C.AI_GENERATE: [],
        C.SYSTEM_INFO: [],
    },
}

# Baseline risk of each capability before the per-call assessment.
CAPABILITY_RISK: dict[Capability, RiskLevel] = {
    C.FS_READ: RiskLevel.SAFE,
    C.FS_WRITE: RiskLevel.LOW,
    C.FS_DELETE: RiskLevel.MEDIUM,
    C.PROCESS_LIST: RiskLevel.SAFE,
    C.PROCESS_LAUNCH: RiskLevel.LOW,
    C.PROCESS_KILL: RiskLevel.MEDIUM,
    C.SHELL_EXECUTE: RiskLevel.MEDIUM,
    C.SYSTEM_INFO: RiskLevel.SAFE,
    C.SYSTEM_SETTINGS: RiskLevel.MEDIUM,
    C.SYSTEM_POWER: RiskLevel.HIGH,
    C.NET_SEARCH: RiskLevel.LOW,
    C.NET_FETCH: RiskLevel.LOW,
    C.NET_API: RiskLevel.MEDIUM,
    C.CODE_EXECUTE: RiskLevel.MEDIUM,
    C.CODE_INSTALL: RiskLevel.HIGH,
    C.GIT: RiskLevel.LOW,
    C.MEMORY_READ: RiskLevel.SAFE,
    C.MEMORY_WRITE: RiskLevel.SAFE,
    C.AI_GENERATE: RiskLevel.LOW,
    C.NOTIFY: RiskLevel.SAFE,
    C.CLIPBOARD: RiskLevel.LOW,
    C.SCREEN_READ: RiskLevel.LOW,
    C.INPUT_CONTROL: RiskLevel.LOW,     # each click and keystroke is assessed on its own (jarvis.computer.policy)
}
