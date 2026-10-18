"""ToolExecutor: the single gate between agents and the machine.

Order of checks for every call (the calling agent cannot skip any of them):
  1. kill switch (global and per agent) and task cancellation
  2. mode (CHAT mode allows read-only tools only)
  3. tool lookup and input validation
  4. tool.plan() -> what exactly will happen (paths, counts, outbound data)
  5. PermissionManager.check() -> capability grants, folder scopes, task restriction,
     path guard, risk assessment, per-agent risk ceiling, secret-leak blocking
  6. confirmation broker when the risk reaches the mode's threshold (timeout = deny)
  7. run with timeout and cancellation; audit + activity log for the outcome
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from jarvis.common.cancel import CancelToken
from jarvis.common.enums import AgentId, Mode, RiskLevel
from jarvis.common.errors import JarvisError, KillSwitchEngaged, TaskCancelled, ToolNotFound
from jarvis.common.i18n import agent_name, t
from jarvis.logs.activity import ActivityLog, Level
from jarvis.logs.audit import AuditEntry, AuditLog
from jarvis.permissions.confirm import ConfirmationBroker, ConfirmationRequest
from jarvis.permissions.manager import PermissionManager
from jarvis.permissions.models import ActionRequest
from jarvis.security.killswitch import KillSwitch
from jarvis.tools.base import ActionPlan, ProgressFn, ToolContext, ToolResult
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.services import ToolServices

log = logging.getLogger(__name__)

_CONFIRM_STATUS_HE = {"denied": "נדחה", "expired": "פג הזמן", "cancelled": "בוטל"}
MAX_SHOWN_PATHS = 20      # paths listed in an approval request; the rest are counted


def agent_display(agent_id: str) -> str:
    """The agent's English name: the activity log's `source` field (the UI shows its own label)."""
    return agent_name(agent_id, "en")


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        services: ToolServices,
        permissions: PermissionManager,
        broker: ConfirmationBroker,
        kill_switch: KillSwitch,
        *,
        audit: AuditLog | None = None,
        activity: ActivityLog | None = None,
        mode_provider: Callable[[], Mode] = lambda: Mode.NORMAL,
    ) -> None:
        self.registry = registry
        self.services = services
        self.permissions = permissions
        self.broker = broker
        self.kill_switch = kill_switch
        self.audit = audit or AuditLog(None)
        self.activity = activity
        self.mode_provider = mode_provider
        self.calls = 0

    async def execute(
        self,
        tool_name: str,
        params: dict[str, Any] | None = None,
        *,
        agent_id: str,
        task_id: str | None = None,
        subtask_id: str | None = None,
        cancel: CancelToken | None = None,
        cwd: Path | None = None,
        progress: ProgressFn | None = None,
        mode: Mode | None = None,
        lang: str = "en",
    ) -> ToolResult:
        """Run one tool call for an agent. Raises only for kill switch / cancellation.

        `lang` is the language of the request: summaries, reasons and log lines a person reads use it."""
        started = time.monotonic()
        cancel = cancel or CancelToken()
        mode = mode or self.mode_provider()
        params = dict(params or {})
        agent = AgentId.parse(agent_id).value
        self.calls += 1

        self.kill_switch.check(agent)
        cancel.raise_if_cancelled()

        try:
            tool = self.registry.get(tool_name)
        except ToolNotFound as exc:
            return ToolResult.fail(str(exc), tool=tool_name)

        if mode == Mode.CHAT and not tool.read_only:
            await self._audit(agent, task_id, tool_name, "", params, RiskLevel.SAFE, "denied", "skipped",
                              t(lang, "מצב שיחה: הפעולות מושבתות", "CHAT mode: actions are disabled"))
            return ToolResult.fail(t(lang, "JARVIS במצב שיחה והפעולות מושבתות. כדי לפעול צריך לעבור למצב רגיל.",
                                     "JARVIS is in CHAT mode; actions are disabled. Switch to NORMAL mode to act."),
                                   denied="mode", tool=tool_name)

        try:
            parsed = tool.Params.model_validate(params)
        except ValidationError as exc:
            errors = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
            return ToolResult.fail(t(lang, f"קלט לא תקין ל-{tool_name}: {errors}", f"Invalid input for {tool_name}: {errors}"),
                                   tool=tool_name)

        ctx = ToolContext(agent_id=agent, task_id=task_id, subtask_id=subtask_id, mode=mode, cancel=cancel,
                          services=self.services, cwd=cwd, progress_fn=progress, lang=lang)

        async def subcall(name: str, sub_params: dict[str, Any]) -> ToolResult:
            return await self.execute(name, sub_params, agent_id=agent, task_id=task_id, subtask_id=subtask_id,
                                      cancel=cancel, cwd=cwd, progress=progress, mode=mode, lang=lang)

        ctx.subcall = subcall
        try:
            plan = await tool.plan(parsed, ctx)
        except JarvisError as exc:
            return ToolResult.fail(exc.message, tool=tool_name, denied="permission" if exc.code == "permission_denied" else None)
        except Exception as exc:
            log.exception("Planning %s failed", tool_name)
            return ToolResult.fail(t(lang, f"{tool_name} לא הצליח להכין את הפעולה: {exc}",
                                     f"{tool_name} could not prepare the action: {exc}"), tool=tool_name)

        caps = plan.capabilities if plan.capabilities is not None else list(tool.capabilities)
        req_params = parsed.model_dump(mode="json")
        if plan.affected_count is not None:
            req_params["_affected_count"] = plan.affected_count
        request = ActionRequest(
            agent_id=agent,
            task_id=task_id,
            tool=tool_name,
            capabilities=caps,
            summary=plan.summary,
            paths=plan.paths,
            risk=max(tool.risk, plan.risk) if plan.risk is not None else tool.risk,
            risk_reasons=plan.risk_reasons,
            outbound=plan.outbound,
            destination=plan.destination,
            always_confirm=tool.always_confirm or plan.always_confirm,
            irreversible=plan.irreversible,
            sends_data=plan.sends_data,
            params=req_params,
            lang=lang,
        )
        decision = self.permissions.check(request, mode)
        name = agent_name(agent, lang)

        if not decision.allowed:
            await self._audit(agent, task_id, tool_name, plan.summary, req_params, decision.risk,
                              "blocked" if decision.blocked else "denied", "skipped", decision.reason)
            await self._activity(t(lang, f"{name}: נחסם — {plan.summary} ({decision.reason})",
                                   f"{name}: blocked — {plan.summary} ({decision.reason})"), agent, task_id, level="warning")
            return ToolResult.fail(t(lang, f"אין הרשאה: {decision.reason}", f"Not allowed: {decision.reason}"),
                                   denied="blocked" if decision.blocked else "permission", tool=tool_name)

        confirmed = False
        if decision.needs_confirmation:
            await self._activity(t(lang, f"{name} ממתין לאישור: {plan.summary}", f"{name} is waiting for approval: {plan.summary}"),
                                 agent, task_id, level="warning", kind="confirm")
            creq = ConfirmationRequest(
                agent_id=agent, task_id=task_id, tool=tool_name, summary=plan.summary, reasons=decision.reasons,
                risk=decision.risk.name.lower(), details=self._approval_details(plan, req_params),
                irreversible=decision.irreversible, always_ask=decision.always_ask, scope=plan.remember_scope,
            )
            approval = asyncio.ensure_future(self.broker.request(creq))
            approved = await self._wait_cancellable(approval, cancel, agent)
            if not approved:
                status = t(lang, _CONFIRM_STATUS_HE.get(creq.status, creq.status), creq.status)
                await self._audit(agent, task_id, tool_name, plan.summary, req_params, decision.risk, "rejected",
                                  "skipped", t(lang, f"האישור {status}", f"confirmation {status}"))
                await self._activity(t(lang, f"{name}: הפעולה לא אושרה — {plan.summary}",
                                       f"{name}: action not approved — {plan.summary}"), agent, task_id, level="warning")
                return ToolResult.fail(t(lang, f"המשתמש לא אישר: {plan.summary} ({status})",
                                         f"The user did not approve: {plan.summary} ({status})"),
                                       denied="confirmation", tool=tool_name)
            confirmed = True

        self.kill_switch.check(agent)
        cancel.raise_if_cancelled()
        self.permissions.consume(decision)
        if decision.risk >= RiskLevel.MEDIUM:
            await self._audit(agent, task_id, tool_name, plan.summary, req_params, decision.risk,
                              "confirmed" if confirmed else "allowed", "pending", "; ".join(decision.reasons))

        timeout = tool.timeout_s
        run = asyncio.ensure_future(tool.run(parsed, ctx))
        try:
            result = await self._wait_cancellable(run, cancel, agent, timeout=timeout)
        except TimeoutError:
            result = ToolResult.fail(t(lang, f"{tool_name} לא הסתיים בזמן ({timeout:.0f} שניות)",
                                       f"{tool_name} timed out after {timeout:.0f}s"))
        except (KillSwitchEngaged, TaskCancelled):
            await self._audit(agent, task_id, tool_name, plan.summary, req_params, decision.risk,
                              "confirmed" if confirmed else "allowed", "error", t(lang, "בוטל", "cancelled"))
            raise
        except JarvisError as exc:
            result = ToolResult.fail(exc.message, denied="permission" if exc.code == "permission_denied" else None)
        except Exception as exc:
            log.exception("Tool %s crashed", tool_name)
            result = ToolResult.fail(t(lang, f"{tool_name} נכשל: {type(exc).__name__}: {exc}",
                                       f"{tool_name} failed: {type(exc).__name__}: {exc}"))

        result.tool = tool_name
        result.duration_ms = int((time.monotonic() - started) * 1000)
        if result.summary:
            result.summary = self.services.redactor.redact_text(result.summary)
        if result.error:
            result.error = self.services.redactor.redact_text(result.error)
        if result.data is not None:
            result.data = self.services.redactor.redact(result.data)
        # Why it failed: the error, else what the tool said (a stopped computer session has only a summary).
        reason = None if result.ok else (result.error or result.summary or t(lang, "בלי סיבה", "no reason given"))
        await self._audit(agent, task_id, tool_name, plan.summary, req_params, decision.risk,
                          "confirmed" if confirmed else "allowed", "ok" if result.ok else "error",
                          "; ".join(decision.reasons), error=reason, duration_ms=result.duration_ms)
        if not tool.read_only or not result.ok:
            text = result.summary or plan.summary
            await self._activity(
                f"{name}: {text}" if result.ok else t(lang, f"{name}: נכשל — {plan.summary}: {reason}",
                                                      f"{name}: failed — {plan.summary}: {reason}"),
                agent, task_id, level="info" if result.ok else "error",
            )
        return result

    def _approval_details(self, plan: ActionPlan, params: dict[str, Any]) -> dict[str, Any]:
        """What the approval dialog shows: the exact paths first, the tool's own details, then the raw input."""
        paths = [use.path for use in plan.paths]
        if len(paths) > MAX_SHOWN_PATHS:
            paths = [*paths[:MAX_SHOWN_PATHS], f"… +{len(paths) - MAX_SHOWN_PATHS}"]
        shown = {k: v for k, v in params.items() if not k.startswith("_")}   # internal hints stay internal
        return {**({"paths": paths} if paths else {}), **plan.details, "params": self.services.redactor.redact(shown)}

    async def _wait_cancellable(self, fut: asyncio.Future[Any], cancel: CancelToken, agent: str,
                                timeout: float | None = None) -> Any:
        """Await `fut`, aborting on task cancellation or kill switch."""
        waiter = asyncio.ensure_future(cancel.wait())
        try:
            deadline = None if timeout is None else time.monotonic() + timeout
            while True:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                step = 0.25 if remaining is None else min(0.25, remaining)
                done, _ = await asyncio.wait({fut, waiter}, timeout=step, return_when=asyncio.FIRST_COMPLETED)
                if fut in done:
                    return fut.result()
                if waiter in done or self.kill_switch.is_engaged(agent):
                    fut.cancel()
                    with contextlib.suppress(BaseException):
                        await asyncio.wait_for(fut, timeout=5)
                    if self.kill_switch.is_engaged(agent):
                        self.kill_switch.check(agent)
                    raise TaskCancelled(cancel.reason or "cancelled")
                if deadline is not None and time.monotonic() >= deadline:
                    fut.cancel()
                    with contextlib.suppress(BaseException):
                        await asyncio.wait_for(fut, timeout=5)
                    raise TimeoutError
        finally:
            waiter.cancel()

    async def _audit(self, agent: str, task_id: str | None, tool: str, action: str, params: dict[str, Any],
                     risk: RiskLevel, decision: str, outcome: str, reason: str, *, error: str | None = None,
                     duration_ms: int | None = None) -> None:
        try:
            cap = ",".join(c.value for c in self.registry.get(tool).capabilities) if self.registry.has(tool) else ""
            await self.audit.record(AuditEntry(
                agent_id=agent, task_id=task_id, tool=tool, capability=cap, action=action, params=params,
                risk=risk.name.lower(), decision=decision, outcome=outcome, reason=reason, error=error,  # type: ignore[arg-type]
                duration_ms=duration_ms,
            ))
        except Exception:  # pragma: no cover
            log.exception("Audit failed")

    async def _activity(self, message: str, agent: str, task_id: str | None, *, level: Level = "info",
                        kind: str = "tool") -> None:
        if self.activity is None:
            return
        try:
            await self.activity.log(message, source=agent_display(agent), level=level, task_id=task_id,
                                    agent_id=agent, kind=kind)
        except Exception:  # pragma: no cover
            log.exception("Activity log failed")
