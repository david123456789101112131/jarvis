"""Task orchestration: plans (a DAG of subtasks for the five agents) and the orchestrator that
runs them in parallel with retries, timeouts, cancellation, verification and checkpoints."""

from __future__ import annotations

import importlib
from typing import Any

from jarvis.orchestration.models import PlanDraft, PlannedStep, SubTask, TaskPlan, plan_from_draft

# The orchestrator depends on the agents, and the Planner Agent depends on the plan models above,
# so these are imported on first use to keep `jarvis.orchestration.models` free of cycles.
_LAZY = {
    "TaskOrchestrator": "jarvis.orchestration.orchestrator",
    "JarvisTask": "jarvis.orchestration.task",
    "TaskOutcome": "jarvis.orchestration.task",
    "TimelineEntry": "jarvis.orchestration.task",
    "Verifier": "jarvis.orchestration.verify",
}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        return getattr(importlib.import_module(_LAZY[name]), name)
    raise AttributeError(f"module 'jarvis.orchestration' has no attribute {name!r}")


__all__ = ["JarvisTask", "PlanDraft", "PlannedStep", "SubTask", "TaskOrchestrator", "TaskOutcome", "TaskPlan",
           "TimelineEntry", "Verifier", "plan_from_draft"]
