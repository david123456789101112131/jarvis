"""Shared pytest fixtures.

Every test runs with `JARVIS_HOME` pointing at a fresh temporary folder, so nothing touches
the real user profile, keyring or workspace.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "jarvis_home"
    home.mkdir()
    monkeypatch.setenv("JARVIS_HOME", str(home))
    for key in list(os.environ):
        if key.startswith("JARVIS__"):
            monkeypatch.delenv(key, raising=False)
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "BRAVE_API_KEY", "TAVILY_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    return home


@pytest.fixture
def jarvis_home(_isolated_home: Path) -> Path:
    return _isolated_home


class Kit:
    """A fully wired tool layer on temporary folders (settings, guard, permissions, executor)."""

    def __init__(self, root: Path) -> None:
        from jarvis.bus import MessageBus
        from jarvis.common.enums import Mode
        from jarvis.config.paths import resolve_paths
        from jarvis.config.settings import Settings
        from jarvis.logs.activity import ActivityLog
        from jarvis.logs.audit import AuditLog
        from jarvis.permissions import ConfirmationBroker, PermissionManager
        from jarvis.security import KillSwitch, PathGuard
        from jarvis.security.redaction import Redactor
        from jarvis.tools import ToolExecutor, ToolRegistry, ToolServices

        self.settings = Settings()
        self.paths = resolve_paths(self.settings.paths).ensure()
        self.user_docs = root / "Documents"
        self.user_docs.mkdir(exist_ok=True)
        self.guard = PathGuard(workspace=self.paths.workspace, projects=self.paths.projects,
                               write_roots=[self.user_docs], protected_roots=[self.paths.data],
                               denied_roots=[root / "denied"])
        self.bus = MessageBus()
        self.redactor = Redactor()
        self.audit = AuditLog(self.paths.logs, redactor=self.redactor)
        self.activity = ActivityLog(self.paths.logs, redactor=self.redactor)
        self.permissions = PermissionManager(self.guard, self.settings.security,
                                             store_file=self.paths.data / "permissions.json",
                                             user_roots=[self.user_docs])
        self.broker = ConfirmationBroker(self.bus, timeout_s=self.settings.security.confirm_timeout_s)
        self.kill_switch = KillSwitch()
        self.registry = ToolRegistry()
        self.services = ToolServices(settings=self.settings, paths=self.paths, guard=self.guard,
                                     redactor=self.redactor, bus=self.bus)
        self.mode = Mode.NORMAL
        self.executor = ToolExecutor(self.registry, self.services, self.permissions, self.broker, self.kill_switch,
                                     audit=self.audit, activity=self.activity, mode_provider=lambda: self.mode)


@pytest.fixture
def kit(tmp_path: Path, _isolated_home: Path) -> Kit:
    return Kit(tmp_path)
