"""Stage 12: `jarvis check`, the readiness report that runs on the user's PC."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from jarvis.common.checks import CheckReport, format_checks
from jarvis.config.paths import resolve_paths
from jarvis.config.settings import Settings
from jarvis.selfcheck import format_report, run_checks

SECRET = "sk-ant-test-" + "7" * 24
NAMES = ["python", "folders", "memory", "server", "interface", "desktop", "keys", "ai", "tools", "internet", "voice",
         "computer", "media"]


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch) -> tuple[Settings, object]:
    import jarvis.selfcheck as selfcheck

    async def offline(report: CheckReport, settings: Settings) -> None:      # no network in unit tests
        report.add("internet", "skip", "not checked in tests")

    monkeypatch.setattr(selfcheck, "_internet", offline)
    settings = Settings()
    return settings, resolve_paths(settings.paths).ensure()


async def test_every_area_is_reported_and_keys_are_named_never_shown(setup, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    settings, paths = setup
    settings.ai.allow_paid = True
    notes: list[str] = []
    report = await run_checks(settings, paths, progress=notes.append)
    assert [c["name"] for c in report["checks"]] == NAMES
    assert notes[0] == "checking python"
    by_name = {c["name"]: c for c in report["checks"]}
    for name in ("folders", "memory", "server", "interface", "keys", "ai"):
        assert by_name[name]["status"] == "ok", by_name[name]
    assert "ANTHROPIC_API_KEY" in by_name["keys"]["detail"] and "anthropic" in by_name["ai"]["detail"]
    text = json.dumps(report, ensure_ascii=False) + format_report(report)
    assert SECRET not in text and SECRET[-12:] not in text
    assert not list(Path(paths.workspace).glob(".jarvis-check-*"))           # probes cleaned up
    settings.ai.allow_paid = False                                           # the default: the key is not used
    ai = next(c for c in (await run_checks(settings, paths))["checks"] if c["name"] == "ai")
    assert ai["status"] == "warn" and ai["data"]["paid"] is False and not ai["data"]["anthropic"]
    assert "Paid AI (Grok, Claude, ChatGPT) is off" in ai["data"]["hints"][0]


async def test_computer_control_line(setup, monkeypatch, tmp_path) -> None:
    import jarvis.computer.backend as backend
    from jarvis.computer.server import EXE_NAME

    settings, paths = setup

    class Ready:
        def problem(self) -> None:
            return None

    monkeypatch.setattr(backend, "get_backend", lambda: Ready())
    settings.computer.models_dir = str(tmp_path)
    line = lambda report: next(c for c in report["checks"] if c["name"] == "computer")  # noqa: E731
    missing = line(await run_checks(settings, paths))
    assert missing["status"] == "warn" and "jarvis computer install" in missing["detail"]
    (tmp_path / "llama.cpp").mkdir()
    (tmp_path / "llama.cpp" / EXE_NAME).write_bytes(b"")
    (tmp_path / "gguf").mkdir()
    for name in ("Holo-3.1-9B-Q8_0.gguf", "mmproj-Holo-3.1-9B-F16.gguf"):
        (tmp_path / "gguf" / name).write_bytes(b"")
    ready = line(await run_checks(settings, paths))
    assert ready["status"] == "ok" and "Holo-3.1-9B-Q8_0.gguf" in ready["detail"]
    settings.computer.enabled = False
    assert line(await run_checks(settings, paths))["status"] == "ok"


async def test_media_line(setup, tmp_path) -> None:
    settings, paths = setup
    settings.media.models_dir = str(tmp_path)
    line = lambda report: next(c for c in report["checks"] if c["name"] == "media")  # noqa: E731
    missing = line(await run_checks(settings, paths))
    assert missing["status"] == "warn" and "runtime, image, video" in missing["detail"]
    assert "jarvis media install" in " ".join(missing["data"]["hints"])
    python = tmp_path / "venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    for folder in ("Z-Image-Turbo", "Wan2.2-TI2V-5B"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "model_index.json").write_text("{}", encoding="utf-8")
    ready = line(await run_checks(settings, paths))
    assert ready["status"] == "ok" and "for free" in ready["detail"] and "without the Hebrew translator" in ready["detail"]
    assert "GB of RAM free now" in ready["detail"]
    settings.media.engine = "xai"
    assert "for free" in line(await run_checks(settings, paths))["detail"]    # paid AI off: on this computer anyway
    settings.ai.allow_paid = True
    assert "Grok Imagine (paid" in line(await run_checks(settings, paths))["detail"]


async def test_without_an_ai_key_it_says_what_still_works(setup) -> None:
    settings, paths = setup
    report = await run_checks(settings, paths)
    ai = next(c for c in report["checks"] if c["name"] == "ai")
    assert ai["status"] == "warn" and "jarvis chat install" in " ".join(ai["data"]["hints"])
    assert report["ok"] is (not any(c["status"] == "fail" for c in report["checks"]))


async def test_the_free_chat_model_answers_once_installed(setup, tmp_path) -> None:
    from jarvis.computer.server import EXE_NAME

    settings, paths = setup
    settings.computer.models_dir = str(tmp_path / "JARVIS-models")
    (tmp_path / "JARVIS-models" / "llama.cpp").mkdir(parents=True)          # computer control's runtime ...
    (tmp_path / "JARVIS-models" / "llama.cpp" / EXE_NAME).write_bytes(b"")
    (tmp_path / "JARVIS-models" / "chat" / "gguf").mkdir(parents=True)      # ... and the model beside it
    (tmp_path / "JARVIS-models" / "chat" / "gguf" / "gemma-4-12B-it-qat-UD-Q4_K_XL.gguf").write_bytes(b"")
    ai = next(c for c in (await run_checks(settings, paths))["checks"] if c["name"] == "ai")
    assert ai["status"] == "ok" and "answers with: local" in ai["detail"], ai


async def test_a_crashing_check_is_a_failed_line_not_a_crash() -> None:
    report = CheckReport()

    async def boom() -> None:
        raise RuntimeError("disk on fire\nsecond line")

    await report.guard("boom", boom())
    report.add("fine", "ok", "works", hints=["a hint"])
    done = report.finish()
    assert done["ok"] is False and done["status"] == "fail"
    text = format_checks("JARVIS self-check", done)
    assert "[FAIL] boom       unexpected error: disk on fire" in text and "second line" not in text
    assert "→ a hint" in text and text.endswith("Problems found. (0.0 s)")
