"""Routes of the "Code & development" screen.

A project is opened from a folder the user picked (`POST /api/dev/open`), which gives the Development agent
read access to that folder for the session. Everything after that is a job (`/api/jobs`, live as `job`
events): looking at the project, finding the files a request is about, planning a change with the AI the
user pressed, applying or undoing it, running the project's tests and asking for a fix. The tools behind
them go through the permission manager and the approval dialog like every other action.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from jarvis.app import JarvisApp


class OpenBody(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    lang: Literal["he", "en"] | None = None


class WriteBody(BaseModel):
    allow: bool
    lang: Literal["he", "en"] | None = None


class FindBody(BaseModel):
    request: str = Field(default="", max_length=4000)
    error_text: str = Field(default="", max_length=20_000)
    refresh: bool = False
    lang: Literal["he", "en"] | None = None


class ProposeBody(BaseModel):
    request: str = Field(min_length=2, max_length=4000)
    files: list[str] = Field(default_factory=list, max_length=24)
    mode: Literal["auto", "fast", "strong"] = "auto"
    error_text: str = Field(default="", max_length=20_000)
    parent_id: str = Field(default="", max_length=64)
    engine: str | None = Field(default=None, max_length=20)
    lang: Literal["he", "en"] | None = None


class TestsBody(BaseModel):
    command: str = Field(default="", max_length=500)
    lang: Literal["he", "en"] | None = None


class FixBody(BaseModel):
    tests_job_id: str = Field(default="", max_length=64)
    error_text: str = Field(default="", max_length=20_000)
    request: str = Field(default="", max_length=4000)
    mode: Literal["auto", "fast", "strong"] = "auto"
    engine: str | None = Field(default=None, max_length=20)
    lang: Literal["he", "en"] | None = None


class FileEditBody(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    content: str | None = Field(default=None, max_length=2_000_000)
    include: bool | None = None
    lang: Literal["he", "en"] | None = None


class LangBody(BaseModel):
    lang: Literal["he", "en"] | None = None


def dev_routes(app: JarvisApp) -> APIRouter:
    r = APIRouter()

    def lang(body: Any) -> str:
        return getattr(body, "lang", None) or app.lang

    def view(change_id: str) -> dict[str, Any]:
        found = app.dev.change_view(change_id)
        if found is None:
            raise HTTPException(404, f"No change {change_id}")
        return found

    @r.get("/api/dev/status")
    async def status() -> dict[str, Any]:
        return app.dev.status()

    # ---------------------------------------------------------------------------- the project
    @r.post("/api/dev/open")
    async def open_project(body: OpenBody) -> dict[str, Any]:
        session, job = await app.dev.open(body.path, lang=lang(body))
        return {"session": session.view(), "job": job.view()}

    @r.post("/api/dev/sessions/{session_id}/write")
    async def allow_changes(session_id: str, body: WriteBody) -> dict[str, Any]:
        return app.dev.set_writable(session_id, body.allow, lang=lang(body)).view()

    @r.delete("/api/dev/sessions/{session_id}")
    async def close_project(session_id: str) -> dict[str, Any]:
        return {"closed": app.dev.close(session_id)}

    @r.post("/api/dev/sessions/{session_id}/inspect")
    async def inspect(session_id: str, body: FindBody) -> dict[str, Any]:
        job = await app.dev.inspect(session_id, request=body.request, error_text=body.error_text,
                                    refresh=body.refresh, lang=lang(body))
        return job.view()

    # ---------------------------------------------------------------------------- changes
    @r.post("/api/dev/sessions/{session_id}/propose")
    async def propose(session_id: str, body: ProposeBody) -> dict[str, Any]:
        job = await app.dev.propose(session_id, body.request, files=body.files, mode=body.mode,
                                    error_text=body.error_text, parent_id=body.parent_id, engine=body.engine,
                                    lang=lang(body))
        return job.view()

    @r.get("/api/dev/sessions/{session_id}/changes")
    async def changes(session_id: str) -> list[dict[str, Any]]:
        return app.dev.changes(session_id)

    @r.get("/api/dev/changes/{change_id}")
    async def change(change_id: str) -> dict[str, Any]:
        return view(change_id)

    @r.put("/api/dev/changes/{change_id}/files")
    async def edit_file(change_id: str, body: FileEditBody) -> dict[str, Any]:
        app.dev.edit_file(change_id, body.path, content=body.content, include=body.include, lang=lang(body))
        return view(change_id)

    @r.post("/api/dev/changes/{change_id}/apply")
    async def apply(change_id: str, body: LangBody) -> dict[str, Any]:
        return (await app.dev.apply(change_id, lang=lang(body))).view()

    @r.post("/api/dev/changes/{change_id}/undo")
    async def undo(change_id: str, body: LangBody) -> dict[str, Any]:
        return (await app.dev.apply(change_id, lang=lang(body), undo=True)).view()

    @r.post("/api/dev/changes/{change_id}/discard")
    async def discard(change_id: str, body: LangBody) -> dict[str, Any]:
        app.dev.discard(change_id, lang=lang(body))
        return view(change_id)

    # ---------------------------------------------------------------------------- tests
    @r.post("/api/dev/sessions/{session_id}/tests")
    async def tests(session_id: str, body: TestsBody) -> dict[str, Any]:
        return (await app.dev.run_tests(session_id, command=body.command, lang=lang(body))).view()

    @r.post("/api/dev/sessions/{session_id}/fix")
    async def fix(session_id: str, body: FixBody) -> dict[str, Any]:
        job = await app.dev.fix(session_id, tests_job_id=body.tests_job_id, error_text=body.error_text,
                                request=body.request, mode=body.mode, engine=body.engine, lang=lang(body))
        return job.view()

    return r


__all__ = ["dev_routes"]
