"""The work screens' shared routes over the real local server: jobs (live events, stop, retry, history),
the native file dialog (and its typed-path fallback) and the speed measurements."""

from __future__ import annotations

import asyncio
from typing import Any

from integration.test_server import Live, connect, live  # noqa: F401  (the running-app fixture)
from jarvis.jobs import JobContext


async def _slow(ctx: JobContext) -> dict[str, Any]:
    async with ctx.step("wait", ctx.t("ממתין", "Waiting")):
        await ctx.guard(asyncio.sleep(30))
    return {"never": True}


class TestJobs:
    async def test_a_job_is_live_on_the_socket_stoppable_and_retryable(self, live: Live) -> None:  # noqa: F811
        events = await connect(live)
        await events.wait("hello")
        job = await live.app.jobs.start(kind="test.slow", area="code", title="בדיקה", run=_slow,
                                        steps=[("wait", "ממתין")], sends=["שום דבר"])
        running = await events.wait("job", lambda d: d["id"] == job.id and d["steps"][0]["status"] == "running")
        assert running["area"] == "code" and running["real"] is True and running["sends"] == ["שום דבר"]
        listed = await live.get("/api/jobs", params={"area": "code"})
        assert [j["id"] for j in listed] == [job.id]
        assert (await live.get(f"/api/jobs/{job.id}"))["status"] == "running"
        assert (await live.get("/api/state"))["jobs"][0]["id"] == job.id     # the snapshot carries them

        await live.post(f"/api/jobs/{job.id}/cancel")
        stopped = await events.wait("job", lambda d: d["id"] == job.id and d["status"] == "cancelled")
        assert stopped["can_retry"] is True
        again = await live.post(f"/api/jobs/{job.id}/retry")
        assert again["retry_of"] == job.id and again["status"] == "running"
        await live.post(f"/api/jobs/{again['id']}/cancel")
        await events.wait("job", lambda d: d["id"] == again["id"] and d["status"] == "cancelled")

        r = await live.http.post(f"/api/jobs/{again['id']}/cancel")
        assert r.status_code == 200                         # stopping a stopped job is harmless
        assert (await live.http.get("/api/jobs/job_nope")).status_code == 404
        cleared = (await live.http.delete("/api/jobs")).json()
        assert cleared["cleared"] == 2 and await live.get("/api/jobs") == []
        await events.close()

    async def test_a_running_job_cannot_be_retried(self, live: Live) -> None:  # noqa: F811
        job = await live.app.jobs.start(kind="test.slow", area="video", title="x", run=_slow)
        r = await live.http.post(f"/api/jobs/{job.id}/retry")
        assert r.status_code == 409 and r.json()["error"]["message"]
        live.app.jobs.cancel(job.id)
        await live.app.jobs.wait(job.id, 5)


class TestDialog:
    async def test_without_a_window_the_screen_is_told_to_take_a_typed_path(self, live: Live) -> None:  # noqa: F811
        r = await live.http.post("/api/dialog", json={"kind": "open"})
        assert r.status_code == 409 and r.json()["error"]["message"]

    async def test_the_window_dialog_returns_what_the_user_picked(self, live: Live) -> None:  # noqa: F811
        asked: list[dict[str, Any]] = []

        def dialog(request: dict[str, Any]) -> list[str] | None:
            asked.append(request)
            return None if request["kind"] == "save" else ["C:\\Users\\me\\Pictures\\ref.png"]

        live.app.dialog_handler = dialog
        picked = await live.post("/api/dialog", {"kind": "open", "file_types": ["Images (*.png;*.jpg)"],
                                                 "multiple": True})
        assert picked == {"paths": ["C:\\Users\\me\\Pictures\\ref.png"]}
        assert asked[0]["file_types"] == ["Images (*.png;*.jpg)"] and asked[0]["multiple"] is True
        assert await live.post("/api/dialog", {"kind": "save", "save_name": "a.png"}) == {"paths": None}
        assert asked[1]["multiple"] is False                # only Open picks several files
        r = await live.http.post("/api/dialog", json={"kind": "delete"})
        assert r.status_code == 422


class TestMetrics:
    async def test_measurements_snapshots_and_clearing(self, live: Live) -> None:  # noqa: F811
        live.app.metrics.record("command.reply", 420.0)
        live.app.metrics.record("command.reply", 380.0)
        view = await live.get("/api/metrics")
        assert view["summary"]["command.reply"]["count"] == 2
        snap = await live.post("/api/metrics/snapshot", {"label": "before"})
        assert snap["summary"]["command.reply"]["median_ms"] in (380, 420)
        only = await live.get("/api/metrics", params={"prefix": "image."})
        assert only["summary"] == {} and "before" in only["snapshots"]
        assert (await live.http.delete("/api/metrics")).json() == {"ok": True}
        assert await live.get("/api/metrics") == {"summary": {}, "snapshots": {}}
