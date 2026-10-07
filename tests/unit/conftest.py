"""Fixtures for the agent and orchestration tests (see `tests/agent_env.py`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_env import agent_env


@pytest.fixture
async def env(kit, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    async with agent_env(kit, tmp_path, monkeypatch) as e:
        yield e
