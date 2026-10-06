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
