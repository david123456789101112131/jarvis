# PyInstaller build of JARVIS: one folder, two programs that share everything else.
#
#   JARVIS.exe      the app with its window (no console window)
#   jarvis-cli.exe  the command line: `jarvis-cli check --full`, `jarvis-cli say "..."`, `jarvis-cli stop`, ...
#
# Build on Windows with scripts\build.ps1 (it also makes the installer), or directly:
#   pyinstaller packaging/jarvis.spec --noconfirm --clean
# The same spec builds on Linux and macOS (without the Windows-only parts), which is how the build
# itself is tested away from Windows. Speech models are not bundled: JARVIS downloads them the first
# time voice starts (or `jarvis-cli voice-test` does).

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - SPECPATH is defined by PyInstaller
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
from jarvis import __version__  # noqa: E402
from jarvis.security.codecheck import SAFE_MODULES  # noqa: E402

PACKAGE = SRC / "jarvis"
ICON = PACKAGE / "assets" / "jarvis.ico"
WINDOWS = sys.platform == "win32"


def optional(collect, name, **kwargs):
    """Data or metadata of a package that may not be installed (the voice or desktop extra)."""
    try:
        return collect(name, **kwargs)
    except Exception as exc:
        print(f"jarvis.spec: skipping {name}: {exc}")
        return []


datas = [
    (str(PACKAGE / "ui" / "static"), "jarvis/ui/static"),                      # the built interface
    (str(PACKAGE / "agents" / "development" / "templates"), "jarvis/agents/development/templates"),
    (str(PACKAGE / "assets"), "jarvis/assets"),                                # the icon, for windows and toasts
]
for package in ("faster_whisper", "pptx", "webview", "certifi"):
    datas += optional(collect_data_files, package)
# Libraries that read their own version (importlib.metadata) when they load or call home.
for dist in ("aiohttp", "anthropic", "openai", "httpx", "huggingface_hub", "tokenizers", "tqdm", "requests",
             "filelock", "packaging", "numpy", "onnxruntime", "ctranslate2", "faster-whisper", "openwakeword",
             "webrtcvad-wheels", "edge-tts", "keyring", "fastapi", "starlette", "pydantic", "uvicorn", "websockets",
             "pywebview", "jarvis-assistant"):
    datas += optional(copy_metadata, dist)

hiddenimports = collect_submodules("jarvis")             # agents, voice and orchestration load lazily
hiddenimports += sorted(SAFE_MODULES)                     # the code check inspects these modules
hiddenimports += ["pyttsx3.drivers", "pyttsx3.drivers.dummy",
                  "pyttsx3.drivers.sapi5" if WINDOWS else "pyttsx3.drivers.espeak"]
if WINDOWS:
    hiddenimports += ["keyring.backends.Windows", "win32ctypes.core", "comtypes.client"]

excludes = ["tkinter", "_tkinter", "matplotlib", "IPython", "jupyter_client", "notebook", "pytest", "_pytest",
            "playwright", "mypy", "ruff", "PyInstaller", "setuptools", "pip", "torch", "tensorflow"]

a = Analysis(  # noqa: F821 - Analysis, PYZ, EXE and COLLECT are defined by PyInstaller
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(SRC)],
    hookspath=[str(ROOT / "packaging" / "hooks")],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

version_file = None
if WINDOWS:
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo)

    numbers = tuple(int(n) for n in (__version__.split(".") + ["0", "0", "0"])[:4])
    version_file = VSVersionInfo(
        ffi=FixedFileInfo(filevers=numbers, prodvers=numbers),
        kids=[StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", "JARVIS"),
            StringStruct("FileDescription", "JARVIS desktop AI assistant"),
            StringStruct("FileVersion", __version__),
            StringStruct("ProductName", "JARVIS"),
            StringStruct("ProductVersion", __version__),
            StringStruct("LegalCopyright", "JARVIS"),
        ])]), VarFileInfo([VarStruct("Translation", [0x0409, 1200])])],
    )


def program(name, console):
    return EXE(  # noqa: F821
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=name,
        console=console,
        icon=str(ICON),
        version=version_file,
        upx=False,                    # UPX-packed programs trip antivirus heuristics
    )


app = program("JARVIS", console=False)
cli = program("jarvis-cli", console=True)
coll = COLLECT(app, cli, a.binaries, a.datas, name="JARVIS", upx=False)  # noqa: F821
