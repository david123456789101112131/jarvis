"""Stage 8/9: the built-in tools, run through the real executor gate."""

from __future__ import annotations

import asyncio
import shutil
import sys
import zipfile
from pathlib import Path

import httpx
import pytest

from fakes import FakeHost
from jarvis.memory import create_memory
from jarvis.tools.builtin import create_registry
from jarvis.tools.dev import count_text, parse_test_counts
from jarvis.tools.web import parse_duckduckgo


@pytest.fixture
def tk(kit):
    kit.registry = create_registry()
    kit.executor.registry = kit.registry
    kit.host = FakeHost()
    kit.services.host = kit.host
    return kit


async def run(tk, tool: str, params: dict, agent: str = "system", **kw):
    return await tk.executor.execute(tool, params, agent_id=agent, task_id=kw.pop("task_id", "task_t"), **kw)


async def answer(tk, approved: bool) -> str:
    while not tk.broker.pending():
        await asyncio.sleep(0.01)
    req = tk.broker.pending()[0]
    tk.broker.respond(req.id, approved)
    return req.summary


class TestFiles:
    async def test_write_read_list_copy_move(self, tk) -> None:
        ws = tk.paths.workspace
        r = await run(tk, "write_file", {"path": "notes/שלום.txt", "content": "שלום עולם\nשורה 2"}, "development")
        assert r.ok, r.error
        target = ws / "notes" / "שלום.txt"
        assert target.read_text(encoding="utf-8").startswith("שלום")
        r = await run(tk, "read_file", {"path": str(target)}, "research")
        assert r.data["content"].startswith("שלום עולם")
        r = await run(tk, "list_dir", {"path": "workspace", "recursive": True}, "planner")
        assert any(e["name"].endswith("שלום.txt") for e in r.data["entries"])
        r = await run(tk, "copy_path", {"source": str(target), "destination": str(ws / "copy.txt")}, "development")
        assert r.ok and (ws / "copy.txt").exists()
        r = await run(tk, "move_path", {"source": str(ws / "copy.txt"), "destination": str(ws / "moved.txt")},
                      "development")
        assert r.ok and (ws / "moved.txt").exists() and not (ws / "copy.txt").exists()
        r = await run(tk, "create_folder", {"path": "workspace/new/inner"}, "development")
        assert r.ok and (ws / "new" / "inner").is_dir()

    async def test_read_hebrew_ansi_and_docx(self, tk) -> None:
        ws = tk.paths.workspace
        (ws / "ansi.txt").write_bytes("מסמך בעברית".encode("cp1255"))
        r = await run(tk, "read_file", {"path": str(ws / "ansi.txt")}, "research")
        assert r.data["content"] == "מסמך בעברית"
        docx = ws / "doc.docx"
        with zipfile.ZipFile(docx, "w") as zf:
            zf.writestr("word/document.xml", '<w:document><w:body><w:p><w:r><w:t>Hello</w:t></w:r></w:p>'
                                             '<w:p><w:r><w:t>שלום</w:t></w:r></w:p></w:body></w:document>')
        r = await run(tk, "read_file", {"path": str(docx)}, "research")
        assert r.data["content"].split() == ["Hello", "שלום"]

    async def test_find_files(self, tk) -> None:
        ws = tk.paths.workspace
        (ws / "a").mkdir()
        (ws / "a" / "report-2026.pdf").write_bytes(b"%PDF-1.4")
        (ws / "a" / "notes.md").write_text("the budget is 500", encoding="utf-8")
        (ws / "node_modules").mkdir()
        (ws / "node_modules" / "report.pdf").write_bytes(b"x")
        r = await run(tk, "find_files", {"root": "workspace", "name": "report"}, "research")
        assert [Path(x["path"]).name for x in r.data["results"]] == ["report-2026.pdf"]
        r = await run(tk, "find_files", {"root": "workspace", "contains": "budget"}, "research")
        assert [Path(x["path"]).name for x in r.data["results"]] == ["notes.md"]
        r = await run(tk, "find_files", {"root": "workspace", "extensions": ["md"]}, "research")
        assert len(r.data["results"]) == 1

    async def test_delete_goes_to_recycle_bin(self, tk, monkeypatch) -> None:
        trashed: list[str] = []
        import send2trash

        monkeypatch.setattr(send2trash, "send2trash", lambda p: (trashed.append(p), Path(p).unlink()))
        f = tk.paths.workspace / "old.txt"
        f.write_text("x")
        r = await run(tk, "delete_path", {"paths": [str(f)]}, "development")
        assert r.ok and trashed == [str(f)] and not f.exists()

    async def test_broad_and_permanent_deletes_ask_first(self, tk) -> None:
        folder = tk.paths.workspace / "many"
        folder.mkdir()
        for i in range(30):
            (folder / f"{i}.txt").write_text("x")
        call = run(tk, "delete_path", {"paths": [str(folder)]}, "system")
        r, summary = await asyncio.gather(call, answer(tk, False))
        assert not r.ok and r.denied == "confirmation" and folder.exists() and "items" in summary
        single = tk.paths.workspace / "one.txt"
        single.write_text("x")
        r, _ = await asyncio.gather(run(tk, "delete_path", {"paths": [str(single)], "permanent": True}, "system"),
                                    answer(tk, True))
        assert r.ok and not single.exists()

    async def test_open_path_and_executables(self, tk) -> None:
        doc = tk.paths.workspace / "a.txt"
        doc.write_text("x")
        assert (await run(tk, "open_path", {"path": str(doc)}, "system")).ok
        assert tk.host.calls[-1] == ("open_path", str(doc))
        exe = tk.paths.workspace / "setup.exe"
        exe.write_bytes(b"MZ")
        r, _ = await asyncio.gather(run(tk, "open_path", {"path": str(exe)}, "system"), answer(tk, False))
        assert not r.ok and ("open_path", str(exe)) not in tk.host.calls


