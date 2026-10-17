"""Error recovery rules: what to do when a subtask fails.

The orchestrator never loops blindly. For every failure it records the error, then picks one of:

* **retry** — the same agent again after an exponential backoff, with the problem passed back as
  feedback (and without the AI model when the model was what failed and the step can run offline);
* **help** — a Research Agent step looks for a fix first, then the step runs again with its findings
  (used for development work);
* **fallback** — another agent that can do the same kind of work takes the step over;
* **halt** — the agent was stopped or its kill switch engaged: the task pauses (interrupted) and
  can be resumed later without redoing finished steps;
* **fail** — retrying cannot help (the user said no, the input is wrong) or the limits are reached.

Limits: a retry limit per step, the same error at most `max_same_error` times, and a dispatch
budget per task.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from jarvis.common.enums import AgentId, ResultStatus

Action = Literal["retry", "help", "fallback", "halt", "fail"]

# The agent was switched off or its kill switch is engaged: wait for the user, do not burn retries.
HALT_CODES = frozenset({"kill_switch", "agent_stopped"})
# Another attempt with the same input cannot succeed (or would pester the user after a "no").
NO_RETRY_CODES = frozenset({
    "refusal", "permission_denied", "confirmation_denied", "security_violation", "project_not_found",
    "need_location", "needs_token", "missing_modules", "ai_required", "invalid_tool_input", "tool_not_found",
    "plan_error", "user_stopped", "computer_unavailable",
})
# The AI model failed: the next attempt runs the step's offline skill when it has one.
MODEL_CODES = frozenset({"ai_unavailable", "ai_error"})
# Failures that another agent may get past (generic file / command work done by the model loop).
FALLBACK_AGENTS: dict[AgentId, AgentId] = {
    AgentId.SYSTEM: AgentId.DEVELOPMENT,
    AgentId.DEVELOPMENT: AgentId.SYSTEM,
}


@dataclass(frozen=True)
class Decision:
    action: Action
    delay_s: float = 0.0
    offline: bool = False
    reason: str = ""


def failure_code(status: ResultStatus, code: str | None, denied: list[str] | None = None) -> str | None:
    """The code that classifies a failed result (a denied action counts as the user's "no")."""
    if code:
        return code
    if denied and status == ResultStatus.FAILED:
        return "permission_denied"
    return None


_PATH_RE = re.compile(r"(?:[a-zA-Z]:[\\/]|\\\\|/)[^\s'\"<>|]+")
_HEX_RE = re.compile(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{8,}\b")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def fingerprint(error: str) -> str:
    """Normalize an error so the same failure is recognised again (paths, ids and numbers vary)."""
    text = (error or "").lower()
    text = _PATH_RE.sub("<path>", text)
    text = _HEX_RE.sub("<id>", text)
    text = _NUM_RE.sub("#", text)
    return " ".join(text.split())[:240]


def same_error_count(history: list[str], error: str) -> int:
    """How many times this error appears in the history (the history includes the latest failure)."""
    fp = fingerprint(error)
    return sum(1 for item in history if fingerprint(item) == fp)


def backoff(attempt: int, base_s: float, cap_s: float) -> float:
    """Delay before attempt `attempt + 1`: base, 2×base, 4×base… capped."""
    if base_s <= 0:
        return 0.0
    return float(min(cap_s, base_s * (2 ** max(0, attempt - 1))))


def decide(
    *,
    code: str | None,
    error: str,
    attempts: int,
    history: list[str],
    max_retries: int,
    max_same_error: int,
    backoff_s: float,
    backoff_max_s: float,
    retryable: bool | None = None,
    can_run_offline: bool = False,
    can_help: bool = False,
    can_fallback: bool = False,
) -> Decision:
    """Choose the recovery step for a failed attempt (`attempts` = attempts made so far)."""
    if code in HALT_CODES:
        return Decision("halt", reason=code or "")
    if code in NO_RETRY_CODES:
        return Decision("fail", reason=code or "")
    offline = False
    if code in MODEL_CODES:
        if can_run_offline:
            offline = True
        elif code == "ai_unavailable" or retryable is False:
            return Decision("fail", reason="the AI model is needed and not available")
    repeated = same_error_count(history, error) >= max(1, max_same_error)
    exhausted = attempts >= 1 + max(0, max_retries)
    if repeated or exhausted:
        if can_fallback:
            return Decision("fallback", reason="same error again" if repeated else "retries used up")
        return Decision("fail", reason="same error again" if repeated else "retries used up")
    if can_help and not offline:
        return Decision("help", reason="look for a fix first")
    return Decision("retry", delay_s=backoff(attempts, backoff_s, backoff_max_s), offline=offline,
                    reason="model failed: retry offline" if offline else "retry")


def dispatch_budget(subtasks: int, *, max_retries: int, max_followups: int, autonomous: bool = False) -> int:
    """How many agent assignments one task may make in total: generous for real recovery (retries,
    a helper, a fallback agent per step), but finite, so a task can never loop forever."""
    per_step = 2 * (1 + max(0, max_retries)) + 1
    budget = max(1, subtasks) * per_step + max(0, max_followups) * 4 + 4
    if autonomous:
        budget *= 2
    return min(budget, 200)


__all__ = ["FALLBACK_AGENTS", "HALT_CODES", "MODEL_CODES", "NO_RETRY_CODES", "Decision", "backoff", "decide",
           "dispatch_budget", "failure_code", "fingerprint", "same_error_count"]
