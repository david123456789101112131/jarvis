"""Routes of the "Apps & tools" screen.

`status` says which ways of working with apps exist here (and why not), how to stop, and the limits;
`list` returns the installed apps (`?q=` searches, `?refresh=1` builds the list again) and the open windows.
Every action starts a job and returns it at once, live afterwards as `job` events: `open` (an installed app),
`look` (one look at the screen), `suggest` (the one next step toward a task; it does nothing) and `step` (do
a suggested step). `correct` keeps what JARVIS got wrong for the task's next look and suggestion, and
deleting a task ends it, together with any approval remembered for it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from jarvis.common.i18n import t
from jarvis.integrations.apps import AppsRequestError

if TYPE_CHECKING:
    from jarvis.app import JarvisApp

Lang = Literal["he", "en"] | None


class OpenBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    lang: Lang = None


class LookBody(BaseModel):
    task_id: str | None = Field(default=None, max_length=80)
    window: str = Field(default="", max_length=200)
    lang: Lang = None


class CorrectBody(BaseModel):
    text: str = Field(min_length=1, max_length=300)
    task_id: str | None = Field(default=None, max_length=80)
    lang: Lang = None


class SuggestBody(BaseModel):
    goal: str = Field(min_length=1, max_length=1000)
    task_id: str | None = Field(default=None, max_length=80)
    window: str = Field(default="", max_length=200)
    lang: Lang = None


class StepBody(BaseModel):
    suggestion: str = Field(min_length=1, max_length=80, description="The id of the suggestion's job.")
    lang: Lang = None


def apps_routes(app: JarvisApp) -> APIRouter:
    r = APIRouter()

    def ready(lang: str | None) -> None:
        if app.kill_switch.is_engaged("system"):
            raise HTTPException(409, t(lang or app.lang, "מתג החירום פעיל; צריך לשחרר אותו קודם",
                                       "The kill switch is engaged; release it first"))

    @r.get("/api/apps/status")
    async def status(lang: Lang = None) -> dict[str, Any]:
        return app.apps.status(lang)

    @r.get("/api/apps/list")
    async def listing(q: str = "", refresh: bool = False, lang: Lang = None) -> dict[str, Any]:
        return await app.apps.listing(q[:100], refresh=refresh, lang=lang)

    @r.post("/api/apps/open")
    async def open_app(body: OpenBody) -> dict[str, Any]:
        ready(body.lang)
        try:
            return (await app.apps.open_app(body.name, lang=body.lang)).view()
        except AppsRequestError as exc:
            raise HTTPException(exc.status, exc.message) from exc

    @r.post("/api/apps/look")
    async def look(body: LookBody) -> dict[str, Any]:
        ready(body.lang)
        return (await app.apps.look(body.task_id, body.window, lang=body.lang)).view()

    @r.post("/api/apps/correct")
    async def correct(body: CorrectBody) -> dict[str, Any]:
        try:
            return app.apps.correct(body.text, body.task_id, lang=body.lang)
        except AppsRequestError as exc:
            raise HTTPException(exc.status, exc.message) from exc

    @r.post("/api/apps/suggest")
    async def suggest(body: SuggestBody) -> dict[str, Any]:
        ready(body.lang)
        try:
            return (await app.apps.suggest(body.goal, body.task_id, body.window, lang=body.lang)).view()
        except AppsRequestError as exc:
            raise HTTPException(exc.status, exc.message) from exc

    @r.post("/api/apps/step")
    async def step(body: StepBody) -> dict[str, Any]:
        ready(body.lang)
        try:
            return (await app.apps.step(body.suggestion, lang=body.lang)).view()
        except AppsRequestError as exc:
            raise HTTPException(exc.status, exc.message) from exc

    @r.delete("/api/apps/task/{task_id}")
    async def end_task(task_id: str) -> dict[str, Any]:
        return {"ended": app.apps.end_task(task_id)}

    return r


__all__ = ["apps_routes"]
