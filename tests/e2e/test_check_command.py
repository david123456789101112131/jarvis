"""Stage 12: `jarvis check --full`, as the user runs it on their PC: a second JARVIS in a temporary folder
runs real commands end to end, and the report says so line by line."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIVE = ["live:start", "live:folder", "live:system", "live:build", "live:approve", "live:kill", "live:restart"]

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(400)]


async def test_check_full_runs_real_commands_in_a_temporary_jarvis(jarvis_home: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("JARVIS__")}
    env.update(JARVIS_HOME=str(jarvis_home), PYTHONPATH=str(ROOT / "src"), PYTHONUTF8="1")
    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "jarvis", "check", "--full", "--json", env=env,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), 380)
    report = json.loads(out.decode("utf-8"))
    checks = {c["name"]: c for c in report["checks"]}
    for name in LIVE:
        assert checks[name]["status"] == "ok", (checks.get(name), err.decode("utf-8", "replace")[-2000:])
    assert "עברו" in checks["live:build"]["detail"] or "Node.js" in checks["live:build"]["detail"]
    assert proc.returncode == (0 if report["ok"] else 1)
    # The temporary JARVIS is gone and left nothing in the user's JARVIS folder.
    assert not list((jarvis_home / "workspace").iterdir()) and not list((jarvis_home / "projects").iterdir())
