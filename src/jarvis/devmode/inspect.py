"""What a project is, found without running it or asking an AI.

`inspect_project()` walks the folder the user picked once (with a file and time budget, skipping
dependency, build and tool folders) and reads only small manifest files: the languages by file count, the
manifests and what they declare (dependencies, scripts), the frameworks those name, the commands that exist
(tests, run, build, lint — none of them is run here), the tools this computer has for them, the coding style
(indentation, quotes, line endings, line length), the top of the README and the folder layout.

Files that may hold secrets (.env, private keys, credential files…) are never opened: they are listed as
"not read", and the AI never gets them.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10
    tomllib = None  # type: ignore[assignment]

from jarvis.tools.dev import detect_test_command, venv_python

IS_WINDOWS = sys.platform == "win32"

MAX_FILES = 20_000           # files looked at in one inspection
MAX_SECONDS = 4.0            # …and the time it may take
MAX_TEXT_BYTES = 400_000     # a text file bigger than this is not read for the AI or for matching
README_CHARS = 2_000

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env", ".env", "$recycle.bin",
    "system volume information", ".cache", ".idea", ".vs", ".vscode-test", "dist", "build", "out", "target",
    "bin", "obj", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".nox", "htmlcov", "coverage",
    ".next", ".nuxt", ".svelte-kit", ".parcel-cache", ".turbo", ".expo", ".gradle", ".terraform", "pods",
    "deriveddata", "site-packages", "vendor", ".eggs", "wheels", ".ipynb_checkpoints", ".angular",
}

# Files that may hold passwords, keys or tokens: never opened, never sent.
SECRET_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks", "*.ppk", "id_rsa*",
    "id_dsa*", "id_ecdsa*", "id_ed25519*", "credentials*", "*credentials.json", "secret.*", "secrets.*",
    "*.secret", ".npmrc", ".pypirc", ".netrc", "_netrc", ".git-credentials", "*.kdbx", "service-account*.json",
    "*-adminsdk-*.json", "*.tfvars", "*.tfstate", "*.sqlite", "*.db", "token.json", "*.token",
)
SAFE_EXAMPLES = (".env.example", ".env.sample", ".env.template", ".env.dist")

LANGUAGES = {
    ".py": "Python", ".pyw": "Python", ".ipynb": "Jupyter", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".mts": "TypeScript", ".cts": "TypeScript", ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript",
    ".cjs": "JavaScript", ".vue": "Vue", ".svelte": "Svelte", ".html": "HTML", ".htm": "HTML", ".css": "CSS",
    ".scss": "SCSS", ".sass": "SCSS", ".less": "Less", ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin",
    ".cs": "C#", ".fs": "F#", ".vb": "Visual Basic", ".go": "Go", ".rs": "Rust", ".rb": "Ruby", ".php": "PHP",
    ".c": "C", ".h": "C/C++", ".cpp": "C++", ".cc": "C++", ".cxx": "C++", ".hpp": "C++", ".swift": "Swift",
    ".m": "Objective-C", ".dart": "Dart", ".lua": "Lua", ".r": "R", ".jl": "Julia", ".scala": "Scala",
    ".sh": "Shell", ".bash": "Shell", ".ps1": "PowerShell", ".psm1": "PowerShell", ".bat": "Batch",
    ".cmd": "Batch", ".sql": "SQL", ".gd": "GDScript", ".asm": "Assembly", ".ahk": "AutoHotkey",
}
DATA_FORMATS = {".json": "JSON", ".yml": "YAML", ".yaml": "YAML", ".toml": "TOML", ".xml": "XML", ".md": "Markdown",
                ".rst": "reStructuredText", ".ini": "INI", ".cfg": "INI", ".csv": "CSV", ".txt": "Text"}
TEXT_EXT = set(LANGUAGES) | set(DATA_FORMATS) | {
    ".env.example", ".editorconfig", ".gitignore", ".prettierrc", ".eslintrc", ".babelrc", ".svg", ".graphql",
    ".gql", ".proto", ".tf", ".hcl", ".gradle", ".properties", ".lock", ".conf", ".dockerfile", ".mk", ".j2",
    ".jinja", ".hbs", ".ejs", ".pug", ".twig", ".liquid", ".astro", ".mdx", ".tex", ".ui", ".qml", ".xaml",
    ".csproj", ".sln", ".plist", ".gemspec", ".rake", ".pyi", ".pxd", ".pyx",
}
TEXT_NAMES = {"dockerfile", "makefile", "procfile", "gemfile", "rakefile", "jenkinsfile", "license", "readme",
              "requirements.txt", "pipfile", ".gitignore", ".editorconfig", ".prettierrc", ".eslintrc", "cmakelists.txt"}

MANIFESTS = (
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "requirements-dev.txt", "Pipfile",
    "environment.yml", "package.json", "tsconfig.json", "vite.config.ts", "vite.config.js", "next.config.js",
    "next.config.mjs", "angular.json", "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "build.gradle.kts",
    "composer.json", "Gemfile", "CMakeLists.txt", "Makefile", "Dockerfile", "docker-compose.yml",
    "compose.yaml", "pytest.ini", "tox.ini", ".editorconfig", "manage.py", "pubspec.yaml",
)

# dependency name -> the framework or tool it means
JS_FRAMEWORKS = {
    "react": "React", "next": "Next.js", "vue": "Vue", "nuxt": "Nuxt", "svelte": "Svelte",
    "@sveltejs/kit": "SvelteKit", "@angular/core": "Angular", "solid-js": "Solid", "preact": "Preact",
    "vite": "Vite", "webpack": "webpack", "express": "Express", "fastify": "Fastify", "koa": "Koa",
    "@nestjs/core": "NestJS", "electron": "Electron", "react-native": "React Native", "expo": "Expo",
    "three": "Three.js", "tailwindcss": "Tailwind CSS", "typescript": "TypeScript", "jest": "Jest",
    "vitest": "Vitest", "mocha": "Mocha", "@playwright/test": "Playwright", "cypress": "Cypress",
    "eslint": "ESLint", "prettier": "Prettier", "socket.io": "Socket.IO", "discord.js": "discord.js",
    "phaser": "Phaser", "jquery": "jQuery", "bootstrap": "Bootstrap", "prisma": "Prisma", "mongoose": "Mongoose",
}
PY_FRAMEWORKS = {
    "fastapi": "FastAPI", "flask": "Flask", "django": "Django", "pytest": "pytest", "pydantic": "Pydantic",
    "sqlalchemy": "SQLAlchemy", "numpy": "NumPy", "pandas": "pandas", "torch": "PyTorch",
    "tensorflow": "TensorFlow", "pyside6": "PySide6", "pyqt5": "PyQt5", "pyqt6": "PyQt6", "streamlit": "Streamlit",
    "requests": "requests", "httpx": "httpx", "aiohttp": "aiohttp", "selenium": "Selenium",
    "playwright": "Playwright", "scrapy": "Scrapy", "discord.py": "discord.py",
    "python-telegram-bot": "python-telegram-bot", "pygame": "pygame", "kivy": "Kivy", "opencv-python": "OpenCV",
    "pillow": "Pillow", "transformers": "Transformers", "uvicorn": "Uvicorn", "celery": "Celery",
    "customtkinter": "CustomTkinter", "pywebview": "pywebview", "ruff": "Ruff", "mypy": "mypy", "black": "Black",
    "openai": "OpenAI SDK", "anthropic": "Anthropic SDK", "beautifulsoup4": "Beautiful Soup", "gradio": "Gradio",
}


@dataclass
class FileInfo:
    path: str                  # relative, with forward slashes
    size: int
    mtime: float
    language: str = ""
    text: bool = False         # a text file JARVIS may read
    secret: bool = False       # may hold secrets: never read


@dataclass
class ProjectScan:
    root: Path
    files: list[FileInfo] = field(default_factory=list)
    truncated: bool = False
    seconds: float = 0.0
    dirs: Counter[str] = field(default_factory=Counter)   # top-level folder -> files in it


def is_secret_name(name: str) -> bool:
    low = name.lower()
    if low in SAFE_EXAMPLES:
        return False
    return any(fnmatch.fnmatch(low, pattern) for pattern in SECRET_PATTERNS)


def is_text_name(name: str) -> bool:
    low = name.lower()
    if low in TEXT_NAMES:
        return True
    return Path(low).suffix in TEXT_EXT or low.startswith(".env.")


def scan(root: Path, *, max_files: int = MAX_FILES, max_seconds: float = MAX_SECONDS) -> ProjectScan:
    """Every file under `root` worth knowing about (skipping dependency, build and tool folders)."""
    out = ProjectScan(root=root)
    started = time.monotonic()
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if (d.lower() not in SKIP_DIRS and not d.startswith(".")) or d == ".github")
        rel_dir = Path(folder).relative_to(root)
        top = rel_dir.parts[0] if rel_dir.parts else ""
        for name in sorted(files):
            if len(out.files) >= max_files or time.monotonic() - started > max_seconds:
                out.truncated = True
                break
            p = Path(folder) / name
            try:
                st = p.stat()
            except OSError:
                continue
            rel = (rel_dir / name).as_posix()
            suffix = Path(name.lower()).suffix
            secret = is_secret_name(name)
            out.files.append(FileInfo(path=rel, size=st.st_size, mtime=st.st_mtime,
                                      language=LANGUAGES.get(suffix, ""), secret=secret,
                                      text=not secret and is_text_name(name) and st.st_size <= MAX_TEXT_BYTES))
            out.dirs[top or "."] += 1
        if out.truncated:
            break
    out.seconds = time.monotonic() - started
    return out


_CACHE: dict[str, tuple[float, tuple[Any, ...], ProjectScan]] = {}
CACHE_S = 60.0               # a scan is reused this long while the folder's top level is unchanged
CACHE_KEEP = 8


def _signature(root: Path) -> tuple[Any, ...]:
    """Cheap fingerprint of the project's top level: names, sizes and times (a new or saved file changes it)."""
    out = []
    try:
        for entry in sorted(os.scandir(root), key=lambda e: e.name):
            try:
                st = entry.stat()
            except OSError:
                continue
            out.append((entry.name, st.st_size, int(st.st_mtime)))
    except OSError:
        return ()
    return tuple(out)