class TestApps:
    async def test_open_app_hebrew_and_sites(self, tk) -> None:
        r = await run(tk, "open_app", {"name": "כרום"})
        assert r.ok and tk.host.calls[-1][:2] == ("launch", "Google Chrome")
        r = await run(tk, "open_app", {"name": "יוטיוב"})
        assert r.ok and tk.host.calls[-1] == ("open_url", "https://www.youtube.com")
        r = await run(tk, "open_app", {"name": "photoshop"})
        assert not r.ok and "couldn't find" in r.error
        r = await run(tk, "close_app", {"name": "chrome"})
        assert r.ok and r.summary == "Closed Google Chrome"

    async def test_core_processes_are_protected(self, tk) -> None:
        r, _ = await asyncio.gather(run(tk, "close_app", {"name": "explorer"}), answer(tk, True))
        assert not r.ok and r.denied == "blocked"
        import os

        r, _ = await asyncio.gather(run(tk, "kill_process", {"pid": os.getpid()}), answer(tk, True))
        assert not r.ok and "protected" in r.error

    async def test_creative_agent_cannot_close_apps(self, tk) -> None:
        r = await run(tk, "close_app", {"name": "chrome"}, "creative")
        assert not r.ok and r.denied == "permission"


@pytest.mark.skipif(sys.platform == "win32" and not shutil.which("bash"), reason="bash commands")
class TestShellAndCode:
    async def test_run_command(self, tk) -> None:
        cmd = "Write-Output hello" if sys.platform == "win32" else "echo hello"
        r = await run(tk, "run_command", {"command": cmd})
        assert r.ok and r.data["stdout"].strip() == "hello"
        assert tk.services.processes.list() == []

    async def test_dangerous_command_is_never_run_silently(self, tk) -> None:
        marker = tk.paths.workspace / "marker.txt"
        cmd = f"rm -rf / --no-preserve-root; echo x > '{marker}'"
        r, _ = await asyncio.gather(run(tk, "run_command", {"command": cmd}), answer(tk, False))
        assert not r.ok and r.denied == "confirmation" and not marker.exists()

    async def test_run_python_and_tests(self, tk) -> None:
        r = await run(tk, "run_python", {"code": "print(6*7)"}, "development")
        assert r.ok and r.data["stdout"].strip() == "42"
        proj = tk.paths.projects / "calc"
        (proj / "tests").mkdir(parents=True)
        (proj / "pyproject.toml").write_text("[project]\nname='calc'\nversion='0.1'\n")
        (proj / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (proj / "tests" / "test_calc.py").write_text(
            "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parents[1]))\n"
            "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")
        # The project's own code (not generated by JARVIS): running its tests asks first.
        r, asked = await asyncio.gather(run(tk, "run_tests", {"path": str(proj)}, "development"), answer(tk, True))
        assert r.ok, r.error
        assert r.data["counts"].get("passed") == 1 and "calc" in asked

    @pytest.mark.skipif(not shutil.which("git"), reason="git not installed")
    async def test_git(self, tk) -> None:
        repo = tk.paths.projects / "repo"
        r = await run(tk, "git", {"args": ["init"], "path": str(repo)}, "development")
        assert r.ok, r.error
        r = await run(tk, "git", {"args": ["status", "--short"], "path": str(repo)}, "development")
        assert r.ok
        r, _ = await asyncio.gather(
            run(tk, "git", {"args": ["push", "origin", "main"], "path": str(repo)}, "development"),
            answer(tk, False))
        assert r.denied == "confirmation"


@pytest.mark.parametrize(("output", "counts", "he"), [
    ("===== 5 passed, 1 skipped in 0.21s =====", {"passed": 5, "skipped": 1}, "5 עברו, 1 דולגה"),
    ("Tests:       1 failed, 5 passed, 6 total", {"failed": 1, "passed": 5}, "1 נכשלה, 5 עברו"),
    (" Test Files  1 passed (1)\n      Tests  7 passed (7)", {"passed": 7}, "7 עברו"),
    # Node's own test runner: TAP when piped, the spec reporter in a terminal
    ("ok 18 - orders\n1..18\n# tests 18\n# pass 18\n# fail 0\n# skipped 0\n", {"passed": 18}, "18 עברו"),
    ("ℹ tests 7\nℹ pass 6\nℹ fail 1\nℹ cancelled 0\n", {"passed": 6, "failed": 1}, "6 עברו, 1 נכשלה"),
    ("nothing to see", {}, ""),
])
def test_test_results_are_counted_for_every_runner(output: str, counts: dict, he: str) -> None:
    assert parse_test_counts(output) == counts
    assert count_text(counts, "he") == he


DDG_HTML = """
<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&rut=x">
Example A</a><a class="result__snippet">First snippet</a></div>
<div class="result"><a class="result__a" href="https://duckduckgo.com/y.js?ad=1">Ad</a></div>
<div class="result"><a class="result__a" href="https://example.org/b">Example B</a></div>
"""

PAGE = """<html><head><title>JARVIS Docs</title><script>var x=1</script></head><body><nav>menu</nav>
<main><h1>Welcome</h1><p>JARVIS is a desktop assistant.</p><ul><li>Fast</li><li>Safe</li></ul>
<a href="/about">About</a></main><footer>footer</footer></body></html>"""


def mock_http(tk, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    tk.services._http = httpx.AsyncClient(transport=httpx.MockTransport(wrapped), follow_redirects=True)
    return seen


class TestWeb:
    def test_parse_duckduckgo(self) -> None:
        results = parse_duckduckgo(DDG_HTML, 5)
        assert [r["url"] for r in results] == ["https://example.com/a", "https://example.org/b"]
        assert results[0]["snippet"] == "First snippet"

    async def test_search_and_fetch(self, tk) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "duckduckgo" in request.url.host:
                return httpx.Response(200, text=DDG_HTML, headers={"content-type": "text/html"})
            return httpx.Response(200, text=PAGE, headers={"content-type": "text/html; charset=utf-8"})

        mock_http(tk, handler)
        r = await run(tk, "web_search", {"query": "jarvis assistant"}, "research")
        assert r.ok and r.data["provider"] == "duckduckgo" and len(r.data["results"]) == 2
        r = await run(tk, "fetch_url", {"url": "https://example.com/docs", "include_links": True}, "research")
        assert r.ok and r.data["title"] == "JARVIS Docs"
        assert "JARVIS is a desktop assistant." in r.data["content"] and "- Fast" in r.data["content"]
        assert "menu" not in r.data["content"] and "var x" not in r.data["content"]
        assert r.data["links"][0]["url"] == "https://example.com/about"

    async def test_secrets_never_leave_and_network_switch(self, tk) -> None:
        seen = mock_http(tk, lambda req: httpx.Response(200, json={"ok": True}))
        key = "sk-ant-api03-" + "K" * 40
        tk.redactor.register("ANTHROPIC_API_KEY", key)
        r = await run(tk, "http_request", {"method": "POST", "url": "https://api.example.com/x",
                                           "json_body": {"token": key}}, "research")
        assert not r.ok and r.denied == "blocked" and seen == []
        r = await run(tk, "http_request", {"url": "https://api.example.com/ok"}, "research")
        assert r.ok and r.data["body"] == {"ok": True}
        tk.settings.security.network_enabled = False
        r = await run(tk, "fetch_url", {"url": "https://example.com"}, "research")
        assert not r.ok and "turned off" in r.error


class TestSystemAndMemory:
    async def test_system_info_and_health(self, tk) -> None:
        r = await run(tk, "system_info", {}, "planner")
        assert r.ok and {"cpu", "memory", "disks", "network"} <= set(r.data)
        r = await run(tk, "health_check", {})
        assert r.ok and "findings" in r.data

    def test_the_busiest_processes_from_a_recent_reading_without_waiting(self, monkeypatch) -> None:
        import subprocess
        import time

        from jarvis.tools import apps

        monkeypatch.setattr(apps, "_process_read_at", None)
        monkeypatch.setattr(apps, "_listed_read", None)
        waits: list[float] = []
        pause = time.sleep
        monkeypatch.setattr(time, "sleep", lambda s: (waits.append(s), pause(s)))
        busy = subprocess.Popen([sys.executable, "-c", "while True: sum(range(1000))"])
        try:
            pause(0.3)
            apps.top_processes(5)                          # no reading yet: it measures for sample_s
            assert waits == [0.4]
            apps.prime_processes()                         # what the background monitor does every 30 s
            pause(0.6)
            late = subprocess.Popen([sys.executable, "-c", "while True: sum(range(1000))"])
            try:
                pause(0.6)
                waits.clear()
                rows = apps.top_processes(400)
                assert waits == []                         # the reading 1.2 s ago starts the window
                by_pid = {r["pid"]: r for r in rows}
                cores = __import__("psutil").cpu_count() or 1
                assert by_pid[busy.pid]["cpu"] > 40 / cores      # one busy core, measured since the reading
                assert by_pid[late.pid]["cpu"] > 20 / cores      # started since: measured from its own start
            finally:
                late.kill()
        finally:
            busy.kill()

    def test_the_busiest_processes_from_one_listing_of_every_process(self, monkeypatch) -> None:
        """What Windows answers in one system call: the CPU use is the change since the last reading, a reused
        pid counts from its own start, and the System Idle Process is not a busy program."""
        import time

        from jarvis.host.winapi import ProcessInfo
        from jarvis.tools import apps

        now = time.time()
        first = {0: ProcessInfo(0, "System Idle Process", 500.0, 8192, 0.0),
                 4: ProcessInfo(4, "System", 10.0, 1 << 20, now - 3600),
                 100: ProcessInfo(100, "busy.exe", 5.0, 50 << 20, now - 60),
                 200: ProcessInfo(200, "old.exe", 1.0, 10 << 20, now - 60)}
        second = {0: ProcessInfo(0, "System Idle Process", 504.0, 8192, 0.0),
                  4: ProcessInfo(4, "System", 10.0, 1 << 20, now - 3600),
                  100: ProcessInfo(100, "busy.exe", 6.0, 50 << 20, now - 60),
                  200: ProcessInfo(200, "new.exe", 0.5, 300 << 20, now - 1.0)}   # the pid, now another program
        listings = [first, second]
        monkeypatch.setattr(apps, "_list_all", lambda: listings.pop(0) if len(listings) > 1 else listings[0])
        monkeypatch.setattr(apps.psutil, "cpu_count", lambda logical=True: 2)
        monkeypatch.setattr(apps, "_process_read_at", None)
        monkeypatch.setattr(apps, "_listed_read", None)

        apps.prime_processes()
        monkeypatch.setattr(apps, "_process_read_at", time.monotonic() - 2.0)      # that reading, 2 s ago
        rows = {r["pid"]: r for r in apps.top_processes(10)}
        assert 0 not in rows and rows[4]["cpu"] == 0.0
        assert 24.0 <= rows[100]["cpu"] <= 25.0       # 1 s of CPU in 2 s: half of one of the 2 cores
        assert 23.0 <= rows[200]["cpu"] <= 25.0       # 0.5 s of CPU since it started 1 s ago
        assert rows[200]["name"] == "new.exe" and rows[200]["memory_mb"] == 300.0
        assert [r["pid"] for r in apps.top_processes(3, sort="memory", sample_s=0.0)] == [200, 100, 4]

    async def test_volume_brightness_media_power(self, tk) -> None:
        assert (await run(tk, "volume", {"action": "set", "level": 30})).summary == "Volume is 30%"
        assert (await run(tk, "volume", {"action": "up", "step": 5})).data["level"] == 35
        assert (await run(tk, "brightness", {"action": "down", "step": 20})).data["level"] == 50
        assert (await run(tk, "media", {"action": "next"})).ok
        r, _ = await asyncio.gather(run(tk, "power", {"action": "shutdown", "delay_s": 60}), answer(tk, False))
        assert not r.ok and not any(c[0] == "power" for c in tk.host.calls)
        assert (await run(tk, "cancel_shutdown", {})).ok

    async def test_memory_tools(self, tk, tmp_path) -> None:
        memory = create_memory(tmp_path / "m.db")
        await memory.start()
        tk.services.memory = memory
        try:
            r = await run(tk, "remember", {"text": "The user prefers dark mode", "category": "preference"}, "planner")
            assert r.ok
            r = await run(tk, "recall", {"query": "dark mode"}, "creative")
            assert "dark mode" in r.summary
        finally:
            await memory.close()


class TestLanguage:
    """What a person reads (summaries, approval requests, reasons, the activity log) follows the
    language of the request; English stays the default for callers that do not say."""

    async def test_hebrew_request_gets_hebrew_texts(self, tk) -> None:
        r = await run(tk, "create_folder", {"path": "workspace/דוחות"}, "development", lang="he")
        assert r.ok and r.summary.startswith("נוצרה התיקייה")
        assert tk.activity.recent()[-1].message.startswith("סוכן פיתוח: נוצרה התיקייה")
        assert tk.audit.recent()[-1].action.startswith("יצירת התיקייה")
        r = await run(tk, "create_folder", {"path": "workspace/reports"}, "development")
        assert r.summary.startswith("Folder created")

    async def test_approval_request_and_reasons_in_hebrew(self, tk) -> None:
        target = tk.paths.workspace / "old.txt"
        target.write_text("x", encoding="utf-8")
        task = asyncio.ensure_future(run(tk, "delete_path", {"paths": [str(target)], "permanent": True}, "system",
                                         lang="he"))
        while not tk.broker.pending():
            await asyncio.sleep(0.01)
        req = tk.broker.pending()[0]
        assert req.summary.startswith("מחיקה לצמיתות: old.txt")
        assert "מחיקה לצמיתות (אי אפשר לבטל אותה)" in req.reasons
        tk.broker.respond(req.id, False)
        res = await task
        assert not res.ok and res.error.startswith("המשתמש לא אישר") and "נדחה" in res.error
        assert target.exists()

    async def test_denials_and_command_risks_in_hebrew(self, tk) -> None:
        r = await run(tk, "run_command", {"command": "dir"}, "planner", lang="he")
        assert not r.ok and r.error.startswith("אין הרשאה: לסוכן תכנון אין הרשאת")
        from jarvis.permissions import analyze_command

        a = analyze_command("format c: /q", "he")
        assert a.reasons == ["מריץ פקודת מערכת", "מפרמט כונן"]
        assert analyze_command("format c: /q").reasons == ["runs a shell command", "formats a drive"]
