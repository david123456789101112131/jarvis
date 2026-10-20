"""The code screen end to end over the real local server, with Grok replaced by a scripted stand-in (no paid
calls): opening a project gives read access to that folder only; planning sends the ticked files (secrets
hidden) only after the approval that shows where they go and the price; nothing is written before "Apply";
apply and undo keep the file's bytes; a file changed on disk is not overwritten; the project's tests run
after an approval and report what passed and failed; a fix is asked with the failures."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

import jarvis.agents.manager as manager_module
import jarvis.server.api as api_module
from fakes import FakeHost, ScriptedProvider
from integration.test_server import Live
from jarvis.ai.router import ProviderRouter
from jarvis.ai.types import AIMessage, AIResponse, CompletionRequest, Usage
from jarvis.app import JarvisApp
from jarvis.common.enums import Capability
from jarvis.config.settings import Settings

CONFIG_SECRET = "abcd1234efgh5678"
CART = "class Cart:\n    def __init__(self):\n        self.items = []\n\n    def total(self):\n        return sum(self.items)\n"


class FakeGrok(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__([], name="xai")

    @property
    def model(self) -> str:
        return "grok-4.7"


def proposal(**over: Any) -> dict[str, Any]:
    body = {
        "understood": "לדלג על מספרים שליליים בסכום",
        "plan": ["לסנן מספרים שליליים ב-total", "להוסיף בדיקה"],
        "assumptions": ["מספר שלילי הוא החזרה ולא פריט"],
        "questions": [],
        "edits": [
            {"path": "shop/cart.py", "action": "modify", "why": "הסינון",
             "replacements": [{"find": "        return sum(self.items)\n",
                               "replace": "        return sum(i for i in self.items if i >= 0)\n"}]},
            {"path": "tests/test_negative.py", "action": "create", "why": "בדיקה חדשה",
             "content": "from shop.cart import Cart\n\n\ndef test_negative():\n    c = Cart()\n"
                        "    c.items = [3, -1]\n    assert c.total() == 3\n"},
        ],
        "tests": {"added": True, "command": "pytest", "notes": "בדיקה למספר שלילי"},
        "summary": "total מדלג על מספרים שליליים; נוספה בדיקה",
        "risks": [],
    }
    body.update(over)
    return body


def answer(body: dict[str, Any]) -> AIResponse:
    text = json.dumps(body, ensure_ascii=False)
    return AIResponse(text=text, stop_reason="end", message=AIMessage(role="assistant", text=text),
                      usage=Usage(input_tokens=6_000, output_tokens=2_500, reasoning_tokens=1_000))


def project(root: Path) -> Path:
    files = {
        "pyproject.toml": '[project]\nname = "shop"\ndependencies = []\n',
        "shop/__init__.py": "",
        "shop/cart.py": CART,
        "shop/config.py": f'API_KEY = "{CONFIG_SECRET}"\nTIMEOUT = 5\n',
        "tests/test_cart.py": "from shop.cart import Cart\n\n\ndef test_empty():\n    assert Cart().total() == 0\n",
        ".env": "XAI_API_KEY=xai-" + "Z" * 40 + "\n",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.replace("\n", "\r\n").encode() if rel == "shop/cart.py" else text.encode())
    return root


@pytest.fixture
async def dev(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[Live, FakeGrok]]:
    monkeypatch.setattr(manager_module, "GRACE_S", 0.5)
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>JARVIS</title>", encoding="utf-8")
    monkeypatch.setattr(api_module, "STATIC_DIR", static)
    settings = Settings()
    settings.ai.allow_paid = True
    grok = FakeGrok()
    app = JarvisApp(settings, host=FakeHost(), router=ProviderRouter(settings.ai, [grok]), voice=False,
                    use_keyring=False, port=0)
    await app.start()
    live = Live(app, FakeHost())
    try:
        yield live, grok
    finally:
        await live.http.aclose()
        await app.shutdown()


async def done(live: Live, job: dict[str, Any], timeout: float = 30) -> dict[str, Any]:
    finished = await live.app.jobs.wait(job["id"], timeout)
    assert finished is not None and finished.status != "running", job
    return finished.view()


async def approve(live: Live, *, approved: bool = True, timeout: float = 15) -> Any:
    """Answer the next approval dialog as the user would, and return what it showed."""
    broker = live.app.broker
    deadline = asyncio.get_running_loop().time() + timeout
    while not broker.pending():
        assert asyncio.get_running_loop().time() < deadline, "no approval was asked for"
        await asyncio.sleep(0.02)
    request = broker.pending()[0]
    broker.respond(request.id, approved)
    return request


async def open_project(live: Live, root: Path) -> dict[str, Any]:
    opened = await live.post("/api/dev/open", {"path": str(root), "lang": "he"})
    report = await done(live, opened["job"])
    assert report["status"] == "done", report
    return {**opened["session"], "report": report["result"]["report"]}


class TestCodeScreen:
    async def test_plan_review_apply_and_undo(self, dev: tuple[Live, FakeGrok], tmp_path: Path) -> None:
        live, grok = dev
        root = project(tmp_path / "shop")
        status = await live.get("/api/dev/status")
        assert status["engines"][0]["provider"] == "xai" and status["engines"][0]["price"] == [2.0, 6.0]

        session = await open_project(live, root)
        assert session["report"]["commands"]["test"] == "pytest" and ".env" in session["report"]["secret_files"]
        grants = [g for g in live.app.permissions.grants("development") if g.task_id == session["id"]]
        assert [(g.capability, g.roots) for g in grants] == [(Capability.FS_READ, [str(root)])]

        found = await done(live, await live.post(f"/api/dev/sessions/{session['id']}/inspect",
                                                 {"request": "סכום העגלה", "lang": "he"}))
        assert found["result"]["ranked"][0]["path"] == "shop/cart.py"

        grok.push(answer(proposal()))
        job = await live.post(f"/api/dev/sessions/{session['id']}/propose",
                              {"request": "שהסכום ידלג על מספרים שליליים", "files": ["shop/cart.py", "shop/config.py",
                                                                                   ".env"]})
        assert job["sends"] and "api.x.ai" in job["sends"][0] and job["real"] is True
        asked = await approve(live)
        assert asked.risk == "high" and asked.details["engine"].startswith("Grok (grok-4.7)")
        assert asked.details["files"] == ["shop/cart.py (1K)", "shop/config.py (1K)",
                                          "tests/test_cart.py (1K, הבדיקות של cart.py)"]   # its tests come along
        assert asked.details["not_sent"] == [".env"] and "$" in asked.details["price"]
        planned = await done(live, job)
        assert planned["status"] == "done", planned
        assert planned["cost"] and planned["result"]["files"][1]["path"] == "tests/test_negative.py"

        sent = grok.requests[0].messages[0].text
        assert "def total(self)" in sent and CONFIG_SECRET not in sent and "xai-ZZZZ" not in sent
        assert 'API_KEY = "[REDACTED]"' in sent and grok.requests[0].effort == "low"
        assert "=== tests/test_cart.py (6 lines) — the project's tests for shop/cart.py ===" in sent

        change_id = planned["result"]["change_id"]
        change = await live.get(f"/api/dev/changes/{change_id}")
        assert change["can_apply"] is True and change["writable"] is False and change["masked"] == 1
        assert change["files"][0]["eol"] == "CRLF" and change["files"][1]["new_file"] is True
        assert change["cost_usd"] == pytest.approx((6_000 * 2 + 2_500 * 6) / 1e6)
        assert (root / "shop" / "cart.py").read_bytes() == CART.replace("\n", "\r\n").encode()   # nothing written yet

        r = await live.http.post(f"/api/dev/changes/{change_id}/apply", json={})
        assert r.status_code == 409 and r.json()["error"]["code"] == "not_writable"
        allowed = await live.post(f"/api/dev/sessions/{session['id']}/write", {"allow": True})
        assert allowed["writable"] is True

        job = await live.post(f"/api/dev/changes/{change_id}/apply", {})
        asked = await approve(live)          # the project is outside JARVIS's folders, so each write is approved
        assert asked.tool == "dev_apply" and asked.details["files"] == ["shop/cart.py (+1 −1)",
                                                                        "tests/test_negative.py (+7 −0, חדש)"]
        applied = await done(live, job)
        assert applied["status"] == "done", applied
        cart = (root / "shop" / "cart.py").read_bytes()
        assert b"if i >= 0)\r\n" in cart and cart.count(b"\r\n") == cart.count(b"\n")           # CRLF kept
        assert (root / "tests" / "test_negative.py").is_file()
        assert (await live.get(f"/api/dev/changes/{change_id}"))["status"] == "applied"
        listed = await live.get(f"/api/dev/sessions/{session['id']}/changes")
        assert listed[0]["id"] == change_id and listed[0]["can_undo"] is True

        job = await live.post(f"/api/dev/changes/{change_id}/undo", {})
        asked = await approve(live)
        assert asked.details["files"] == ["shop/cart.py (יחזור לגרסה הקודמת)", "tests/test_negative.py (יימחק)"]
        undone = await done(live, job)
        assert undone["status"] == "done", undone
        assert (root / "shop" / "cart.py").read_bytes() == CART.replace("\n", "\r\n").encode()
        assert not (root / "tests" / "test_negative.py").exists()

        # Changed on disk since the proposal: nothing is written, and the screen is told why.
        (root / "shop" / "cart.py").write_text("class Cart:\n    pass\n", encoding="utf-8")
        job = await live.post(f"/api/dev/changes/{change_id}/apply", {})
        await approve(live)
        again = await done(live, job)
        assert again["status"] == "failed" and "השתנו" in again["error"]
        assert (root / "shop" / "cart.py").read_text(encoding="utf-8") == "class Cart:\n    pass\n"
        assert not (root / "tests" / "test_negative.py").exists()
        assert (await live.get(f"/api/dev/changes/{change_id}"))["files"][0]["status"] == "conflict"

        assert (await live.http.delete(f"/api/dev/sessions/{session['id']}")).json() == {"closed": True}
        assert not [g for g in live.app.permissions.grants("development") if g.task_id == session["id"]]
        r = await live.http.post(f"/api/dev/sessions/{session['id']}/inspect", json={})
        assert r.status_code == 409 and r.json()["error"]["code"] == "no_session"

    async def test_a_declined_approval_sends_nothing(self, dev: tuple[Live, FakeGrok], tmp_path: Path) -> None:
        live, grok = dev
        session = await open_project(live, project(tmp_path / "shop"))
        grok.push(answer(proposal()))
        job = await live.post(f"/api/dev/sessions/{session['id']}/propose",
                              {"request": "לתקן את הסכום", "files": ["shop/cart.py"], "lang": "en"})
        await approve(live, approved=False)
        failed = await done(live, job)
        assert failed["status"] == "failed" and "did not approve" in failed["error"]
        assert grok.requests == [] and failed["can_retry"] is True

    async def test_the_tool_never_reaches_grok_without_the_users_click(self, dev: tuple[Live, FakeGrok],
                                                                      tmp_path: Path) -> None:
        live, grok = dev
        root = project(tmp_path / "shop")
        session = await open_project(live, root)
        grok.push(answer(proposal()))
        result = await live.app.executor.execute(
            "dev_propose", {"path": str(root), "session_id": session["id"], "request": "x" * 10,
                            "files": ["shop/cart.py"]}, agent_id="development", task_id=session["id"], lang="en")
        assert not result.ok and "only when you ask" in (result.error or "")
        assert grok.requests == [] and live.app.broker.pending() == []

    async def test_with_paid_ai_off_grok_is_not_offered_and_nothing_reaches_it(self, dev: tuple[Live, FakeGrok],
                                                                              tmp_path: Path) -> None:
        live, grok = dev
        live.app.settings.ai.allow_paid = False
        status = await live.get("/api/dev/status")
        assert status["engines"] == [] and "jarvis chat install" in status["needs"][0]["en"]
        assert "Grok" not in status["needs"][0]["en"]
        session = await open_project(live, project(tmp_path / "shop"))
        grok.push(answer(proposal()))
        r = await live.http.post(f"/api/dev/sessions/{session['id']}/propose",
                                 json={"request": "לתקן את הסכום", "engine": "xai", "lang": "he"})
        assert r.status_code >= 400 and "jarvis chat install" in r.json()["error"]["message"]
        assert grok.requests == [] and live.app.broker.pending() == []

    async def test_an_edit_after_review_needs_a_new_approval(self, dev: tuple[Live, FakeGrok], tmp_path: Path) -> None:
        live, grok = dev
        root = project(tmp_path / "shop")
        session = await open_project(live, root)
        grok.push(answer(proposal()))
        job = await live.post(f"/api/dev/sessions/{session['id']}/propose", {"request": "סינון", "files": ["shop/cart.py"]})
        await approve(live)
        change_id = (await done(live, job))["result"]["change_id"]
        before = await live.get(f"/api/dev/changes/{change_id}")

        mine = CART.replace("return sum(self.items)", "return sum(max(i, 0) for i in self.items)")
        edited = await live.http.put(f"/api/dev/changes/{change_id}/files", json={"path": "shop/cart.py", "content": mine})
        assert edited.status_code == 200 and edited.json()["files"][0]["edited"] is True
        dropped = await live.http.put(f"/api/dev/changes/{change_id}/files",
                                      json={"path": "tests/test_negative.py", "include": False})
        after = dropped.json()
        assert after["digest"] != before["digest"] and after["files"][1]["include"] is False

        await live.post(f"/api/dev/sessions/{session['id']}/write", {"allow": True})
        stale = await live.app.executor.execute("dev_apply", {"change_id": change_id, "digest": before["digest"]},
                                                agent_id="development", task_id=session["id"], lang="en")
        assert not stale.ok and stale.denied == "permission" and "approve it again" in (stale.error or "")
        assert b"max(i, 0)" not in (root / "shop" / "cart.py").read_bytes()

        job = await live.post(f"/api/dev/changes/{change_id}/apply", {})
        await approve(live)
        applied = await done(live, job)
        assert applied["status"] == "done", applied
        assert b"max(i, 0) for i in self.items)\r\n" in (root / "shop" / "cart.py").read_bytes()
        assert not (root / "tests" / "test_negative.py").exists()

        masked = await live.http.put(f"/api/dev/changes/{change_id}/files",
                                     json={"path": "shop/cart.py", "content": 'k = "[REDACTED]"'})
        assert masked.status_code == 409                      # applied files are edited by undoing first

    async def test_a_broken_proposal_is_shown_but_cannot_be_applied(self, dev: tuple[Live, FakeGrok],
                                                                   tmp_path: Path) -> None:
        live, grok = dev
        session = await open_project(live, project(tmp_path / "shop"))
        broken = proposal(edits=[
            {"path": "shop/cart.py", "action": "modify", "why": "x",
             "replacements": [{"find": "return sum(self.items)", "replace": "return sum(self.items"}]},
            {"path": "shop/other.py", "action": "modify", "why": "a file the model was not shown",
             "replacements": [{"find": "a", "replace": "b"}]},
            {"path": "../outside.py", "action": "create", "why": "x", "content": "x = 1\n"}])
        grok.push(answer(broken))
        job = await live.post(f"/api/dev/sessions/{session['id']}/propose", {"request": "x" * 5, "files": ["shop/cart.py"]})
        await approve(live)
        planned = await done(live, job)
        assert planned["status"] == "done" and len(planned["result"]["blocked"]) == 3
        change = await live.get(f"/api/dev/changes/{planned['result']['change_id']}")
        codes = [[p["code"] for p in f["problems"]] for f in change["files"]]
        assert codes == [["syntax"], ["missing"], ["path"]] and change["can_apply"] is False

    async def test_a_proposal_can_be_revised_in_words(self, dev: tuple[Live, FakeGrok], tmp_path: Path) -> None:
        live, grok = dev
        session = await open_project(live, project(tmp_path / "shop"))
        grok.push(answer(proposal(edits=[{"path": "shop/config.py", "action": "modify", "why": "timeout",
                                          "replacements": [{"find": "TIMEOUT = 5\n", "replace": "TIMEOUT = 10\n"}]}])))
        job = await live.post(f"/api/dev/sessions/{session['id']}/propose",
                              {"request": "להגדיל את הזמן", "files": ["shop/config.py"]})
        await approve(live)
        first = (await done(live, job))["result"]["change_id"]

        grok.push(answer(proposal(edits=[])))
        job = await live.post(f"/api/dev/sessions/{session['id']}/propose",
                              {"request": "לא 10, אלא 30", "files": ["shop/config.py"], "parent_id": first})
        await approve(live)
        assert (await done(live, job))["status"] == "done"
        sent = grok.requests[1].messages[0].text
        assert sent.startswith("REQUEST:\nלא 10, אלא 30") and "YOUR PREVIOUS PROPOSAL, NOT APPLIED" in sent
        assert "להגדיל את הזמן" in sent and "+TIMEOUT = 10" in sent and CONFIG_SECRET not in sent

    async def test_a_bad_answer_changes_nothing(self, dev: tuple[Live, FakeGrok], tmp_path: Path) -> None:
        live, grok = dev
        session = await open_project(live, project(tmp_path / "shop"))
        cut = AIResponse(text='{"understood": "x", "pl', stop_reason="max_tokens",
                         message=AIMessage(role="assistant", text="x"), usage=Usage(input_tokens=5, output_tokens=5))
        grok.push(cut)
        job = await live.post(f"/api/dev/sessions/{session['id']}/propose",
                              {"request": "x" * 5, "files": ["shop/cart.py"], "lang": "en"})
        await approve(live)
        failed = await done(live, job)
        assert failed["status"] == "failed" and "cut off" in failed["error"] and len(grok.requests) == 1


class TestTests:
    async def test_tests_run_after_approval_and_a_fix_gets_the_failures(self, dev: tuple[Live, FakeGrok],
                                                                       tmp_path: Path) -> None:
        live, grok = dev
        root = project(tmp_path / "shop")
        (root / "tests" / "test_total.py").write_text(
            "from shop.cart import Cart\n\n\ndef test_total():\n    c = Cart()\n    c.items = [2, 2]\n"
            "    assert c.total() == 5\n", encoding="utf-8")
        session = await open_project(live, root)
        job = await live.post(f"/api/dev/sessions/{session['id']}/tests", {"lang": "en"})
        asked = await approve(live)
        assert asked.tool == "run_tests" and asked.risk == "high"
        ran = await done(live, job, timeout=120)
        result = ran["result"]
        assert ran["status"] == "done" and result["ok"] is False, ran
        assert result["counts"] == {"failed": 1, "passed": 1} and result["runner"] == "pytest"
        assert result["failures"][0]["name"] == "tests/test_total.py::test_total"
        assert "python" in result["command"] and "-m pytest" in result["command"]

        def check(request: CompletionRequest) -> AIResponse:
            text = request.messages[0].text
            assert "Failing tests:" in text and "tests/test_total.py::test_total" in text
            return answer(proposal(edits=[], summary="the test expects 5 but 2 + 2 is 4"))

        grok.push(check)
        fix = await live.post(f"/api/dev/sessions/{session['id']}/fix", {"tests_job_id": ran["id"], "lang": "en"})
        assert fix["kind"] == "dev.fix"
        await approve(live)
        fixed = await done(live, fix)
        assert fixed["status"] == "done" and fixed["result"]["summary"] == "the test expects 5 but 2 + 2 is 4"

    async def test_a_project_without_tests_says_so(self, dev: tuple[Live, FakeGrok], tmp_path: Path) -> None:
        live, _grok = dev
        root = tmp_path / "notes"
        root.mkdir()
        (root / "notes.md").write_text("# notes\n", encoding="utf-8")
        session = await open_project(live, root)
        failed = await done(live, await live.post(f"/api/dev/sessions/{session['id']}/tests", {"lang": "en"}))
        assert failed["status"] == "failed" and "no tests" in failed["error"] and live.app.broker.pending() == []


class TestPrivacy:
    async def test_protected_folders_are_refused_and_changes_can_be_cleared(self, dev: tuple[Live, FakeGrok],
                                                                           tmp_path: Path) -> None:
        live, grok = dev
        r = await live.http.post("/api/dev/open", json={"path": str(live.app.paths.data), "lang": "en"})
        assert r.status_code == 409 and "protected" in r.json()["error"]["message"]
        r = await live.http.post("/api/dev/open", json={"path": str(tmp_path / "nope"), "lang": "en"})
        assert r.status_code == 409 and "not found" in r.json()["error"]["message"]

        session = await open_project(live, project(tmp_path / "shop"))
        grok.push(answer(proposal()))
        job = await live.post(f"/api/dev/sessions/{session['id']}/propose", {"request": "x" * 5, "files": ["shop/cart.py"]})
        await approve(live)
        change_id = (await done(live, job))["result"]["change_id"]
        assert live.app.dev.inventory()["changes"] == 1
        assert live.app.dev.clear()["changes"] == 1
        assert (await live.http.get(f"/api/dev/changes/{change_id}")).status_code == 404