def scan_cached(root: Path, *, refresh: bool = False) -> ProjectScan:
    """`scan()` remembered for a minute: the code screen asks again for every request, and nothing in a scan
    is secret (paths, sizes and times only; the cache stays in memory and `clear_cache()` empties it)."""
    key = str(root)
    sig = _signature(root)
    hit = _CACHE.get(key)
    if hit and not refresh and time.monotonic() - hit[0] < CACHE_S and hit[1] == sig:
        return hit[2]
    result = scan(root)
    _CACHE[key] = (time.monotonic(), sig, result)
    while len(_CACHE) > CACHE_KEEP:
        _CACHE.pop(next(iter(_CACHE)))
    return result


def clear_cache() -> int:
    n = len(_CACHE)
    _CACHE.clear()
    return n


# ============================================================================ reading files
def read_text(path: Path, limit: int = MAX_TEXT_BYTES) -> str | None:
    """A small text file as text (UTF-8 with or without BOM, UTF-16, Hebrew ANSI); None for binary or big."""
    from jarvis.tools.fs import decode_text

    try:
        if path.stat().st_size > limit:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:4096] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return None
    return decode_text(data)


def _json(path: Path) -> dict[str, Any]:
    text = read_text(path)
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _toml(path: Path) -> dict[str, Any]:
    text = read_text(path)
    if not text or tomllib is None:
        return {}
    try:
        return tomllib.loads(text)
    except Exception:
        return {}


