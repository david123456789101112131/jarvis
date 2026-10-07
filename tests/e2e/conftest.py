"""Fixtures for the end-to-end tests: real JARVIS processes on the test's temporary JARVIS_HOME."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest
from e2e_support import Jarvis


@pytest.fixture
async def launch(jarvis_home: Path) -> AsyncIterator[Callable[[], Awaitable[Jarvis]]]:
    """Start JARVIS (again) on this test's JARVIS_HOME; every process is gone when the test ends."""
    started: list[Jarvis] = []

    async def start() -> Jarvis:
        j = Jarvis(jarvis_home)
        started.append(j)
        return await j.start()

    try:
        yield start
    finally:
        for j in started:
            await j.close()
