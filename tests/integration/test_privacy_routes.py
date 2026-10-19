"""The "Privacy & data" routes over the real local server: what JARVIS keeps (counts, sizes and places, never the
data), deleting it for good, and work in progress that stays."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from integration.test_server import Live, connect, live  # noqa: F401  (the running-app fixture)
from jarvis.jobs import JobContext
from jarvis.logs.audit import AuditEntry
from jarvis.media.store import MediaItem
from jarvis.memory import ChatRecord, ResultRecord, TaskRecord
from jarvis.privacy import CLEARABLE

SECRET_WORDS = "zebra-private"            # in every piece of content: it must never come back from the routes


async def _slow(ctx: JobContext) -> dict[str, Any]:
    await ctx.guard(asyncio.sleep(30))
    return {}


async def _quick(ctx: JobContext) -> dict[str, Any]:
    return {"summary": f"done {SECRET_WORDS}"}


async def _fill(live: Live) -> dict[str, Any]:  # noqa: F811
    app, paths = live.app, live.app.paths
    await app.memory.add_chat(ChatRecord(role="user", text=f"שיחה {SECRET_WORDS}"))
    await app.memory.add_command(f"פקודה {SECRET_WORDS}")
    await app.memory.remember(f"עובדה {SECRET_WORDS}")
    await app.memory.upsert_project(f"פרויקט {SECRET_WORDS}", path=str(paths.projects / "p"))
    await app.memory.record_file(paths.workspace / f"{SECRET_WORDS}.txt")
    await app.memory.save_task(TaskRecord(id="task_done", title=SECRET_WORDS, status="completed"))
    await app.memory.add_result(ResultRecord(task_id="task_done", summary=SECRET_WORDS))
    await app.memory.save_task(TaskRecord(id="task_paused", title="paused", status="interrupted"))
    (paths.data / "organize-logs").mkdir(exist_ok=True)
    (paths.data / "organize-logs" / "20261019-1.json").write_text(json.dumps({"root": SECRET_WORDS}), encoding="utf-8")
    finished = await app.jobs.start(kind="test.quick", area="apps", title=SECRET_WORDS, run=_quick)
    await app.jobs.wait(finished.id, 5)
    running = await app.jobs.start(kind="test.slow", area="apps", title="still running", run=_slow)
    app.dev.dir.mkdir(parents=True, exist_ok=True)
    (app.dev.dir / "chg_1.json").write_text(json.dumps({"diff": SECRET_WORDS}), encoding="utf-8")
    preview = app.media.store.folder / "job_1" / "a.png"
    preview.parent.mkdir(parents=True, exist_ok=True)
    preview.write_bytes(b"\x89PNG" + SECRET_WORDS.encode())
    app.media.store.add([MediaItem(kind="image", job_id="job_1", path=str(preview), format="PNG", mime="image/png",
                                   bytes=preview.stat().st_size, description=SECRET_WORDS)])
    app.metrics.record("command.instant", 3.0)
    await app.audit.record(AuditEntry(tool="read_file", action=f"read {SECRET_WORDS}"))
    await app.activity.log(f"activity {SECRET_WORDS}")
    (paths.logs / "jarvis.log").write_text(f"log {SECRET_WORDS}\n", encoding="utf-8")
    (paths.logs / "jarvis.log.1").write_text(f"old {SECRET_WORDS}\n", encoding="utf-8")
    paths.tts_cache.mkdir(parents=True, exist_ok=True)
    (paths.tts_cache / "abc.wav").write_bytes(b"RIFF....")
    (paths.cache / "app_index_linux.json").write_text("{}", encoding="utf-8")
    grant = app.permissions.grant("system", "fs.write", roots=[str(paths.workspace)], source="user", reason="test")
    return {"running": running, "grant": grant, "preview": preview}


def _counts(inventory: dict[str, Any]) -> dict[str, int]:
    return {item["key"]: item["count"] for item in inventory["items"]}


class TestPrivacyRoutes:
    async def test_what_is_kept_is_counted_without_its_content(self, live: Live) -> None:  # noqa: F811
        await _fill(live)
        inventory = await live.get("/api/privacy")
        assert [i["key"] for i in inventory["items"]] == list(CLEARABLE)
        counts = _counts(inventory)
        assert counts["chat"] >= 1 and counts["commands"] == 1 and counts["memory"] == 1 and counts["projects"] == 1
        assert counts["tasks"] == 1 and counts["files"] == 2 and counts["jobs"] == 1 and counts["code"] == 1
        assert counts["media"] == 1 and counts["speed"] >= 1 and counts["temp"] == 2 and counts["approvals"] == 1
        assert counts["logs"] >= 3                                   # activity, audit, jarvis.log and its old copy
        tasks = next(i for i in inventory["items"] if i["key"] == "tasks")
        assert tasks["kept"] == 1                                     # the interrupted one stays
        jobs = next(i for i in inventory["items"] if i["key"] == "jobs")
        assert jobs["running"] == 1
        assert SECRET_WORDS not in json.dumps(inventory, ensure_ascii=False)
        kept = {k["key"]: k for k in inventory["kept"]}
        assert set(kept) == {"keys", "settings", "work", "models"}
        assert kept["keys"]["count"] == len(kept["keys"]["names"])   # the keys' names only, never their values
        senses = inventory["senses"]
        assert senses["microphone"]["enabled"] is False and senses["screen"]["model"] == "local"
        assert senses["cloud"]["only_when_told"] is True
        assert [(a["name"], a["configured"]) for a in senses["cloud"]["ai"]] == [
            ("Claude (Anthropic)", False), ("Grok (xAI)", False), ("OpenAI", False)]

    async def test_deleting_everything_keeps_work_in_progress(self, live: Live) -> None:  # noqa: F811
        filled = await _fill(live)
        app, paths = live.app, live.app.paths
        policy = [g.id for g in app.permissions.grants() if g.source == "policy"]
        events = await connect(live)
        await events.wait("hello")
        out = await live.post("/api/privacy/clear", {"items": list(CLEARABLE)})
        assert all(r["ok"] for r in out["results"].values()), out["results"]
        assert out["results"]["files"]["count"] == 2 and out["results"]["approvals"]["count"] == 1
        counts = _counts(out["inventory"])
        assert all(counts[k] == 0 for k in CLEARABLE if k != "logs"), counts
        assert counts["logs"] == 1                                    # only the activity line saying what went
        assert (paths.logs / "activity.log").read_text(encoding="utf-8").count("\n") == 1
        assert not (paths.logs / "jarvis.log.1").exists() and (paths.logs / "jarvis.log").read_text() == ""
        assert (paths.logs / "audit.jsonl").read_text(encoding="utf-8") == "" and app.audit.recent() == []
        # still there: the running job, the interrupted task, the default permissions; the user's folders
        assert [j.id for j in app.jobs.recent()] == [filled["running"].id]
        assert [t.id for t in await app.memory.tasks()] == ["task_paused"]
        assert sorted(g.id for g in app.permissions.grants() if g.source == "policy") == sorted(policy)
        assert app.permissions.grants(include_expired=True) and filled["grant"].id not in {
            g.id for g in app.permissions.grants(include_expired=True)}
        assert not filled["preview"].exists() and paths.workspace.is_dir()
        # every open window is refreshed with the emptied conversation and lists
        state = await events.wait("state", lambda d: d["chat"] == [])
        assert len(events.of("state")) == 2                          # the one on connecting, and the new one
        assert [j["id"] for j in state["jobs"]] == [filled["running"].id]
        await events.close()
        app.jobs.cancel(filled["running"].id)
        await app.jobs.wait(filled["running"].id, 5)

    async def test_busy_work_is_left_alone_and_unknown_kinds_are_refused(self, live: Live) -> None:  # noqa: F811
        app = live.app
        app.dev.dir.mkdir(parents=True, exist_ok=True)
        (app.dev.dir / "chg_1.json").write_text("{}", encoding="utf-8")
        coding = await app.jobs.start(kind="test.slow", area="code", title="applying a change", run=_slow)
        inventory = await live.get("/api/privacy")
        assert next(i for i in inventory["items"] if i["key"] == "code")["busy"] is True
        out = await live.post("/api/privacy/clear", {"items": ["code", "speed"]})
        assert out["results"]["code"] == {"ok": False, "reason": "busy"} and out["results"]["speed"]["ok"]
        assert (app.dev.dir / "chg_1.json").exists()
        r = await live.http.post("/api/privacy/clear", json={"items": ["chat", "passwords"]})
        assert r.status_code == 422 and "passwords" in r.json()["error"]["message"]
        assert (await live.http.post("/api/privacy/clear", json={"items": []})).status_code == 422
        app.jobs.cancel(coding.id)
        await app.jobs.wait(coding.id, 5)


class TestLogFiles:
    def test_the_open_jarvis_log_is_emptied_and_goes_on_working(self, tmp_path: Path) -> None:
        import logging
        import logging.handlers

        from jarvis.privacy import _empty_log

        path = tmp_path / "jarvis.log"
        handler = logging.handlers.RotatingFileHandler(path, maxBytes=10_000, backupCount=2, encoding="utf-8")
        root = logging.getLogger()
        root.addHandler(handler)
        logger = logging.getLogger("jarvis.test.privacy")
        logger.setLevel(logging.INFO)
        try:
            logger.info("before %s", SECRET_WORDS)
            handler.flush()
            assert SECRET_WORDS in path.read_text(encoding="utf-8")
            assert _empty_log(path) is True and path.read_text(encoding="utf-8") == ""
            logger.info("after")
            handler.flush()
            assert path.read_text(encoding="utf-8").strip() == "after"
            assert not (tmp_path / "jarvis.log.1").exists()            # no rotation from a stale position
        finally:
            root.removeHandler(handler)
            handler.close()
