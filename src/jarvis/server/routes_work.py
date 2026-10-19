"""Routes shared by the work screens (code, images, video, apps): their jobs, file dialogs and speed numbers.

`/api/jobs` lists, stops and retries the jobs those screens start (`jarvis.jobs`); every change also arrives
live as a `job` event on the WebSocket. `/api/dialog` opens the native Open / Save / Folder dialog of the
JARVIS window, so a file reaches JARVIS only when the user picks it there (the screens fall back to a typed
path when JARVIS runs without its window). `/api/metrics` returns the measured durations behind the speed
report, and can keep a named snapshot of them ("before" / "after") or clear them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from jarvis.common.i18n import t

if TYPE_CHECKING:
    from jarvis.app import JarvisApp


class DialogBody(BaseModel):
    kind: Literal["open", "folder", "save"]
    title: str = Field(default="", max_length=200)
    directory: str = Field(default="", max_length=1000)
    save_name: str = Field(default="", max_length=260)
    file_types: list[str] = Field(default_factory=list, max_length=10)
    multiple: bool = False


class SnapshotBody(BaseModel):
    label: str = Field(min_length=1, max_length=40)


def work_routes(app: JarvisApp) -> APIRouter:
    r = APIRouter()

    # ---------------------------------------------------------------------------- jobs
    @r.get("/api/jobs")
    async def jobs(area: str | None = None, kind: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return [j.view() for j in app.jobs.recent(area=area, kind=kind, limit=max(1, min(int(limit), 200)))]

    @r.get("/api/jobs/{job_id}")
    async def job(job_id: str) -> dict[str, Any]:
        found = app.jobs.get(job_id)
        if found is None:
            raise HTTPException(404, f"No job {job_id}")
        return found.view()

    @r.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str) -> dict[str, Any]:
        found = app.jobs.cancel(job_id, "stopped by the user")
        if found is None:
            raise HTTPException(404, f"No job {job_id}")
        return found.view()

    @r.post("/api/jobs/{job_id}/retry")
    async def retry_job(job_id: str) -> dict[str, Any]:
        if app.jobs.get(job_id) is None:
            raise HTTPException(404, f"No job {job_id}")
        again = await app.jobs.retry(job_id)
        if again is None:
            raise HTTPException(409, t(app.lang, "אי אפשר לנסות שוב את הפעולה הזו (היא עדיין רצה, או ש-JARVIS "
                                                 "הופעל מחדש מאז). אפשר להתחיל אותה מחדש מהמסך שלה.",
                                       "This action cannot be retried (it is still running, or JARVIS restarted "
                                       "since). Start it again from its screen."))
        return again.view()

    @r.delete("/api/jobs")
    async def clear_jobs() -> dict[str, Any]:
        return {"cleared": app.jobs.clear_history()}

    # ---------------------------------------------------------------------------- the native file dialog
    @r.post("/api/dialog")
    async def dialog(body: DialogBody) -> dict[str, Any]:
        if app.dialog_handler is None:
            raise HTTPException(409, t(app.lang, "אין כאן חלון של JARVIS לפתוח בו חלון בחירה; אפשר להקליד את הנתיב.",
                                       "There is no JARVIS window here to open a file dialog in; type the path."))
        try:
            paths = await app.file_dialog(body.kind, title=body.title, directory=body.directory,
                                          save_name=body.save_name, file_types=body.file_types,
                                          multiple=body.multiple)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"paths": paths}

    # ---------------------------------------------------------------------------- speed measurements
    @r.get("/api/metrics")
    async def metrics(prefix: str | None = None) -> dict[str, Any]:
        if prefix:
            return {"summary": app.metrics.summary(prefix), "snapshots": app.metrics.snapshots()}
        return app.metrics.view()

    @r.post("/api/metrics/snapshot")
    async def snapshot(body: SnapshotBody) -> dict[str, Any]:
        return app.metrics.snapshot(body.label)

    @r.delete("/api/metrics")
    async def clear_metrics() -> dict[str, Any]:
        app.metrics.clear()
        return {"ok": True}

    return r


__all__ = ["work_routes"]