_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._\-]*)(\[[^\]]*\])?")


def requirement_names(lines: list[str]) -> list[str]:
    """Package names from requirement lines ("fastapi>=0.110", "uvicorn[standard]"), comments and options left out."""
    names = []
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http:", "https:")):
            continue
        m = _REQ_NAME.match(line)
        if m:
            names.append(m.group(1).lower())
    return names


# ============================================================================ the report
def inspect_project(root: Path, scan_result: ProjectScan | None = None) -> dict[str, Any]:
    """The project report the code screen shows and the AI gets (no secrets, nothing run)."""
    s = scan_result or scan(root)
    files = s.files
    lang_counts = Counter(f.language for f in files if f.language)
    data_counts = Counter(DATA_FORMATS[Path(f.path).suffix.lower()] for f in files
                          if Path(f.path).suffix.lower() in DATA_FORMATS)
    names = {f.path for f in files}
    manifests = [m for m in MANIFESTS if m in names]
    manifests += sorted(f.path for f in files if "/" not in f.path and f.path.lower().endswith((".csproj", ".sln")))

    python = _python_facts(root, names)
    node = _node_facts(root, names)
    frameworks = sorted({*python.pop("frameworks"), *node.pop("frameworks")})
    commands = _commands(root, names, python, node)
    tools = _tools(root, python, node)
    style = _style(root, files)
    readme = next((f.path for f in files if "/" not in f.path and f.path.lower().startswith("readme")), None)
    readme_text = ""
    if readme:
        readme_text = (read_text(root / readme) or "")[:README_CHARS]
    tests_dirs = sorted({f.path.split("/")[0] for f in files if "/" in f.path
                         and f.path.split("/")[0].lower() in ("tests", "test", "__tests__", "spec", "e2e")})
    test_files = [f.path for f in files if _is_test_file(f.path)]
    missing = _missing(root, names, python, node, commands, tools, test_files)
    return {
        "root": str(root),
        "name": root.name,
        "files": len(files),
        "truncated": s.truncated,
        "scan_ms": int(s.seconds * 1000),
        "languages": [{"name": n, "files": c} for n, c in lang_counts.most_common(8)],
        "formats": [{"name": n, "files": c} for n, c in data_counts.most_common(6)],
        "manifests": manifests,
        "frameworks": frameworks,
        "python": python,
        "node": node,
        "commands": commands,
        "tools": tools,
        "style": style,
        "readme": {"path": readme, "text": readme_text} if readme else None,
        "layout": [{"name": d, "files": c} for d, c in sorted(s.dirs.items(), key=lambda kv: (-kv[1], kv[0]))[:24]],
        "tests": {"dirs": tests_dirs, "files": len(test_files), "examples": test_files[:6]},
        "secret_files": sorted(f.path for f in files if f.secret)[:30],
        "missing": missing,
        "local_modules": _local_modules(files),
    }


