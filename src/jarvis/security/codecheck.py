"""Is a Python snippet only a calculation? Then it may run without asking.

`run_python` lets the agents compute things (statistics, dates, text processing). Code that only
uses a small set of standard modules for maths and text, and none of Python's ways to reach the
file system, the network, other programs or the interpreter's internals, cannot harm the
computer. Everything else needs the user's approval.

The check is an allow-list on the syntax tree, so it errs on the side of asking:
* imports only from `SAFE_MODULES` (no star imports, no relative imports, no submodules other than
  `collections.abc`);
* no names that open files or run code (`open`, `exec`, `eval`, `getattr`, `__import__`, …);
* no attribute that starts with "_" (blocks `().__class__.__subclasses__()` and `random._os`), also
  where a `match` pattern reads attributes by name (`case object(__class__=c)`);
* no attribute named after a module (blocks `typing.sys` or `enum.bltns`, which re-export
  `sys` and `builtins`), and no frame / code / generator internals (`gi_frame.f_builtins`);
* no string or bytes containing "__" (blocks building dunder names for later lookup).
"""

from __future__ import annotations

import ast
import importlib
import sys
import types
from functools import cache

# Maths, dates and text only. Modules that look up attributes from strings at run time
# (operator.attrgetter, string.Formatter.get_field) or evaluate annotations (typing,
# dataclasses) are deliberately missing.
SAFE_MODULES = frozenset({
    "math", "cmath", "statistics", "decimal", "fractions", "random", "json", "re", "datetime", "time", "calendar",
    "itertools", "functools", "collections", "heapq", "bisect", "textwrap", "unicodedata", "copy", "pprint",
    "numbers", "hashlib", "base64", "binascii", "struct", "difflib", "uuid", "colorsys", "secrets", "zoneinfo", "array",
})

BLOCKED_NAMES = frozenset({
    "open", "exec", "eval", "compile", "input", "breakpoint", "globals", "locals", "vars", "getattr", "setattr",
    "delattr", "help", "exit", "quit", "memoryview",
})

_INTERNAL_PREFIXES = ("f_", "gi_", "cr_", "ag_", "tb_", "co_", "func_", "im_")
# getnode/uuid1 may start `ip`/`ifconfig` to find the network card's address (outside Windows).
_INTERNAL_NAMES = frozenset({"mro", "get_field", "vformat", "get_value", "attrgetter", "methodcaller", "load_module",
                             "exec_module", "system", "popen", "spawn", "getnode", "uuid1"})
_SAFE_SUBMODULES = frozenset({"collections.abc"})
MAX_CODE_CHARS = 20_000


@cache
def _blocked_attributes() -> frozenset[str]:
    """Module names (the standard library's and anything the safe modules re-export) as attributes."""
    names = set(getattr(sys, "stdlib_module_names", ())) - SAFE_MODULES
    for name in SAFE_MODULES:
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        names.update(attr for attr in dir(module) if isinstance(getattr(module, attr, None), types.ModuleType))
    return frozenset(names - SAFE_MODULES)


def _internal(name: str, blocked: frozenset[str]) -> bool:
    """An attribute name that reaches past maths and text (see the module docstring)."""
    return name.startswith("_") or name in blocked or name in _INTERNAL_NAMES or name.startswith(_INTERNAL_PREFIXES)


def _safe_module(name: str) -> bool:
    return name in SAFE_MODULES or name in _SAFE_SUBMODULES


def pure_python_problem(code: str) -> str | None:
    """None when the code only computes; otherwise a short English reason why it needs approval."""
    if len(code) > MAX_CODE_CHARS:
        return "the code is long"
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return "the code is not valid Python"
    blocked = _blocked_attributes()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _safe_module(alias.name):
                    return f"imports {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.level or not _safe_module(node.module or ""):
                return f"imports {node.module or 'a local module'}"
            for alias in node.names:
                name = alias.name
                if name == "*":
                    return "imports everything from a module"
                # `from json import codecs` reaches what `json.codecs` would: judged like an attribute.
                if _internal(name, blocked) or name in BLOCKED_NAMES:
                    return f"imports {name} from {node.module}"
        elif isinstance(node, ast.Name):
            if node.id in BLOCKED_NAMES or node.id.startswith("__"):
                return f"uses {node.id}"
        elif isinstance(node, ast.Attribute):
            if _internal(node.attr, blocked):
                return f"uses .{node.attr}"
        elif isinstance(node, ast.MatchClass):
            # `case object(__class__=c)` reads the attribute like `subject.__class__` would.
            for attr in node.kwd_attrs:
                if _internal(attr, blocked):
                    return f"uses .{attr}"
        elif isinstance(node, ast.Constant) and isinstance(node.value, str | bytes) and (
                "__" in node.value if isinstance(node.value, str) else b"__" in node.value):
            return "builds a hidden attribute name"
    return None


__all__ = ["BLOCKED_NAMES", "SAFE_MODULES", "pure_python_problem"]
