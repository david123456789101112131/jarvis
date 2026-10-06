"""PermissionManager: who may do what, where, and for which task.

Supports everything the spec asks of the permission layer:
* grant / revoke permissions (per agent and capability, optional expiry and use limit),
* restrict an agent to one task, restrict an agent to specific folders,
* limit dangerous actions (per-agent risk ceiling, confirmation thresholds per mode),
* every decision is returned with reasons so the executor can audit it.

Policy grants (the default least-privilege profiles) are rebuilt at start-up; grants made by
the user or by JARVIS are persisted to `<data>/permissions.json`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Iterable
from datetime import timedelta
from pathlib import Path
from typing import Any

from jarvis.common.clock import utcnow
from jarvis.common.enums import AgentId, Capability, Mode, RiskLevel
from jarvis.config.settings import SecuritySettings
from jarvis.permissions.models import ActionRequest, Decision, Grant, GrantSource
from jarvis.permissions.policies import AGENT_PROFILES
from jarvis.permissions.risk import RiskAssessor
from jarvis.security.path_guard import PathGuard, default_user_write_roots, is_within, normalize_path

log = logging.getLogger(__name__)

_ACCESS_CAP = {"read": Capability.FS_READ, "write": Capability.FS_WRITE, "delete": Capability.FS_DELETE}


class PermissionManager:
    def __init__(
        self,
        guard: PathGuard,
        settings: SecuritySettings | None = None,
        *,
        store_file: Path | None = None,
        assessor: RiskAssessor | None = None,
        user_roots: Iterable[Path] | None = None,
    ) -> None:
        self.guard = guard
        self.settings = settings or SecuritySettings()
        self.assessor = assessor or RiskAssessor(guard, broad_delete_threshold=self.settings.broad_delete_threshold)
        self.store_file = store_file
        self.user_roots = [normalize_path(p) for p in (user_roots if user_roots is not None else default_user_write_roots())]
        self._lock = threading.RLock()
        self._grants: dict[str, Grant] = {}
        self._task_restrictions: dict[str, str] = {}          # agent -> only this task id
        self._folder_scopes: dict[str, list[str]] = {}        # agent -> only these roots
        self._load_policies()
        self._load_store()

    # ------------------------------------------------------------------ grants
    def _load_policies(self) -> None:
        for agent, caps in AGENT_PROFILES.items():
            for cap, roots in caps.items():
                g = Grant(agent_id=agent.value, capability=cap, roots=list(roots), source="policy",
                          reason="default profile")
                self._grants[g.id] = g

    def grant(
        self,
        agent_id: str,
        capability: Capability | str,
        *,
        roots: Iterable[str | Path] = (),
        task_id: str | None = None,
        ttl_s: float | None = None,
        max_uses: int | None = None,
        source: GrantSource = "user",
        reason: str = "",
    ) -> Grant:
        agent = agent_id if agent_id == "*" else AgentId.parse(agent_id).value
        g = Grant(
            agent_id=agent,
            capability=Capability(capability),
            roots=[str(normalize_path(r)) if not str(r).startswith("{") else str(r) for r in roots],
            task_id=task_id,
            expires_at=(utcnow() + timedelta(seconds=ttl_s)) if ttl_s else None,
            max_uses=max_uses,
            source=source,
            reason=reason,
        )
        with self._lock:
            self._grants[g.id] = g
        self._save_store()
        log.info("Granted %s to %s (roots=%s task=%s)", g.capability, g.agent_id, g.roots, g.task_id)
        return g

    def revoke(self, grant_id: str) -> bool:
        with self._lock:
            removed = self._grants.pop(grant_id, None)
        if removed:
            self._save_store()
        return removed is not None

    def revoke_capability(self, agent_id: str, capability: Capability | str) -> int:
        agent = AgentId.parse(agent_id).value
        cap = Capability(capability)
        with self._lock:
            ids = [g.id for g in self._grants.values() if g.agent_id == agent and g.capability == cap]
            for gid in ids:
                self._grants.pop(gid, None)
        self._save_store()
        return len(ids)

    def revoke_task_grants(self, task_id: str) -> int:
        with self._lock:
            ids = [g.id for g in self._grants.values() if g.task_id == task_id]
            for gid in ids:
                self._grants.pop(gid, None)
        if ids:
            self._save_store()
        return len(ids)

    def reset_agent(self, agent_id: str) -> None:
        """Back to the default profile: drop user/JARVIS grants and restrictions for the agent."""
        agent = AgentId.parse(agent_id).value
        with self._lock:
            for gid in [g.id for g in self._grants.values() if g.agent_id == agent and g.source != "policy"]:
                self._grants.pop(gid, None)
            policy_caps = AGENT_PROFILES[AgentId(agent)]
            present = {g.capability for g in self._grants.values() if g.agent_id == agent and g.source == "policy"}
            for cap, roots in policy_caps.items():
                if cap not in present:
                    g = Grant(agent_id=agent, capability=cap, roots=list(roots), source="policy", reason="default profile")
                    self._grants[g.id] = g
            self._task_restrictions.pop(agent, None)
            self._folder_scopes.pop(agent, None)
        self._save_store()

    def grants(self, agent_id: str | None = None, *, include_expired: bool = False) -> list[Grant]:
        with self._lock:
            items = list(self._grants.values())
        if agent_id is not None:
            agent = AgentId.parse(agent_id).value
            items = [g for g in items if g.agent_id in (agent, "*")]
        if not include_expired:
            items = [g for g in items if not g.is_expired()]
        return items

    def capabilities(self, agent_id: str, task_id: str | None = None) -> list[str]:
        agent = AgentId.parse(agent_id).value
        caps = {g.capability.value for g in self.grants(agent) if g.task_id in (None, task_id)}
        return sorted(caps)

    # ------------------------------------------------------------------ restrictions
    def restrict_to_task(self, agent_id: str, task_id: str | None) -> None:
        agent = AgentId.parse(agent_id).value
        with self._lock:
            if task_id is None:
                self._task_restrictions.pop(agent, None)
            else:
                self._task_restrictions[agent] = task_id
        self._save_store()

    def restrict_folders(self, agent_id: str, roots: Iterable[str | Path] | None) -> None:
        agent = AgentId.parse(agent_id).value
        with self._lock:
            if roots is None:
                self._folder_scopes.pop(agent, None)
            else:
                self._folder_scopes[agent] = [str(normalize_path(r)) for r in roots]
        self._save_store()

    def set_risk_ceiling(self, agent_id: str, level: RiskLevel | int) -> None:
        agent = AgentId.parse(agent_id).value
        self.settings.agent_risk_ceilings[agent] = int(level)

    def risk_ceiling(self, agent_id: str) -> RiskLevel:
        return RiskLevel(self.settings.agent_risk_ceilings.get(agent_id, int(RiskLevel.HIGH)))

    def confirm_threshold(self, mode: Mode | str) -> RiskLevel:
        key = Mode(mode).value
        return RiskLevel(self.settings.confirm_thresholds.get(key, int(RiskLevel.HIGH)))

    # ------------------------------------------------------------------ decisions
    def _resolve_roots(self, tokens: Iterable[str]) -> list[Path] | None:
        """Grant roots -> concrete folders. None means "no scope beyond the global path rules"."""
        out: list[Path] = []
        for t in tokens:
            if t == "{any}":
                return None
            if t == "{workspace}":
                out.append(self.guard.workspace)
            elif t == "{projects}":
                out.append(self.guard.projects)
            elif t == "{user}":
                out.extend(self.user_roots)
            else:
                out.append(normalize_path(t))
        return out

    def _matching_grants(self, agent: str, cap: Capability, task_id: str | None) -> list[Grant]:
        return [g for g in self.grants(agent) if g.applies_to(agent, cap, task_id)]

    def check(self, req: ActionRequest, mode: Mode | str = Mode.NORMAL) -> Decision:
        agent = req.agent_id
        reasons: list[str] = []
        used: list[str] = []

        restricted = self._task_restrictions.get(agent)
        if restricted is not None and req.task_id != restricted:
            return Decision(allowed=False, reasons=[f"{agent} is restricted to task {restricted}"])

        for cap in req.capabilities:
            matches = self._matching_grants(agent, cap, req.task_id)
            if not matches:
                return Decision(allowed=False, reasons=[f"{agent} agent has no permission for '{cap.value}'"])
            used.extend(g.id for g in matches[:1])

        scope = self._folder_scopes.get(agent)
        for use in req.paths:
            path = normalize_path(use.path)
            if self.guard.is_denied(path):
                return Decision(allowed=False, blocked=True, reasons=[f"access to protected location denied: {path}"])
            cap = _ACCESS_CAP[use.access]
            matches = self._matching_grants(agent, cap, req.task_id)
            if not matches:
                return Decision(allowed=False, reasons=[f"{agent} agent has no '{cap.value}' permission"])
            ok_grant = None
            for g in matches:
                roots = self._resolve_roots(g.roots)
                if roots is None or any(is_within(path, r) for r in roots):
                    ok_grant = g
                    break
            if ok_grant is None:
                return Decision(allowed=False, reasons=[f"{path} is outside the folders {agent} may {use.access}"])
            if use.access in ("write", "delete"):
                extra = self._resolve_roots(ok_grant.roots) or []
                if ok_grant.source != "policy":
                    if not self.guard.can_write(path, extra_roots=extra):
                        return Decision(allowed=False, reasons=[f"writing to {path} is not allowed"])
                elif not self.guard.can_write(path):
                    return Decision(allowed=False, reasons=[f"writing to {path} is outside the allowed folders"])
            elif not self.guard.can_read(path):
                return Decision(allowed=False, reasons=[f"reading {path} is not allowed"])
            if scope is not None and not any(is_within(path, normalize_path(r)) for r in scope):
                return Decision(allowed=False, reasons=[f"{agent} is restricted to folders {scope}"])
            used.append(ok_grant.id)

        assessment = self.assessor.assess(req)
        risk = assessment.risk
        reasons.extend(r for r in assessment.reasons if r)
        if assessment.blocked:
            return Decision(allowed=False, blocked=True, risk=risk, reasons=reasons or ["blocked by security policy"])
        ceiling = self.risk_ceiling(agent)
        if risk > ceiling:
            return Decision(
                allowed=False, blocked=True, risk=risk,
                reasons=[*reasons, f"risk {risk.name} exceeds what the {agent} agent may do ({ceiling.name})"],
            )
        threshold = self.confirm_threshold(mode)
        needs = req.always_confirm or risk >= threshold or risk >= RiskLevel.CRITICAL
        if Mode(mode) == Mode.AUTONOMOUS and self.settings.autonomous_preapprove_high and risk == RiskLevel.HIGH \
                and not req.always_confirm:
            needs = False
        return Decision(allowed=True, needs_confirmation=needs, risk=risk, reasons=reasons, grant_ids=sorted(set(used)))

    def consume(self, decision: Decision) -> None:
        """Count one use against every grant that allowed the action (for max_uses grants)."""
        with self._lock:
            for gid in decision.grant_ids:
                g = self._grants.get(gid)
                if g is not None and g.max_uses is not None:
                    g.uses += 1
        if any(self._grants.get(gid) and self._grants[gid].max_uses for gid in decision.grant_ids):
            self._save_store()

    # ------------------------------------------------------------------ persistence / views
    def snapshot(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for agent in AgentId:
            gs = self.grants(agent.value)
            out[agent.value] = {
                "capabilities": sorted({g.capability.value for g in gs if g.task_id is None}),
                "grants": [g.model_dump(mode="json") for g in gs if g.source != "policy"],
                "restricted_to_task": self._task_restrictions.get(agent.value),
                "folder_scope": self._folder_scopes.get(agent.value),
                "risk_ceiling": self.risk_ceiling(agent.value).name,
            }
        return out

    def _load_store(self) -> None:
        if not self.store_file or not self.store_file.exists():
            return
        try:
            data = json.loads(self.store_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("Could not read %s; starting with default permissions", self.store_file)
            return
        for raw in data.get("grants", []):
            try:
                g = Grant.model_validate(raw)
            except ValueError:
                continue
            if not g.is_expired():
                self._grants[g.id] = g
        self._task_restrictions.update(data.get("task_restrictions", {}))
        self._folder_scopes.update(data.get("folder_scopes", {}))
        for agent, level in (data.get("risk_ceilings") or {}).items():
            self.settings.agent_risk_ceilings[agent] = int(level)

    def _save_store(self) -> None:
        if not self.store_file:
            return
        with self._lock:
            data = {
                "grants": [g.model_dump(mode="json") for g in self._grants.values() if g.source != "policy"],
                "task_restrictions": dict(self._task_restrictions),
                "folder_scopes": dict(self._folder_scopes),
                "risk_ceilings": dict(self.settings.agent_risk_ceilings),
            }
        try:
            self.store_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.store_file)
        except OSError:
            log.exception("Could not save permissions")