def _local_modules(files: list[FileInfo]) -> list[str]:
    """Top-level Python modules and packages of the project itself (at its root or in src/), so importing them
    is not taken for a missing library."""
    names: set[str] = set()
    for f in files:
        parts = f.path.split("/")
        if parts[0] == "src" and len(parts) > 1:
            parts = parts[1:]
        if len(parts) == 1 and parts[0].endswith(".py"):
            names.add(parts[0][:-3])
        elif len(parts) >= 2 and f.path.endswith(".py"):
            names.add(parts[0])
    return sorted(n for n in names if n.isidentifier())[:200]


def _is_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return (name.startswith("test_") and name.endswith(".py")) or name.endswith("_test.py") \
        or bool(re.search(r"\.(test|spec)\.[cm]?[jt]sx?$", name)) or "/__tests__/" in f"/{path.lower()}"


def _python_facts(root: Path, names: set[str]) -> dict[str, Any]:
    deps: list[str] = []
    dev_deps: list[str] = []
    scripts: dict[str, str] = {}
    tool_config: list[str] = []
    line_length = None
    project_name = ""
    py = _toml(root / "pyproject.toml") if "pyproject.toml" in names else {}
    if py:
        proj = py.get("project", {}) if isinstance(py.get("project"), dict) else {}
        project_name = str(proj.get("name", ""))
        deps += requirement_names([str(d) for d in proj.get("dependencies", []) or []])
        for group in (proj.get("optional-dependencies", {}) or {}).values():
            dev_deps += requirement_names([str(d) for d in group or []])
        for group in (py.get("dependency-groups", {}) or {}).values():
            dev_deps += requirement_names([str(d) for d in group or [] if isinstance(d, str)])
        scripts.update({str(k): str(v) for k, v in (proj.get("scripts", {}) or {}).items()})
        tool = py.get("tool", {}) if isinstance(py.get("tool"), dict) else {}
        poetry = tool.get("poetry", {}) if isinstance(tool.get("poetry"), dict) else {}
        deps += [str(k).lower() for k in (poetry.get("dependencies", {}) or {}) if str(k).lower() != "python"]
        dev_deps += [str(k).lower() for k in (poetry.get("dev-dependencies", {}) or {})]
        for name in ("ruff", "black", "mypy", "pytest", "isort", "pylint", "flake8", "coverage"):
            if name in tool:
                tool_config.append(name)
        for name in ("ruff", "black"):
            value = tool.get(name, {}).get("line-length") if isinstance(tool.get(name), dict) else None
            if isinstance(value, int):
                line_length = line_length or value
    for req in sorted(n for n in names if "/" not in n and re.fullmatch(r"requirements[\w\-.]*\.txt", n.lower())):
        text = read_text(root / req) or ""
        (dev_deps if "dev" in req.lower() or "test" in req.lower() else deps).extend(
            requirement_names(text.splitlines()))
    has_python = any(n.endswith((".py", ".pyw")) for n in names) or bool(py) or "setup.py" in names
    all_deps = {*deps, *dev_deps}
    frameworks = {PY_FRAMEWORKS[d] for d in all_deps if d in PY_FRAMEWORKS}
    if "manage.py" in names:
        frameworks.add("Django")
    venv = venv_python(root)
    version = _venv_version(root) if venv else ""
    return {"present": has_python, "name": project_name, "dependencies": sorted(set(deps)),
            "dev_dependencies": sorted(set(dev_deps) - set(deps)), "scripts": scripts, "tool_config": tool_config,
            "line_length": line_length, "venv": bool(venv), "venv_python": version,
            "pytest_installed": _venv_has(root, "pytest") if venv else None, "frameworks": frameworks}


