"""The whole app over its real local server: REST, WebSocket events, the access rules, no secret leaks.

`JarvisApp` is built exactly as the CLI builds it, on a temporary JARVIS_HOME, with the
desktop replaced by `FakeHost`, no AI provider, voice off and the server on a free port.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import websockets

import jarvis.agents.manager as manager_module
import jarvis.server.api as api_module
from fakes import FakeHost
from jarvis.ai.router import ProviderRouter
from jarvis.app import JarvisApp, read_runtime_file
from jarvis.common.cancel import CancelToken
from jarvis.common.enums import Mode
from jarvis.common.errors import PermissionDenied
from jarvis.config.settings import Settings
from jarvis.tools.base import ToolContext
from jarvis.tools.web import check_network

SECRET = "sk-ant-test-" + "9" * 24


class Live:
    def __init__(self, app: JarvisApp, host: FakeHost) -> None:
        self.app, self.host = app, host
        self.base = f"http://127.0.0.1:{app.port}"
        self.token = app.auth.token
        self.http = httpx.AsyncClient(base_url=self.base, headers={"X-JARVIS-Token": self.token}, timeout=30)

    async def get(self, path: str, **kw: Any) -> Any:
        r = await self.http.get(path, **kw)
        assert r.status_code == 200, r.text
        return r.json()

    async def post(self, path: str, body: Any = None, **kw: Any) -> Any:
        r = await self.http.post(path, json=body, **kw)
        assert r.status_code == 200, r.text
        return r.json()

    def ws_url(self, token: str | None = None) -> str:
        return f"ws://127.0.0.1:{self.app.port}/ws?token={token or self.token}"


class Events:
    """A WebSocket client that keeps every event it receives."""

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.items: list[dict[str, Any]] = []
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        try:
            async for raw in self.ws:
                self.items.append(json.loads(raw))
        except websockets.ConnectionClosed:
            pass

    def of(self, type_: str) -> list[Any]:
        return [e["data"] for e in self.items if e["type"] == type_]

    async def wait(self, type_: str, check: Any = lambda d: True, timeout: float = 15.0) -> Any:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            for data in self.of(type_):
                if check(data):
                    return data
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"no {type_} event matched; got {[e['type'] for e in self.items][-30:]}")
            await asyncio.sleep(0.02)

    async def close(self) -> None:
        await self.ws.close()
        self._reader.cancel()


@pytest.fixture
async def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Live]:
    monkeypatch.setattr(manager_module, "GRACE_S", 0.5)
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><html><head><title>JARVIS</title></head><body></body></html>",
                                       encoding="utf-8")
    (static / "assets" / "app.js").write_text("console.log('jarvis')", encoding="utf-8")
    monkeypatch.setattr(api_module, "STATIC_DIR", static)
    settings = Settings()
    host = FakeHost()
    app = JarvisApp(settings, host=host, router=ProviderRouter(settings.ai, []), voice=False, use_keyring=False,
                    port=0)
    await app.start()
    live = Live(app, host)
    try:
        yield live
    finally:
        await live.http.aclose()
        await app.shutdown()


async def connect(live: Live, **kw: Any) -> Events:
    ws = await websockets.connect(live.ws_url(**kw), max_size=None)
    return Events(ws)


# =========================================================================== access rules
class TestAccess:
    async def test_health_is_public_everything_else_needs_the_token(self, live: Live) -> None:
        async with httpx.AsyncClient(base_url=live.base) as anon:
            assert (await anon.get("/api/health")).json()["ok"] is True
            r = await anon.get("/api/state")
            assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
            r = await anon.get("/api/state", headers={"X-JARVIS-Token": "wrong"})
            assert r.status_code == 403
            r = await anon.post("/api/command", json={"text": "מה השעה"})
            assert r.status_code == 403
        assert (await live.get("/api/state"))["version"]

    async def test_foreign_host_and_origin_are_refused(self, live: Live) -> None:
        # DNS rebinding: a page on evil.example resolving to 127.0.0.1 sends Host: evil.example
        r = await live.http.get("/api/state", headers={"Host": f"evil.example:{live.app.port}"})
        assert r.status_code == 403
        r = await live.http.post("/api/command", json={"text": "open notepad"},
                                 headers={"Origin": "https://evil.example"})
        assert r.status_code == 403 and not live.host.calls
        r = await live.http.get("/api/state", headers={"Origin": live.base})
        assert r.status_code == 200

    async def test_websocket_needs_token_and_same_origin(self, live: Live) -> None:
        with pytest.raises(websockets.InvalidStatus):
            await websockets.connect(f"ws://127.0.0.1:{live.app.port}/ws?token=nope")
        with pytest.raises(websockets.InvalidStatus):
            await websockets.connect(live.ws_url(), origin="https://evil.example")
        events = await connect(live)
        await events.wait("hello")
        await events.close()

    async def test_the_page_needs_a_launch_session(self, live: Live) -> None:
        async with httpx.AsyncClient(base_url=live.base) as browser:
            r = await browser.get("/")
            assert r.status_code == 401 and live.token not in r.text and "JARVIS" in r.text
            r = await browser.get("/?launch=wrong")
            assert r.status_code == 403
            r = await browser.get(f"/?launch={live.token}")
            assert r.status_code == 303 and r.headers["location"] == "/"
            cookie = r.headers["set-cookie"]
            assert "httponly" in cookie.lower() and "samesite=strict" in cookie.lower()
            r = await browser.get("/")                                # the cookie came with the redirect
            assert r.status_code == 200
            assert f'"token": "{live.token}"' in r.text and "window.__JARVIS__" in r.text
            csp = r.headers["content-security-policy"]
            nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
            assert f'nonce="{nonce}"' in r.text and "object-src 'none'" in csp
            assert (await browser.get("/tasks")).status_code == 200    # app routes serve the page too
            assert (await browser.get("/assets/app.js")).text == "console.log('jarvis')"
            assert (await browser.get("/assets/../index.html")).status_code in (401, 404)
            assert (await browser.get("/nothing.js")).status_code == 404
        async with httpx.AsyncClient(base_url=live.base) as browser:
            r = await browser.get(f"/?launch={live.token}&view=orb")
            assert r.headers["location"] == "/?view=orb"

    async def test_runtime_file_lets_the_cli_find_this_run(self, live: Live) -> None:
        info = read_runtime_file(live.app.paths)
        assert info == {**info, "pid": os.getpid(), "port": live.app.port, "token": live.token}
        if os.name == "posix":
            assert (live.app.runtime_file.stat().st_mode & 0o077) == 0      # readable by this user only

    async def test_agents_cannot_call_the_control_server(self, live: Live) -> None:
        ctx = ToolContext(agent_id="research", task_id=None, subtask_id=None, mode=Mode.NORMAL, cancel=CancelToken(),
                          services=live.app.services)
        for url in (f"http://127.0.0.1:{live.app.port}/api/state", f"http://localhost:{live.app.port}/",
                    f"http://[::1]:{live.app.port}/ws"):
            with pytest.raises(PermissionDenied):
                check_network(ctx, url)
        assert check_network(ctx, "http://127.0.0.1:5173/") == "127.0.0.1"       # the dev server it built


# =========================================================================== REST
class TestRest:
    async def test_state_snapshot(self, live: Live) -> None:
        state = await live.get("/api/state")
        assert [a["id"] for a in state["agents"]] == ["research", "system", "development", "creative", "planner"]
        assert state["mode"] == "normal" and state["kill_switch"]["global"] is False
        assert state["voice"]["state"] == "disabled" and state["providers"]["active"] is None
        assert set(state["ui"]) == {"theme_color", "orb_size", "orb_opacity", "follow_mouse", "always_active",
                                    "mini_orb"}
        assert state["files"]["workspace"] == str(live.app.paths.workspace)
        assert live.token not in json.dumps(state)

    async def test_command_runs_through_the_core(self, live: Live) -> None:
        res = await live.post("/api/command", {"text": "מה השעה"})
        assert res["accepted"] and res["reply"]["text"].startswith("השעה")
        res = await live.post("/api/command", {"text": "open notepad"})
        assert res["intent"]["name"] == "open_app" and ("launch", "Notepad", ()) in live.host.calls
        chat = await live.get("/api/chat")
        assert [m["role"] for m in chat[-4:]] == ["user", "jarvis", "user", "jarvis"]
        assert (await live.http.delete("/api/chat")).json() == {"ok": True}
        assert await live.get("/api/chat") == []
        r = await live.http.post("/api/command", json={"text": "x" * 9000})
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid"

    async def test_agents_and_tasks(self, live: Live) -> None:
        agent = await live.get("/api/agents/system")
        assert agent["status"] == "idle" and "activity" in agent
        assert (await live.post("/api/agents/system/action", {"action": "stop"}))["enabled"] is False
        assert (await live.post("/api/agents/system/action", {"action": "start"}))["enabled"] is True
        assert (await live.http.get("/api/agents/eighth")).status_code == 404
        res = await live.post("/api/agents/planner/task", {"instruction": "תכנן טיול של שלושה ימים בצפון"})
        task = await live.app.orchestrator.wait(res["task_id"], timeout=60)
        assert task.status.value in ("completed", "failed")
        detail = await live.get(f"/api/tasks/{task.id}")
        assert detail["plan"]["subtasks"][0]["agent"] == "planner" and detail["timeline"]
        assert task.id in [t["id"] for t in await live.get("/api/tasks")]
        assert (await live.http.get("/api/tasks/task_nope")).status_code == 404
        r = await live.http.post(f"/api/tasks/{task.id}/resume")
        assert r.status_code in (200, 409)

    async def test_kill_switch_and_mode(self, live: Live) -> None:
        events = await connect(live)
        ks = await live.post("/api/killswitch", {"engage": True, "reason": "test"})
        assert ks["global"] is True
        await events.wait("killswitch", lambda d: d["global"] is True)
        r = await live.http.post("/api/agents/system/task", json={"instruction": "create a folder"})
        assert r.status_code == 409 and "kill switch" in r.json()["error"]["message"]
        assert (await live.post("/api/killswitch", {"engage": False}))["global"] is False
        assert (await live.post("/api/killswitch", {"engage": True, "agent_id": "research"}))["agents"] == \
            {"research": "UI"}
        await live.post("/api/killswitch", {"engage": False, "agent_id": "research"})
        assert (await live.post("/api/mode", {"mode": "task"}))["mode"] == "task"
        await events.wait("mode", lambda d: d["mode"] == "task")
        r = await live.http.post("/api/mode", json={"mode": "turbo"})
        assert r.status_code == 422
        await events.close()

    async def test_permissions(self, live: Live) -> None:
        perms = await live.get("/api/permissions")
        assert "fs.write" in perms["capabilities"] and "research" in perms["agents"]
        g = await live.post("/api/permissions/grant", {"agent_id": "research", "capability": "fs.write",
                                                         "roots": [str(live.app.paths.workspace)], "ttl_s": 600})
        assert g["capability"] == "fs.write" and g["source"] == "user"
        assert (await live.http.post("/api/permissions/grant", json={"agent_id": "research",
                                                                      "capability": "root"})).status_code == 400
        assert (await live.http.delete(f"/api/permissions/grants/{g['id']}")).json() == {"ok": True}
        assert (await live.http.delete(f"/api/permissions/grants/{g['id']}")).status_code == 404
        await live.post("/api/permissions/agents/development/restrict", {"task_id": "task_x"})
        assert (await live.get("/api/permissions"))["agents"]["development"]["restricted_to_task"] == "task_x"
        await live.post("/api/permissions/agents/development/restrict", {"task_id": None})
        assert (await live.get("/api/permissions"))["agents"]["development"]["restricted_to_task"] is None

    async def test_memory(self, live: Live) -> None:
        fact = await live.post("/api/memory/facts", {"text": "I prefer dark themes", "category": "preference"})
        assert fact["category"] == "preference"
        assert [f["id"] for f in await live.get("/api/memory/facts")] == [fact["id"]]
        hits = await live.get("/api/memory/search", params={"q": "dark"})
        assert any("dark" in h["text"] for h in hits)
        assert (await live.http.delete(f"/api/memory/facts/{fact['id']}")).json() == {"ok": True}
        assert await live.get("/api/memory/facts") == []

    async def test_files(self, live: Live, tmp_path: Path) -> None:
        ws = live.app.paths.workspace
        (ws / "notes").mkdir()
        (ws / "notes" / "a.txt").write_text("hi", encoding="utf-8")
        (ws / "setup.exe").write_bytes(b"MZ")
        listing = await live.get("/api/files/list")
        assert [e["name"] for e in listing["entries"]][:1] == ["notes"]
        assert (await live.http.get("/api/files/list", params={"path": str(tmp_path)})).status_code == 403
        assert (await live.post("/api/files/open", {"path": str(ws / "notes" / "a.txt")}))["revealed"] is False
        assert ("open_path", str(ws / "notes" / "a.txt")) in live.host.calls
        # a program is only shown in Explorer, never started from a click
        assert (await live.post("/api/files/open", {"path": str(ws / "setup.exe")}))["revealed"] is True
        assert ("reveal", str(ws / "setup.exe")) in live.host.calls
        r = await live.http.post("/api/files/open", json={"path": str(live.app.paths.data / "jarvis.db")})
        assert r.status_code == 403
        r = await live.http.post("/api/files/open", json={"path": str(ws / "missing.txt")})
        assert r.status_code == 404

    async def test_settings(self, live: Live) -> None:
        events = await connect(live)
        out = (await live.http.patch("/api/settings", json={"ui": {"theme_color": "green"},
                                                             "general": {"user_name": "דוד"}})).json()
        assert out["ui"]["theme_color"] == "green" and live.app.settings.general.user_name == "דוד"
        await events.wait("settings", lambda d: d["ui"]["theme_color"] == "green")
        saved = live.app.config_file.read_text(encoding="utf-8")
        assert 'theme_color = "green"' in saved
        r = await live.http.patch("/api/settings", json={"ui": {"theme_color": "pink"}})
        assert r.status_code == 400 and live.app.settings.ui.theme_color == "green"
        r = await live.http.patch("/api/settings", content=b"[1, 2]", headers={"Content-Type": "application/json"})
        assert r.status_code == 400
        await events.close()

    async def test_quick_tools_and_window(self, live: Live) -> None:
        tools = await live.get("/api/quick-tools")
        assert [t["id"] for t in tools] == ["chrome", "youtube", "calculator", "settings", "files"]
        assert tools[0]["command"] == "פתח את כרום"
        assert (await live.get("/api/quick-tools", params={"lang": "en"}))[0]["command"] == "open Chrome"
        assert (await live.http.post("/api/window/show")).status_code == 409        # no desktop window here
        seen: list[str] = []
        live.app.window_handler = lambda action: seen.append(action) or True
        assert (await live.post("/api/window/mini")) == {"ok": True} and seen == ["mini"]

    async def test_voice_off(self, live: Live) -> None:
        info = await live.post("/api/voice/mute")
        assert info["state"] == "disabled" and info["enabled"] is False
        assert (await live.post("/api/voice/speak", {"text": "שלום"})) == {"ok": False}
        assert (await live.http.post("/api/voice/explode")).status_code == 422


# =========================================================================== live events
class TestEvents:
    async def test_hello_state_then_live_events(self, live: Live) -> None:
        events = await connect(live)
        hello = await events.wait("hello")
        assert hello["version"]
        state = await events.wait("state")
        assert len(state["agents"]) == 5
        await events.ws.send(json.dumps({"type": "ping"}))
        await events.wait("pong")
        await events.ws.send(json.dumps({"type": "command", "text": "open calculator"}))
        await events.wait("chat.message", lambda d: d["role"] == "user" and d["text"] == "open calculator")
        await events.wait("action", lambda d: d["status"] == "done")
        await events.wait("agent.status", lambda d: d["id"] == "system")
        await events.wait("activity", lambda d: "Calculator" in d["message"] or "מחשבון" in d["message"])
        await events.wait("chat.message", lambda d: d["role"] == "jarvis" and d["kind"] == "result")
        await events.wait("system.stats", lambda d: "cpu" in d, timeout=10)
        await events.close()

    async def test_tasks_stream_their_progress(self, live: Live) -> None:
        events = await connect(live)
        res = await live.post("/api/agents/planner/task", {"instruction": "plan a weekend in Tel Aviv"})
        await events.wait("task.updated", lambda d: d["id"] == res["task_id"])
        await events.wait("task.subtask", lambda d: d["task_id"] == res["task_id"])
        await events.wait("task.timeline", lambda d: d["task_id"] == res["task_id"])
        await events.wait("task.updated", lambda d: d["id"] == res["task_id"] and d["status"] in ("completed", "failed"),
                          timeout=60)
        await events.wait("notification", lambda d: d["level"] in ("success", "error"))
        assert any(c[0] == "notify" for c in live.host.calls)              # the window is not on screen
        await events.close()

    async def test_approval_from_the_ui(self, live: Live) -> None:
        events = await connect(live)
        await events.ws.send(json.dumps({"type": "command", "text": "shut down the computer"}))
        req = await events.wait("confirm.request")
        assert req["risk"] in ("high", "critical") and req["status"] == "pending"
        assert [c["id"] for c in await live.get("/api/confirmations")] == [req["id"]]
        await live.post(f"/api/confirmations/{req['id']}", {"approved": False})
        decided = await events.wait("confirm.response", lambda d: d["id"] == req["id"])
        assert decided["status"] == "denied"
        assert not any(c[0] == "power" for c in live.host.calls)
        assert (await live.http.post(f"/api/confirmations/{req['id']}", json={"approved": True})).status_code == 404
        await events.close()


# =========================================================================== secrets never leave
class TestSecrets:
    async def test_keys_are_stored_but_never_shown(self, live: Live) -> None:
        events = await connect(live)
        r = await live.http.put("/api/secrets/ANTHROPIC_API_KEY", json={"value": SECRET})
        assert r.json() == {"configured": True, "source": "file"}
        status = await live.get("/api/secrets")
        assert status["ANTHROPIC_API_KEY"]["configured"] is True
        assert (await live.http.put("/api/secrets/ROOT_PASSWORD", json={"value": "x" * 10})).status_code == 404
        # Even if the key is typed into the chat, it is masked everywhere it shows up.
        await live.post("/api/command", {"text": f"remember that my key is {SECRET}"})
        await events.wait("chat.message", lambda d: d["role"] == "user")
        await asyncio.sleep(0.3)
        for path in ("/api/state", "/api/chat", "/api/activity", "/api/memory/facts", "/api/settings",
                     "/api/secrets", "/api/audit"):
            assert SECRET not in (await live.http.get(path)).text, path
        assert SECRET not in json.dumps(events.items)
        assert any("[REDACTED" in json.dumps(d) for d in events.of("chat.message"))
        logs = "".join(p.read_text(encoding="utf-8", errors="replace") for p in live.app.paths.logs.glob("*.log*"))
        assert SECRET not in logs
        assert (await live.http.delete("/api/secrets/ANTHROPIC_API_KEY")).json() == {"configured": False}
        await events.close()
