"""The image and video screens' routes over the real local server: status, a demonstration end to end (job
events, the preview file, delete), a real creation against a mocked xAI with the save flow and its approval, and
what the routes refuse (creating without xAI, saving a demonstration or without a path, any file that is not a
preview).

No request reaches xAI: the app's web client is an `httpx.MockTransport`, and the key is a made-up one.
"""

from __future__ import annotations

import asyncio
import base64
import struct
import zlib
from pathlib import Path
from typing import Any

import httpx
import pytest

from integration.test_server import Live, connect, live  # noqa: F401  (the running-app fixture)
from jarvis.media import demo

FAKE_KEY = "xai-" + "R0ute" * 10       # made up: no real key is ever used here


@pytest.fixture(autouse=True)
def _no_real_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XAI_API_KEY", raising=False)


def png(width: int = 3, height: int = 2) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\x40" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


async def until(check: Any, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.02)


class TestLocalByDefault:
    async def test_nothing_paid_is_offered_and_what_to_install_is_said(self, live: Live) -> None:  # noqa: F811
        status = await live.get("/api/media/status", params={"lang": "he"})
        assert status["engine"] == "local" and status["available"] is False and status["demo"] is True
        assert status["host"] == "" and status["network"] is False
        assert all("jarvis media install" in line for line in status["missing"]) and status["missing"]
        offer = status["offer"]
        assert [m["local"] for m in offer["image"]["models"] + offer["video"]["models"]] == [True, True]
        assert offer["local_times"]["image_s"] > 0
        refused = await live.http.post("/api/media/images", json={"description": "שועל", "lang": "he"})
        error = refused.json()["error"]
        assert refused.status_code == 409 and error["code"] == "unavailable" and "jarvis media install" in error["message"]


