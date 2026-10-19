"""The media tools: creating images and videos with a connected service (the "Image creation" and "Video creation"
screens run them as the Creative agent, through the executor and its approvals)."""

from __future__ import annotations

from jarvis.tools.base import Tool

MEDIA_TOOLS: tuple[type[Tool], ...] = ()