def _venv_version(root: Path) -> str:
    for folder in (".venv", "venv", "env"):
        cfg = root / folder / "pyvenv.cfg"
        text = read_text(cfg, 20_000) if cfg.is_file() else None
        if text:
            m = re.search(r"^\s*version(?:_info)?\s*=\s*([\d.]+)", text, re.M)
            if m:
                return m.group(1)
    return ""


def _venv_has(root: Path, package: str) -> bool:
    for folder in (".venv", "venv", "env"):
        base = root / folder
        if not base.is_dir():
            continue
        candidates = [base / "Lib" / "site-packages" / package]
        candidates += list((base / "lib").glob(f"python*/site-packages/{package}")) if (base / "lib").is_dir() else []
        if any(c.exists() for c in candidates):
            return True
    return False


def _node_facts(root: Path, names: set[str]) -> dict[str, Any]:
    pkg = _json(root / "package.json") if "package.json" in names else {}
    deps = sorted(str(k) for k in (pkg.get("dependencies") or {}))
    dev_deps = sorted(str(k) for k in (pkg.get("devDependencies") or {}))
    scripts = {str(k): str(v) for k, v in (pkg.get("scripts") or {}).items()}
    manager = "pnpm" if "pnpm-lock.yaml" in names else "yarn" if "yarn.lock" in names else \
        "bun" if "bun.lockb" in names or "bun.lock" in names else "npm"
    frameworks = {JS_FRAMEWORKS[d] for d in (*deps, *dev_deps) if d in JS_FRAMEWORKS}
    present = bool(pkg) or any(n.endswith((".js", ".ts", ".tsx", ".jsx", ".mjs")) for n in names)
    return {"present": present, "name": str(pkg.get("name", "")), "dependencies": deps, "dev_dependencies": dev_deps,
            "scripts": scripts, "manager": manager if pkg else "", "module": pkg.get("type") == "module",
            "installed": (root / "node_modules").is_dir() if pkg else None, "frameworks": frameworks}


