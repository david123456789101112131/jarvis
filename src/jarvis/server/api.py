"""The local API: REST + one WebSocket for the UI (contract: docs/API.md).

Everything here is thin: each route validates its input and calls the service that owns the
work (core, orchestrator, agent manager, memory, permissions, voice). The server never
touches the OS or the agents' tools itself, and it never returns a secret value.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, ValidationError
from starlette.types import ASGIApp, Receive, Scope, Send

from jarvis import __version__
from jarvis.common.enums import AgentId, Capability, Mode
from jarvis.common.errors import ConfigError, TaskRefused
from jarvis.security.secrets import KNOWN_SECRETS
from jarvis.server.auth import COOKIE, HEADER, ServerAuth
from jarvis.server.hub import Client

if TYPE_CHECKING:
    from jarvis.app import JarvisApp

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "ui" / "static"
_PUBLIC_PATHS = {"/api/health", "/favicon.ico"}


# ======================================================================================= bodies
class CommandBody(BaseModel):
    text: str = Field(max_length=8000)
    source: Literal["text", "voice"] = "text"


class AgentActionBody(BaseModel):
    action: Literal["start", "stop", "restart", "kill", "release"]


class AgentTaskBody(BaseModel):
    instruction: str = Field(min_length=1, max_length=8000)
    priority: Literal["critical", "high", "normal", "low"] | None = None


class GrantBody(BaseModel):
    agent_id: str
    capability: str
    roots: list[str] = Field(default_factory=list)
    task_id: str | None = None
    ttl_s: float | None = Field(default=None, gt=0)
    max_uses: int | None = Field(default=None, gt=0)
    reason: str = ""


class RestrictBody(BaseModel):
    task_id: str | None = None
    folders: list[str] | None = None


class ConfirmBody(BaseModel):
    approved: bool
    remember: bool = False


class KillSwitchBody(BaseModel):
    engage: bool
    agent_id: str | None = None
    reason: str | None = Field(default=None, max_length=300)


class ModeBody(BaseModel):
    mode: Mode


class FactBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    category: Literal["preference", "fact", "decision", "note"] = "fact"


class OpenFileBody(BaseModel):
    path: str
    reveal: bool = False


class SecretBody(BaseModel):
    value: str = Field(min_length=1, max_length=4000)


class SpeakBody(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


# ======================================================================================= helpers
def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _agent(raw: str) -> AgentId:
    try:
        return AgentId.parse(raw)
    except (ValueError, KeyError) as exc:
        raise HTTPException(404, f"Unknown agent: {raw}") from exc


def _limit(value: int, top: int) -> int:
    return max(1, min(int(value), top))


def _dump(obj: Any) -> Any:
    return obj.model_dump(mode="json") if isinstance(obj, BaseModel) else obj


MAX_BODY = 1_000_000      # bytes; every request body is a small JSON document


class GuardMiddleware:
    """Loopback `Host`, same-origin `Origin`, the token on everything but the public paths, and a
    size limit on request bodies."""

    def __init__(self, app: ASGIApp, auth: ServerAuth) -> None:
        self.app = app
        self.auth = auth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        path: str = scope.get("path") or "/"
        problem = None
        if not self.auth.host_ok(headers.get("host")):
            problem = "This server only answers requests made to 127.0.0.1."
        elif not self.auth.origin_ok(headers.get("origin")):
            problem = "Requests from other web pages are not allowed."
        elif (path.startswith("/api/") and path not in _PUBLIC_PATHS) or path == "/ws":
            cookie = _cookie(headers.get("cookie"), COOKIE)
            query = _query_token(scope) if scope["type"] == "websocket" else None
            if not self.auth.authorized(header=headers.get(HEADER), cookie=cookie, query=query):
                problem = "Missing or wrong JARVIS token."
        status, code = 403, "forbidden"
        if problem is None and scope["type"] == "http":
            length = headers.get("content-length")
            if length is not None:
                if not length.strip().isdigit() or int(length) > MAX_BODY:
                    problem, status, code = "The request is too large.", 413, "too_large"
            else:
                # Sent without a length (chunked): read it here, up to the limit, then hand it on.
                body = await _read_body(receive, MAX_BODY)
                if body is None:
                    problem, status, code = "The request is too large.", 413, "too_large"
                else:
                    receive = _replay(body, receive)
        if problem is None:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4403, "reason": problem})
            return
        body = json.dumps({"error": {"code": code, "message": problem}}).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


async def _read_body(receive: Receive, limit: int) -> bytes | None:
    """The whole request body, or None once it passes `limit` bytes (or the client went away)."""
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message.get("type") != "http.request":
            return None
        chunk = message.get("body") or b""
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
        if not message.get("more_body"):
            return b"".join(chunks)


def _replay(body: bytes, receive: Receive) -> Receive:
    """Hand a body that was already read to the app, then pass on what comes next (a disconnect)."""
    sent = False

    async def wrapped() -> dict:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return wrapped


def _cookie(header: str | None, name: str) -> str | None:
    if not header:
        return None
    for part in header.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


def _query_token(scope: Scope) -> str | None:
    from urllib.parse import parse_qs

    values = parse_qs((scope.get("query_string") or b"").decode("latin-1")).get("token")
    return values[0] if values else None


# ======================================================================================= the app
def create_api(app: JarvisApp) -> FastAPI:
    auth = app.auth
    api = FastAPI(title="JARVIS", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    api.add_middleware(GuardMiddleware, auth=auth)

    # ---------------------------------------------------------------------------- errors
    @api.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
        code = {400: "bad_request", 403: "forbidden", 404: "not_found", 409: "conflict"}.get(exc.status_code, "error")
        return _error(exc.status_code, code, str(exc.detail))

    @api.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        return _error(422, "invalid", f"{where}: {first.get('msg', 'invalid request')}".strip(": "))

    @api.exception_handler(TaskRefused)
    async def _refused(request: Request, exc: TaskRefused) -> JSONResponse:
        return _error(409, "refused", exc.message)

    @api.exception_handler(Exception)
    async def _crash(request: Request, exc: Exception) -> JSONResponse:
        log.exception("API %s %s failed", request.method, request.url.path)
        return _error(500, "internal_error", "JARVIS hit an internal error; the details are in the log.")

    # ---------------------------------------------------------------------------- status
    @api.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    @api.get("/api/state")
    async def state() -> dict[str, Any]:
        return await app.state_snapshot()

    # ---------------------------------------------------------------------------- commands & chat
    @api.post("/api/command")
    async def command(body: CommandBody) -> dict[str, Any]:
        result = await app.core.handle_input(body.text, source=body.source)
        return result.view()

    @api.get("/api/chat")
    async def chat(limit: int = 100) -> list[dict[str, Any]]:
        return await app.core.chat_history(_limit(limit, 500))

    @api.delete("/api/chat")
    async def clear_chat() -> dict[str, Any]:
        await app.core.clear_chat()
        return {"ok": True}

    # ---------------------------------------------------------------------------- agents
    @api.get("/api/agents")
    async def agents() -> list[dict[str, Any]]:
        return app.manager.views()

    @api.get("/api/agents/{agent_id}")
    async def agent(agent_id: str) -> dict[str, Any]:
        a = _agent(agent_id)
        view = app.manager.view(a)
        view["activity"] = [_dump(e) for e in app.activity.recent(100, agent_id=a.value)]
        return view

    @api.post("/api/agents/{agent_id}/action")
    async def agent_action(agent_id: str, body: AgentActionBody) -> dict[str, Any]:
        a = _agent(agent_id)
        if body.action == "start":
            return await app.manager.start_agent(a)
        if body.action == "stop":
            return await app.manager.stop_agent(a)
        if body.action == "restart":
            return await app.manager.restart_agent(a)
        if body.action == "kill":
            await app.core.engage_kill_switch("UI", agent_id=a.value)
        else:
            await app.core.release_kill_switch(agent_id=a.value)
        return app.manager.view(a)

    @api.post("/api/agents/{agent_id}/task")
    async def agent_task(agent_id: str, body: AgentTaskBody) -> dict[str, Any]:
        a = _agent(agent_id)
        if app.kill_switch.global_engaged:
            raise HTTPException(409, "The kill switch is engaged. Release it first.")
        task = await app.core.agent_task(a, body.instruction, priority=body.priority, source="api")
        return {"task_id": task.id}

    # ---------------------------------------------------------------------------- tasks
    @api.get("/api/tasks")
    async def tasks(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return await app.orchestrator.list_tasks(status=status or None, limit=_limit(limit, 500))

    @api.get("/api/tasks/{task_id}")
    async def task_detail(task_id: str) -> dict[str, Any]:
        detail = await app.orchestrator.detail(task_id)
        if detail is None:
            raise HTTPException(404, f"No task {task_id}")
        return detail

    @api.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str) -> dict[str, Any]:
        task = await app.orchestrator.cancel(task_id)
        if task is None:
            raise HTTPException(404, f"No task {task_id}")
        return task.summary_view()

    @api.post("/api/tasks/{task_id}/resume")
    async def resume_task(task_id: str) -> dict[str, Any]:
        if app.kill_switch.global_engaged:
            raise HTTPException(409, "The kill switch is engaged. Release it first.")
        try:
            task = await app.orchestrator.resume(task_id)
        except KeyError as exc:
            raise HTTPException(404, f"No task {task_id}") from exc
        return task.summary_view()

    @api.post("/api/tasks/{task_id}/retry")
    async def retry_task(task_id: str) -> dict[str, Any]:
        if app.kill_switch.global_engaged:
            raise HTTPException(409, "The kill switch is engaged. Release it first.")
        try:
            task = await app.orchestrator.retry(task_id)
        except KeyError as exc:
            raise HTTPException(404, f"No task {task_id}") from exc
        return task.summary_view()

    # ---------------------------------------------------------------------------- system & logs
    @api.get("/api/system")
    async def system() -> dict[str, Any]:
        return app.last_stats or await app.sample_system()

    @api.get("/api/system/processes")
    async def processes(limit: int = 15) -> list[dict[str, Any]]:
        from jarvis.tools.apps import top_processes

        return await asyncio.to_thread(top_processes, _limit(limit, 100), "cpu")

    @api.get("/api/activity")
    async def activity(limit: int = 200, task_id: str | None = None, agent_id: str | None = None) -> list[Any]:
        return [_dump(e) for e in app.activity.recent(_limit(limit, 1500), task_id=task_id, agent_id=agent_id)]

    @api.get("/api/audit")
    async def audit(limit: int = 200) -> list[Any]:
        return [_dump(e) for e in app.audit.recent(_limit(limit, 3000))]

    # ---------------------------------------------------------------------------- permissions
    @api.get("/api/permissions")
    async def permissions() -> dict[str, Any]:
        return {"agents": app.permissions.snapshot(), "capabilities": [c.value for c in Capability]}

    @api.post("/api/permissions/grant")
    async def grant(body: GrantBody) -> dict[str, Any]:
        a = _agent(body.agent_id)
        try:
            cap = Capability(body.capability)
        except ValueError as exc:
            raise HTTPException(400, f"Unknown capability: {body.capability}") from exc
        g = app.permissions.grant(a.value, cap, roots=body.roots, task_id=body.task_id, ttl_s=body.ttl_s,
                                  max_uses=body.max_uses, source="user", reason=body.reason or "granted in the UI")
        await app.activity.log(f"Permission '{cap.value}' granted to the {a.value} agent", source="Security",
                               agent_id=a.value, kind="permission")
        return g.model_dump(mode="json")

    @api.delete("/api/permissions/grants/{grant_id}")
    async def revoke(grant_id: str) -> dict[str, Any]:
        if not app.permissions.revoke(grant_id):
            raise HTTPException(404, f"No grant {grant_id}")
        await app.activity.log("A permission grant was revoked", source="Security", kind="permission")
        return {"ok": True}

    @api.post("/api/permissions/agents/{agent_id}/reset")
    async def reset_permissions(agent_id: str) -> dict[str, Any]:
        a = _agent(agent_id)
        app.permissions.reset_agent(a.value)
        await app.activity.log(f"Permissions of the {a.value} agent reset to the defaults", source="Security",
                               agent_id=a.value, kind="permission")
        return {"ok": True}

    @api.post("/api/permissions/agents/{agent_id}/restrict")
    async def restrict(agent_id: str, body: RestrictBody) -> dict[str, Any]:
        a = _agent(agent_id)
        if "task_id" in body.model_fields_set:
            app.permissions.restrict_to_task(a.value, body.task_id)
        if "folders" in body.model_fields_set:
            app.permissions.restrict_folders(a.value, body.folders)
        return {"ok": True}

    # ---------------------------------------------------------------------------- approvals & control
    @api.get("/api/confirmations")
    async def confirmations() -> list[dict[str, Any]]:
        return [r.public() for r in app.broker.pending()]

    @api.post("/api/confirmations/{request_id}")
    async def confirm(request_id: str, body: ConfirmBody) -> dict[str, Any]:
        if not app.broker.respond(request_id, body.approved, by="ui", remember=body.remember):
            raise HTTPException(404, "This approval request is no longer waiting.")
        return {"ok": True}

    @api.post("/api/killswitch")
    async def killswitch(body: KillSwitchBody) -> dict[str, Any]:
        agent_id = _agent(body.agent_id).value if body.agent_id else None
        if body.engage:
            await app.core.engage_kill_switch(body.reason or "UI", agent_id=agent_id)
        else:
            await app.core.release_kill_switch(agent_id=agent_id)
        return app.kill_switch.snapshot()

    @api.post("/api/mode")
    async def set_mode(body: ModeBody) -> dict[str, Any]:
        mode = await app.core.set_mode(body.mode, source="ui")
        return {"mode": mode.value}

    # ---------------------------------------------------------------------------- memory & files
    @api.get("/api/memory/facts")
    async def facts(q: str | None = None) -> list[dict[str, Any]]:
        return [_dump(f) for f in await app.memory.facts(query=q or None)]

    @api.post("/api/memory/facts")
    async def add_fact(body: FactBody) -> dict[str, Any]:
        return _dump(await app.memory.remember(body.text, body.category, source="ui"))

    @api.delete("/api/memory/facts/{fact_id}")
    async def delete_fact(fact_id: str) -> dict[str, Any]:
        if not await app.memory.forget(fact_id):
            raise HTTPException(404, f"No fact {fact_id}")
        return {"ok": True}

    @api.get("/api/memory/search")
    async def search_memory(q: str = Query(min_length=1), limit: int = 20) -> list[dict[str, Any]]:
        return [_dump(h) for h in await app.memory.search(q, limit=_limit(limit, 100))]

    @api.get("/api/memory/projects")
    async def projects() -> list[dict[str, Any]]:
        return [_dump(p) for p in await app.memory.projects()]

    @api.get("/api/files/recent")
    async def recent_files(limit: int = 30) -> list[dict[str, Any]]:
        return [_dump(f) for f in await app.memory.recent_files(_limit(limit, 200))]

    @api.get("/api/files/list")
    async def list_files(path: str | None = None) -> dict[str, Any]:
        try:
            return await app.list_folder(path)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    @api.post("/api/files/open")
    async def open_file(body: OpenFileBody) -> dict[str, Any]:
        try:
            shown = await app.open_path(body.path, reveal=body.reveal)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"ok": True, "revealed": shown == "revealed"}

    # ---------------------------------------------------------------------------- settings & keys
    @api.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        return app.settings.model_dump(mode="json")

    @api.patch("/api/settings")
    async def patch_settings(request: Request) -> dict[str, Any]:
        try:
            patch = await request.json()
        except ValueError as exc:
            raise HTTPException(400, "The body must be a JSON object.") from exc
        if not isinstance(patch, dict):
            raise HTTPException(400, "The body must be a JSON object.")
        try:
            await app.update_settings(patch)
        except (ConfigError, ValidationError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return app.settings.model_dump(mode="json")

    @api.get("/api/secrets")
    async def get_secrets() -> dict[str, Any]:
        return app.secrets.status()

    @api.put("/api/secrets/{name}")
    async def put_secret(name: str, body: SecretBody) -> dict[str, Any]:
        if name not in KNOWN_SECRETS:
            raise HTTPException(404, f"Unknown key: {name}")
        source = await app.set_secret(name, body.value)
        return {"configured": True, "source": source}

    @api.delete("/api/secrets/{name}")
    async def delete_secret(name: str) -> dict[str, Any]:
        if name not in KNOWN_SECRETS:
            raise HTTPException(404, f"Unknown key: {name}")
        await app.delete_secret(name)
        return {"configured": app.secrets.has(name)}

    # ---------------------------------------------------------------------------- voice & window
    @api.post("/api/voice/speak")
    async def speak(body: SpeakBody) -> dict[str, Any]:
        return {"ok": await app.speak(body.text)}

    @api.post("/api/voice/{action}")
    async def voice(action: Literal["start", "stop", "mute", "unmute", "ptt_start", "ptt_stop", "cancel"]
                    ) -> dict[str, Any]:
        return await app.voice_action(action)

    @api.get("/api/quick-tools")
    async def quick_tools(lang: str | None = None) -> list[dict[str, Any]]:
        return app.quick_tools(lang)

    @api.post("/api/window/{action}")
    async def window(action: Literal["show", "hide", "toggle", "mini", "full", "quit"]) -> dict[str, Any]:
        if not await app.window_action(action):
            raise HTTPException(409, "There is no JARVIS window in this run.")
        return {"ok": True}

    # ---------------------------------------------------------------------------- live events
    @api.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()

        async def on_message(msg: dict[str, Any], client: Client) -> None:
            kind = msg.get("type")
            if kind == "ping":
                await client.send_now("pong", {})
            elif kind == "command" and isinstance(msg.get("text"), str) and msg["text"].strip():
                app.spawn(app.core.handle_input(msg["text"][:8000], source="text"), "ws-command")
            elif kind == "confirm" and isinstance(msg.get("id"), str):
                app.broker.respond(msg["id"], bool(msg.get("approved")), by="ui", remember=bool(msg.get("remember")))

        await app.hub.serve(socket, snapshot=app.state_snapshot, version=__version__, on_message=on_message)

    # ---------------------------------------------------------------------------- the page
    @api.get("/", include_in_schema=False)
    async def index(request: Request, launch: str | None = None, view: str | None = None) -> Response:
        if launch is not None:
            if not auth.check_token(launch):
                return _gate_page(403)
            # Swap the launch link for a session cookie, and drop the token from the address bar.
            response = RedirectResponse("/?view=orb" if view == "orb" else "/", status_code=303)
            response.set_cookie(COOKIE, auth.new_session(), httponly=True, samesite="strict", path="/")
            return response
        if not auth.check_session(request.cookies.get(COOKIE)):
            return _gate_page(401)
        return _index_page(app)

    @api.get("/{path:path}", include_in_schema=False)
    async def static(request: Request, path: str) -> Response:
        target = (STATIC_DIR / path).resolve()
        if path and target.is_file() and target.is_relative_to(STATIC_DIR) and target.name != "index.html":
            return FileResponse(target, headers={"Cache-Control": "public, max-age=31536000, immutable"}
                                if path.startswith("assets/") else None)
        if path.startswith(("api/", "assets/")) or "." in Path(path).name:
            return _error(404, "not_found", f"/{path} not found")
        if not auth.check_session(request.cookies.get(COOKIE)):    # an app route ("/tasks"): the page itself
            return _gate_page(401)
        return _index_page(app)

    return api


def _index_page(app: JarvisApp) -> Response:
    index = STATIC_DIR / "index.html"
    if not index.is_file():
        return HTMLResponse(_simple_page("JARVIS is running, but its interface has not been built.",
                                         "Run `npm run build` in the ui folder, then reload."), status_code=503)
    ws_url = f"ws://127.0.0.1:{app.auth.port}/ws"
    # No credentials in the page: its requests are authorised by the HttpOnly session cookie.
    config = {"version": __version__, "wsUrl": ws_url, "apiBase": ""}
    nonce = secrets.token_urlsafe(16)
    script = f"<script nonce=\"{nonce}\">window.__JARVIS__ = {json.dumps(config)};</script>"
    page = index.read_text(encoding="utf-8").replace("<head>", "<head>\n    " + script, 1)
    # Only this page's own scripts run (model output shown in the chat can never execute).
    csp = (f"default-src 'self'; script-src 'self' 'nonce-{nonce}'; style-src 'self' 'unsafe-inline'; "
           f"img-src 'self' data: blob:; font-src 'self' data:; media-src 'self' blob: data:; "
           f"connect-src 'self' {ws_url}; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
           f"form-action 'none'")
    return HTMLResponse(page, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                       "Referrer-Policy": "no-referrer", "Content-Security-Policy": csp})


def _gate_page(status: int) -> HTMLResponse:
    return HTMLResponse(_simple_page("JARVIS פועל. כדי לפתוח את הממשק יש להשתמש בחלון של JARVIS או בפקודה jarvis open.",
                                     "JARVIS is running. Open it from the JARVIS window or with `jarvis open`."),
                        status_code=status, headers={"Cache-Control": "no-store"})


def _simple_page(he: str, en: str) -> str:
    return ("<!doctype html><html lang='he' dir='rtl'><meta charset='utf-8'><title>JARVIS</title>"
            "<body style='background:#03080f;color:#7fdcff;font-family:Segoe UI,Arial,sans-serif;"
            "display:grid;place-items:center;height:100vh;margin:0'><div style='text-align:center'>"
            f"<h1 style='letter-spacing:.3em'>JARVIS</h1><p>{html.escape(he)}</p>"
            f"<p dir='ltr' style='opacity:.7'>{html.escape(en)}</p></div></body></html>")


__all__ = ["STATIC_DIR", "create_api"]
