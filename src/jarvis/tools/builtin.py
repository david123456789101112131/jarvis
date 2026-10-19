"""The built-in tool set and the registry factory."""

from __future__ import annotations

from collections.abc import Iterable

from jarvis.tools.apps import APP_TOOLS
from jarvis.tools.base import Tool
from jarvis.tools.dev import DEV_TOOLS
from jarvis.tools.devmode_tools import DEVMODE_TOOLS
from jarvis.tools.fs import FS_TOOLS
from jarvis.tools.local_media_tools import LOCAL_MEDIA_TOOLS
from jarvis.tools.media_tools import MEDIA_TOOLS
from jarvis.tools.memory_tools import MEMORY_TOOLS
from jarvis.tools.organize import ORGANIZE_TOOLS
from jarvis.tools.project_tools import PROJECT_TOOLS
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.shell import SHELL_TOOLS
from jarvis.tools.step_tools import STEP_TOOLS
from jarvis.tools.system import SYSTEM_TOOLS
from jarvis.tools.web import WEB_TOOLS


def builtin_tool_classes() -> list[type[Tool]]:
    from jarvis.tools.computer import COMPUTER_TOOLS

    classes: list[type[Tool]] = [*FS_TOOLS, *APP_TOOLS, *SHELL_TOOLS, *SYSTEM_TOOLS, *WEB_TOOLS, *DEV_TOOLS,
                                 *MEMORY_TOOLS, *ORGANIZE_TOOLS, *PROJECT_TOOLS, *COMPUTER_TOOLS, *MEDIA_TOOLS, *STEP_TOOLS,
                                 *DEVMODE_TOOLS, *LOCAL_MEDIA_TOOLS]
    try:
        from jarvis.tools.creative_tools import CREATIVE_TOOLS

        classes += list(CREATIVE_TOOLS)
    except ImportError:  # pragma: no cover - optional generators (Pillow / python-pptx) missing
        pass
    return classes


def create_registry(extra: Iterable[Tool] = ()) -> ToolRegistry:
    registry = ToolRegistry(cls() for cls in builtin_tool_classes())
    for tool in extra:
        registry.register(tool)
    return registry