def _commands(root: Path, names: set[str], python: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    """The commands the project has for tests, running, building and checking (found, not run)."""
    detected = detect_test_command(root)
    test = detected[1] if detected else ""
    run: list[str] = []
    build: list[str] = []
    check: list[str] = []
    manager = node.get("manager") or "npm"
    for key in node.get("scripts", {}):
        line = f"{manager} run {key}" if key not in ("start", "test") else f"{manager} {key}"
        if key in ("dev", "start", "serve", "preview"):
            run.append(line)
        elif key.startswith("build"):
            build.append(line)
        elif key in ("lint", "format", "typecheck", "type-check", "check") or key.startswith("lint"):
            check.append(line)
        elif key == "test" and not test:
            test = line
    for key in python.get("scripts", {}):
        run.append(key)
    if "manage.py" in names:
        run.append("python manage.py runserver")
    for entry in ("main.py", "app.py", "run.py", "bot.py", "server.py", "src/main.py"):
        if entry in names:
            run.append(f"python {entry}")
            break
    for script in ("run.bat", "start.bat", "run.ps1", "start.ps1", "run.sh"):
        if script in names:
            run.append(script)
    if "Makefile" in names:
        targets = re.findall(r"^([A-Za-z0-9][\w\-]*):", read_text(root / "Makefile") or "", re.M)
        run += [f"make {t}" for t in targets if t in ("run", "dev", "start", "serve")][:2]
        build += [f"make {t}" for t in targets if t in ("build", "all", "dist")][:2]
        if not test and "test" in targets:
            test = "make test"
    tool_config = python.get("tool_config", [])
    if "ruff" in tool_config:
        check.append("ruff check .")
    if "mypy" in tool_config:
        check.append("mypy")
    if "black" in tool_config:
        check.append("black --check .")
    return {"test": test, "run": run[:6], "build": build[:4], "check": check[:6]}


def _tools(root: Path, python: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    """Which programs this computer has for the project (looked up, not started)."""
    from jarvis.tools.dev import find_python

    system_python = find_python()
    found = {
        "python": bool(python.get("venv") or system_python),
        "python_where": "venv" if python.get("venv") else ("system" if system_python else ""),
        "node": bool(shutil.which("node")),
        "npm": bool(shutil.which("npm.cmd" if IS_WINDOWS else "npm") or shutil.which("npm")),
        "git": bool(shutil.which("git")),
    }
    manager = node.get("manager")
    if manager in ("pnpm", "yarn", "bun"):
        found[manager] = bool(shutil.which(f"{manager}.cmd" if IS_WINDOWS else manager) or shutil.which(manager))
    found["git_repo"] = (root / ".git").exists()
    return found


def _missing(root: Path, names: set[str], python: dict[str, Any], node: dict[str, Any], commands: dict[str, Any],
             tools: dict[str, Any], test_files: list[str]) -> list[dict[str, str]]:
    """What stands between this project and running its tests or code here, in both languages."""
    out: list[dict[str, str]] = []

    def add(key: str, he: str, en: str) -> None:
        out.append({"key": key, "he": he, "en": en})

    if not commands.get("test"):
        add("no_tests", "לא מצאתי בדיקות אוטומטיות בפרויקט (אפשר לבקש ממני להוסיף).",
            "I found no automated tests in the project (you can ask me to add some).")
    elif commands["test"] == "pytest" and python.get("venv") and python.get("pytest_installed") is False:
        add("no_pytest", "pytest לא מותקן בסביבה של הפרויקט (.venv), אז הרצת הבדיקות תיכשל עד שיותקן.",
            "pytest is not installed in the project's environment (.venv), so the tests will not run until it is.")
    if python.get("present") and not tools.get("python"):
        add("no_python", "לא מצאתי Python במחשב הזה.", "Python was not found on this computer.")
    if node.get("present") and node.get("dependencies") and not tools.get("node"):
        add("no_node", "לא מצאתי Node.js במחשב הזה.", "Node.js was not found on this computer.")
    if node.get("installed") is False and (node.get("dependencies") or node.get("dev_dependencies")):
        add("no_node_modules", "התלויות של Node עוד לא הותקנו (אין node_modules); צריך npm install לפני בדיקות.",
            "The Node dependencies are not installed yet (no node_modules); run npm install before testing.")
    if not tools.get("git"):
        add("no_git", "Git לא מותקן; ביטול שינויים יעבוד דרך הגיבוי של JARVIS.",
            "Git is not installed; undoing changes works through JARVIS's own backup.")
    elif not tools.get("git_repo"):
        add("no_repo", "הפרויקט לא במאגר Git; ביטול שינויים יעבוד דרך הגיבוי של JARVIS.",
            "The project is not a Git repository; undoing changes works through JARVIS's own backup.")
    return out


# ============================================================================ coding style
_JS_EXT = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte")


def _style(root: Path, files: list[FileInfo]) -> dict[str, Any]:
    """How the project's code is written: what the AI is told to keep, and what JARVIS keeps when writing."""
    sample = [f for f in sorted(files, key=lambda f: -f.mtime) if f.text and f.language and f.size < 120_000][:24]
    indents: Counter[str] = Counter()
    eol: Counter[str] = Counter()
    quotes: Counter[str] = Counter()
    semis = [0, 0]
    bom = 0
    for f in sample:
        try:
            raw = (root / f.path).read_bytes()
        except OSError:
            continue
        bom += raw.startswith(b"\xef\xbb\xbf")
        crlf = raw.count(b"\r\n")
        eol["crlf" if crlf and crlf >= raw.count(b"\n") / 2 else "lf"] += 1
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        unit = _indent_unit(lines[:400])
        if unit:
            indents[unit] += 1
        if f.path.lower().endswith(_JS_EXT):
            quotes["single"] += len(re.findall(r"(?<![\w\"])'[^'\n]{0,40}'", text))
            quotes["double"] += len(re.findall(r'(?<![\w\'])"[^"\n]{0,40}"', text))
            for line in lines[:400]:
                stripped = line.rstrip()
                if stripped and not stripped.endswith(("{", "}", ",", "(", "[", "*/", "//")):
                    semis[stripped.endswith(";")] += 1
    indent = indents.most_common(1)[0][0] if indents else ""
    editor = read_text(root / ".editorconfig", 20_000) or ""
    max_line = re.search(r"max_line_length\s*=\s*(\d+)", editor)
    prettier = _json(root / ".prettierrc") if (root / ".prettierrc").is_file() else {}
    out: dict[str, Any] = {
        "indent": {"tab": "tabs", "2": "2 spaces", "4": "4 spaces"}.get(indent, ""),
        "line_endings": eol.most_common(1)[0][0].upper() if eol else "",
        "bom": bool(bom and bom >= len(sample) / 2),
        "max_line": int(max_line.group(1)) if max_line else prettier.get("printWidth") if isinstance(
            prettier.get("printWidth"), int) else None,
    }
    if quotes["single"] + quotes["double"] >= 10:
        out["js_quotes"] = "single" if quotes["single"] > quotes["double"] else "double"
    if sum(semis) >= 20:
        out["js_semicolons"] = semis[1] > semis[0]
    return out


def _indent_unit(lines: list[str]) -> str:
    """"tab", "2" or "4": the step one level of nesting adds in this file ("" when it has none)."""
    tabs = sum(1 for line in lines if line.startswith("\t"))
    widths = Counter(len(m.group(1)) for line in lines if (m := re.match(r"^( +)\S", line)))
    spaced = sum(widths.values())
    if tabs > spaced:
        return "tab"
    if not widths:
        return ""
    # The smallest indentation used often is one level: 2 when 2-space lines are common, else 4.
    return "2" if widths.get(2, 0) >= max(3, spaced // 10) else "4" if widths.get(4, 0) else ""


__all__ = ["FileInfo", "ProjectScan", "clear_cache", "inspect_project", "is_secret_name", "read_text", "scan",
           "scan_cached"]
