"""The "Apps & tools" routes over the real local server: what is possible here, the lists, opening an app, and
a look, a suggestion and a step that each wait for the approval on the socket before anything happens.

This machine has no Windows desktop, so the screen jobs first fail with that reason; then the screen and the
model are replaced by the same fakes the unit tests use, and the whole flow runs through the real gate."""

from __future__ import annotations

import base64

import pytest

from integration.test_server import Live, connect, live  # noqa: F401  (the running-app fixture)
from unit.test_apps import PNG, FakeBackend, FakeHolo, FakeModelServer, _click, acted


def _fake_screen(live: Live, monkeypatch: pytest.MonkeyPatch, holo: FakeHolo) -> FakeBackend:  # noqa: F811
    from jarvis.computer import service as svc

    backend = FakeBackend()
    computer = live.app.computer
    computer.backend = backend                       # type: ignore[assignment]
    computer.server = FakeModelServer()              # type: ignore[assignment]
    computer.settings.settle_s = 0.0
    monkeypatch.setattr(svc, "HoloClient", lambda *a, **k: holo)
    return backend


class TestAppsRoutes:
    async def test_status_lists_and_opening_an_app(self, live: Live) -> None:  # noqa: F811
        status = await live.get("/api/apps/status", params={"lang": "en"})
        ways = {w["key"]: w for w in status["ways"]}
        assert set(ways) == {"launch", "windows", "uia", "vision", "connectors"}
        assert not ways["uia"]["available"] and "Windows" in ways["uia"]["reason"]   # no desktop here
        assert [s["key"] for s in status["stop"]] == ["stop_button", "esc_twice", "kill_switch"]
        assert (await live.get("/api/apps/status", params={"lang": "he"}))["note"].startswith("שום דבר")

        found = await live.get("/api/apps/list", params={"q": "note", "lang": "en"})
        assert [a["name"] for a in found["installed"]] == ["Notepad"] and found["windows"] == []
        assert found["total"] == 3 and found["matched"] == 1
        assert (await live.get("/api/apps/list", params={"refresh": "1"}))["matched"] == 3

        events = await connect(live)
        await events.wait("hello")
        job = await live.post("/api/apps/open", {"name": "Notepad", "lang": "en"})
        assert job["kind"] == "apps.open" and job["area"] == "apps" and job["status"] == "running"
        done = await events.wait("job", lambda d: d["id"] == job["id"] and d["status"] != "running")
        assert done["status"] == "done" and done["result"]["summary"] == "Opened Notepad"
        assert ("launch", "Notepad", ()) in live.host.calls
        windows = (await live.get("/api/apps/list"))["windows"]
        assert [w["program"] for w in windows] == ["notepad"]
        await events.close()

    async def test_without_a_windows_desktop_the_screen_jobs_say_why(self, live: Live) -> None:  # noqa: F811
        job = await live.post("/api/apps/look", {"lang": "en"})
        done = await live.app.jobs.wait(job["id"], 10)
        assert done is not None and done.status == "failed" and "Windows" in (done.error or "")
        assert live.app.broker.pending() == []                  # refused before asking anything
        suggested = await live.post("/api/apps/suggest", {"goal": "save the file", "lang": "en"})
        done = await live.app.jobs.wait(suggested["id"], 10)
        assert done is not None and done.status == "failed" and "Windows" in (done.error or "")

    async def test_look_suggest_and_step_wait_for_the_approval(self, live: Live,  # noqa: F811
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
        backend = _fake_screen(live, monkeypatch, FakeHolo(steps=[_click()]))
        events = await connect(live)
        await events.wait("hello")

        look = await live.post("/api/apps/look", {"lang": "en"})
        task_id = look["params"]["task_id"]
        asked = await events.wait("confirm.request", lambda d: d["tool"] == "read_screen")
        assert backend.calls == []                              # nothing seen before the approval
        assert [c["id"] for c in await live.get("/api/confirmations")] == [asked["id"]]
        await live.post(f"/api/confirmations/{asked['id']}", {"approved": True})
        seen = await events.wait("job", lambda d: d["id"] == look["id"] and d["status"] != "running")
        assert seen["status"] == "done" and seen["result"]["look"]["description"] == "Notepad, an empty document"

        task = await live.post("/api/apps/correct", {"text": "it is the Save dialog", "task_id": task_id})
        assert task["task"]["corrections"] == ["it is the Save dialog"]

        suggest = await live.post("/api/apps/suggest", {"goal": "save the file", "task_id": task_id, "lang": "en"})
        asked = await events.wait("confirm.request", lambda d: d["tool"] == "read_screen" and d["id"] != asked["id"])
        await live.post(f"/api/confirmations/{asked['id']}", {"approved": True})
        proposed = await events.wait("job", lambda d: d["id"] == suggest["id"] and d["status"] != "running")
        assert proposed["status"] == "done" and proposed["result"]["proposal"]["can_do"] is True
        assert acted(backend) == []                             # a suggestion does nothing

        step = await live.post("/api/apps/step", {"suggestion": suggest["id"], "lang": "en"})
        asked = await events.wait("confirm.request", lambda d: d["tool"] == "do_screen_step")
        assert asked["always_ask"] is True and asked["summary"] == "Click 'Save' in Untitled - Notepad"
        assert acted(backend) == []                             # nothing done before the approval
        await live.post(f"/api/confirmations/{asked['id']}", {"approved": True})
        did = await events.wait("job", lambda d: d["id"] == step["id"] and d["status"] != "running")
        assert did["status"] == "done", did["error"]
        assert acted(backend) == [("click", 960, 540, "left", 1)]

        again = await live.http.post("/api/apps/step", json={"suggestion": suggest["id"]})
        assert again.status_code == 409 and again.json()["error"]["message"]
        assert (await live.http.delete(f"/api/apps/task/{task_id}")).json() == {"ended": True}

        history = live.app.jobs.history_file.read_text(encoding="utf-8")          # type: ignore[union-attr]
        assert PNG.decode("latin-1") not in history and base64.b64encode(PNG).decode() not in history
        assert "an empty document" not in history                                 # only the summaries are kept
        await events.close()

    async def test_bad_requests_and_the_kill_switch(self, live: Live) -> None:  # noqa: F811
        assert (await live.http.post("/api/apps/suggest", json={"goal": ""})).status_code == 422
        assert (await live.http.post("/api/apps/step", json={"suggestion": "job_nope"})).status_code == 404
        assert (await live.http.post("/api/apps/correct", json={"text": "   "})).status_code == 400
        await live.post("/api/killswitch", {"engage": True, "reason": "test"})
        r = await live.http.post("/api/apps/look", json={"lang": "en"})
        assert r.status_code == 409 and "kill switch" in r.json()["error"]["message"]
        await live.post("/api/killswitch", {"engage": False})