class TestMediaRoutes:
    """Grok Imagine (paid), when the settings choose it."""

    @pytest.fixture(autouse=True)
    def _grok(self, live: Live) -> None:  # noqa: F811
        live.app.settings.media.engine = "xai"

    async def test_status_says_what_is_missing(self, live: Live) -> None:  # noqa: F811
        status = await live.get("/api/media/status", params={"lang": "he"})
        assert status["available"] is False and status["demo"] is True and status["xai"] is False
        assert any("XAI_API_KEY" in line for line in status["missing"])
        assert status["offer"]["image"]["models"][0]["label"] == "רגיל" and status["offer"]["prices_checked"]

    async def test_a_demonstration_end_to_end(self, live: Live, tmp_path: Path) -> None:  # noqa: F811
        events = await connect(live)
        await events.wait("hello")
        job = await live.post("/api/media/images", {"description": "שועל בשלג", "demo": True, "lang": "he"})
        assert job["real"] is False and job["area"] == "images" and job["sends"] == []
        done = await events.wait("job", lambda d: d["id"] == job["id"] and d["status"] == "done")
        (item,) = done["result"]["items"]
        assert item["real"] is False and [i["id"] for i in await live.get("/api/media/items?kind=image")] == [item["id"]]

        r = await live.http.get(item["url"])
        assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
        assert r.headers["x-content-type-options"] == "nosniff" and "sandbox" in r.headers["content-security-policy"]
        assert demo.DEMO_IMAGE_HE in r.text

        r = await live.http.post("/api/media/save", json={"item_id": item["id"], "target": str(tmp_path / "x.png")})
        assert r.status_code == 409 and r.json()["error"]["code"] == "demo"
        assert (await live.http.delete(f"/api/media/items/{item['id']}")).json() == {"deleted": True}
        assert (await live.http.get(item["url"])).status_code == 404
        assert (await live.http.delete(f"/api/media/items/{item['id']}")).status_code == 404
        await events.close()

    async def test_creating_for_real_without_xai_is_refused_not_faked(self, live: Live) -> None:  # noqa: F811
        for path, body in (("/api/media/images", {"description": "a fox"}), ("/api/media/videos", {"scene": "a beach"})):
            r = await live.http.post(path, json=body)
            assert r.status_code == 409 and r.json()["error"]["code"] == "unavailable"
            assert "XAI_API_KEY" in r.json()["error"]["message"]
        assert await live.get("/api/jobs?area=images") == [] and await live.get("/api/jobs?area=video") == []

    async def test_only_previews_are_ever_served(self, live: Live, tmp_path: Path) -> None:  # noqa: F811
        from jarvis.media.store import MediaItem

        secret = live.app.paths.data / "elsewhere.png"
        secret.write_bytes(png())
        stray = MediaItem(kind="image", job_id="x", path=str(secret), format="PNG", mime="image/png")
        live.app.media.store._items[stray.id] = stray                # an index changed by hand
        for path in (f"/api/media/file/{stray.id}", "/api/media/file/media_nope", "/api/media/file/..%2F..%2Fdata",
                     "/api/media/file/%2E%2E"):
            assert (await live.http.get(path)).status_code == 404, path
        async with httpx.AsyncClient(base_url=live.base) as anon:
            assert (await anon.get(f"/api/media/file/{stray.id}")).status_code == 403

    async def test_saving_needs_a_full_path(self, live: Live) -> None:  # noqa: F811
        done = await live.app.jobs.wait((await live.post("/api/media/images", {"description": "x", "demo": True}))["id"], 10)
        assert done is not None
        item_id = done.result["items"][0]["id"]  # type: ignore[index]
        assert (await live.http.post("/api/media/save", json={"item_id": item_id})).status_code == 422
        assert (await live.http.post("/api/media/save", json={"item_id": item_id, "target": ""})).status_code == 422
        r = await live.http.post("/api/media/images", json={"description": "x", "aspect_ratio": "7:3", "demo": True})
        assert r.status_code == 422

    async def test_a_real_image_is_created_shown_and_saved_after_approval(self, live: Live, tmp_path: Path,  # noqa: F811
                                                                          monkeypatch: pytest.MonkeyPatch) -> None:
        picture = png(5, 5)
        seen: list[httpx.Request] = []

        def answer(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(picture).decode()}],
                                             "usage": {"cost_in_usd_ticks": 200_000_000}})

        monkeypatch.setenv("XAI_API_KEY", FAKE_KEY)
        live.app.services._http = httpx.AsyncClient(transport=httpx.MockTransport(answer))
        assert (await live.get("/api/media/status"))["available"] is True
        job = await live.post("/api/media/images", {"description": "a fox", "lang": "en"})
        assert job["real"] is True and job["cost"] == "≈ $0.02" and any("api.x.ai" in s for s in job["sends"])
        done = await live.app.jobs.wait(job["id"], 15)
        assert done is not None and done.status == "done", done.error if done else None
        (item,) = done.result["items"]  # type: ignore[index]
        r = await live.http.get(item["url"])
        assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content == picture
        assert [str(s.url) for s in seen] == ["https://api.x.ai/v1/images/generations"]

        target = tmp_path / "out" / "fox.jpeg"                      # the extension follows the real format
        saving = await live.post("/api/media/save", {"item_id": item["id"], "target": str(target), "lang": "en"})
        assert saving["kind"] == "image.save" and saving["sends"] == []
        await until(lambda: bool(live.app.broker.pending()))
        (ask,) = await live.get("/api/confirmations")
        assert ask["tool"] == "copy_path" and ask["agent_id"] == "creative"
        await live.post(f"/api/confirmations/{ask['id']}", {"approved": True})
        saved = await live.app.jobs.wait(saving["id"], 15)
        assert saved is not None and saved.status == "done", saved.error if saved else None
        assert (tmp_path / "out" / "fox.png").read_bytes() == picture and not target.exists()
        r = await live.http.post("/api/media/save", json={"item_id": item["id"], "target": str(tmp_path / "out" / "fox.png")})
        assert r.status_code == 409 and r.json()["error"]["code"] == "exists"
        assert not [g for g in live.app.permissions.grants("creative", include_expired=True) if g.source == "user"]
