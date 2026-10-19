"""One previewed on-screen step, done only after the user approves it (the "Apps & tools" screen)."""

from __future__ import annotations

from jarvis.tools.base import Tool

STEP_TOOLS: tuple[type[Tool], ...] = ()
