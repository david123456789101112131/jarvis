"""Image and video creation: the xAI client, the catalog, the tools behind the permission gate, the service's jobs
and the save flow.

No test talks to xAI or any other service: every request goes to an `httpx.MockTransport`, and the key is a
made-up one (`FAKE_KEY`). The real environment's XAI_API_KEY is removed before each test.
"""

from __future__ import annotations

import asyncio
import base64
import json
import struct
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from jarvis.media import catalog
from jarvis.media.client import MediaError, Picture, XaiMedia
from jarvis.media.files import probe, safe_name, with_format
from jarvis.security.redaction import Redactor

FAKE_KEY = "xai-" + "T3st" * 12           # made up: no real key is ever used here


@pytest.fixture(autouse=True)
def _no_real_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XAI_API_KEY", raising=False)


def png(width: int = 2, height: int = 1) -> bytes:
    """A real (tiny, grey) PNG."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\x80" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


Handler = Callable[[httpx.Request], httpx.Response]


def mock_http(handler: Handler) -> tuple[Callable[[], httpx.AsyncClient], list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    return (lambda: client), seen


def xai(handler: Handler, key: str | None = FAKE_KEY) -> tuple[XaiMedia, list[httpx.Request]]:
    http, seen = mock_http(handler)
    redactor = Redactor()
    if key:
        redactor.register("XAI_API_KEY", key)
    return XaiMedia(lambda: key, http, redact=redactor.redact_text), seen


def body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


# =========================================================================== the client
class TestClient:
    async def test_an_image_from_a_description(self) -> None:
        picture = png(4, 3)

        def answer(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [{"b64_json": b64(picture), "mime_type": "image/png"}] * 2,
                                             "model": "grok-imagine-image", "usage": {"cost_in_usd_ticks": 400_000_000}})

        client, seen = xai(answer)
        batch = await client.images(prompt="a red fox in the snow", model="grok-imagine-image", count=2,
                                    aspect_ratio="16:9", resolution="1k", lang="en")
        assert [p.data for p in batch.pictures] == [picture, picture]
        assert batch.usd == pytest.approx(0.04) and batch.model == "grok-imagine-image"
        (request,) = seen
        assert request.method == "POST" and str(request.url) == "https://api.x.ai/v1/images/generations"
        assert request.headers["Authorization"] == f"Bearer {FAKE_KEY}"
        assert body(request) == {"model": "grok-imagine-image", "prompt": "a red fox in the snow",
                                 "response_format": "b64_json", "n": 2, "aspect_ratio": "16:9", "resolution": "1k"}
        assert client.requests == 1

    async def test_an_edit_sends_the_image_itself_and_nothing_about_size(self) -> None:
        source = png(8, 8)
        client, seen = xai(lambda r: httpx.Response(200, json={"data": [{"b64_json": b64(png())}]}))
        await client.images(prompt="make it night", model="grok-imagine-image", count=1, aspect_ratio="1:1",
                            resolution="2k", source=Picture(source), lang="en")
        (request,) = seen
        assert request.url.path == "/v1/images/edits"
        sent = body(request)
        assert sent["image"] == {"url": "data:image/png;base64," + b64(source), "type": "image_url"}
        assert "n" not in sent and "aspect_ratio" not in sent and "resolution" not in sent

    async def test_an_image_given_as_a_link_is_fetched_without_the_key(self) -> None:
        picture = png()

        def answer(request: httpx.Request) -> httpx.Response:
            if request.url.host == "api.x.ai":
                return httpx.Response(200, json={"data": [{"url": "https://imgen.x.ai/out/1.png"}]})
            return httpx.Response(200, content=picture)

        client, seen = xai(answer)
        batch = await client.images(prompt="p", model="grok-imagine-image", count=1, aspect_ratio="1:1",
                                    resolution="1k", lang="en")
        assert batch.pictures[0].data == picture
        assert str(seen[1].url) == "https://imgen.x.ai/out/1.png" and "Authorization" not in seen[1].headers

    async def test_no_key_sends_nothing(self) -> None:
        client, seen = xai(lambda r: httpx.Response(200, json={}), key=None)
        with pytest.raises(MediaError) as err:
            await client.images(prompt="p", model="m", count=1, aspect_ratio="1:1", resolution="1k", lang="he")
        assert err.value.details["kind"] == "no_key" and "אין מפתח API של xAI" in err.value.message
        assert not seen and client.requests == 0

    async def test_a_key_typed_in_hebrew_is_refused_before_sending(self) -> None:
        client, seen = xai(lambda r: httpx.Response(200, json={}), key="xai-שלום")
        with pytest.raises(MediaError) as err:
            await client.start_video(prompt="p", model="m", seconds=5, aspect_ratio="16:9", resolution="480p", lang="en")
        assert err.value.details["kind"] == "bad_key" and not seen

    @pytest.mark.parametrize(("status", "error", "kind", "words"), [
        (401, "Incorrect API key provided", "auth", "refused the API key"),
        (403, "Your team doesn't have any credits yet", "credits", "console.x.ai"),
        (402, "Insufficient balance", "credits", "no credits left"),
        (400, "Generated image rejected by content moderation", "refused", "content policy"),
        (403, "Forbidden: this model is not enabled for your team", "forbidden", "403"),
        (429, "Rate limit reached", "rate_limit", "Try again in a minute"),
        (503, "Service unavailable", "unavailable", "not available right now"),
        (422, "aspect_ratio: unknown variant", "bad_request", "did not accept"),
    ])
    async def test_failures_in_words(self, status: int, error: str, kind: str, words: str) -> None:
        client, seen = xai(lambda r: httpx.Response(status, json={"error": error}))
        with pytest.raises(MediaError) as err:
            await client.images(prompt="p", model="m", count=1, aspect_ratio="1:1", resolution="1k", lang="en")
        assert err.value.details["kind"] == kind and words in err.value.message
        assert len(seen) == 1                       # nothing is retried on its own: every request may cost money

    async def test_hebrew_failures(self) -> None:
        client, _ = xai(lambda r: httpx.Response(429, json={"error": "slow down"}))
        with pytest.raises(MediaError) as err:
            await client.images(prompt="p", model="m", count=1, aspect_ratio="1:1", resolution="1k", lang="he")
        assert "יותר מדי בקשות" in err.value.message

    async def test_the_key_never_shows_in_an_error(self) -> None:
        client, _ = xai(lambda r: httpx.Response(401, json={"error": {"message": f"Incorrect API key {FAKE_KEY}"}}))
        with pytest.raises(MediaError) as err:
            await client.images(prompt="p", model="m", count=1, aspect_ratio="1:1", resolution="1k", lang="en")
        assert FAKE_KEY not in err.value.message and FAKE_KEY not in json.dumps(err.value.to_dict())

    async def test_timeouts_and_lost_connections(self) -> None:
        def slow(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        def offline(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route", request=request)

        for handler, kind in ((slow, "timeout"), (offline, "network")):
            client, _ = xai(handler)
            with pytest.raises(MediaError) as err:
                await client.images(prompt="p", model="m", count=1, aspect_ratio="1:1", resolution="1k", lang="en")
            assert err.value.details["kind"] == kind

    async def test_an_answer_without_images_is_an_error_not_an_empty_success(self) -> None:
        client, _ = xai(lambda r: httpx.Response(200, json={"data": []}))
        with pytest.raises(MediaError) as err:
            await client.images(prompt="p", model="m", count=1, aspect_ratio="1:1", resolution="1k", lang="en")
        assert err.value.details["kind"] == "bad_response"

    async def test_a_video_from_a_description(self) -> None:
        client, seen = xai(lambda r: httpx.Response(200, json={"request_id": "vid-123"}))
        rid = await client.start_video(prompt="Scene: a lighthouse", model="grok-imagine-video-1.5-lite", seconds=6,
                                       aspect_ratio="9:16", resolution="720p", lang="en")
        assert rid == "vid-123"
        assert str(seen[0].url) == "https://api.x.ai/v1/videos/generations"
        assert body(seen[0]) == {"model": "grok-imagine-video-1.5-lite", "prompt": "Scene: a lighthouse",
                                 "duration": 6, "resolution": "720p", "aspect_ratio": "9:16"}

    async def test_a_video_from_a_first_frame_and_references(self) -> None:
        frame, ref = png(4, 4), png(2, 2)
        client, seen = xai(lambda r: httpx.Response(200, json={"request_id": "vid-9"}))
        await client.start_video(prompt="p", model="grok-imagine-video-1.5", seconds=3, aspect_ratio="16:9",
                                 resolution="480p", image=Picture(frame), references=[Picture(ref)], lang="en")
        sent = body(seen[0])
        assert sent["image"] == {"url": "data:image/png;base64," + b64(frame)}
        assert "aspect_ratio" not in sent                    # a first frame sets the shape
        assert sent["reference_images"] == [{"url": "data:image/png;base64," + b64(ref)}]

    async def test_asking_about_a_video(self) -> None:
        answers = iter([
            httpx.Response(202, json={}),
            httpx.Response(200, json={"status": "pending", "progress": 40}),
            httpx.Response(200, json={"status": "done", "progress": 100, "model": "grok-imagine-video-1.5-lite",
                                      "video": {"url": "https://vidgen.x.ai/v/1.mp4", "duration": 6},
                                      "usage": {"cost_in_usd_ticks": 1_800_000_000}}),
            httpx.Response(200, json={"status": "failed", "error": {"message": "the scene was refused"}}),
            httpx.Response(200, json={"status": "expired"}),
        ])
        client, seen = xai(lambda r: next(answers))
        first = await client.video_state("vid-1", lang="en")
        assert first.status == "pending" and first.progress is None
        assert (await client.video_state("vid-1", lang="en")).progress == pytest.approx(0.4)
        done = await client.video_state("vid-1", lang="en")
        assert (done.status, done.url, done.seconds) == ("done", "https://vidgen.x.ai/v/1.mp4", 6.0)
        assert done.usd == pytest.approx(0.18)
        failed = await client.video_state("vid-1", lang="en")
        assert failed.status == "failed" and "the scene was refused" in failed.error
        assert "expired" in (await client.video_state("vid-1", lang="en")).error
        assert all(str(r.url) == "https://api.x.ai/v1/videos/vid-1" and r.method == "GET" for r in seen)

    async def test_downloading_the_finished_video(self, tmp_path: Path) -> None:
        def answer(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("missing.mp4"):
                return httpx.Response(404)
            return httpx.Response(200, content=MP4)

        client, seen = xai(answer)
        folder = tmp_path / "downloads"
        dest = folder / "clip.mp4"
        assert await client.download("https://vidgen.x.ai/v/1.mp4", dest, lang="en") == len(MP4)
        assert dest.read_bytes() == MP4 and "Authorization" not in seen[0].headers
        with pytest.raises(MediaError) as err:
            await client.download("https://vidgen.x.ai/v/missing.mp4", folder / "x.mp4", lang="en")
        assert err.value.details["kind"] == "unavailable"
        with pytest.raises(MediaError):
            await client.download("http://vidgen.x.ai/v/1.mp4", folder / "y.mp4", lang="en")
        with pytest.raises(MediaError) as err:
            await client.download("https://vidgen.x.ai/v/1.mp4", folder / "z.mp4", lang="en", max_bytes=10)
        assert sorted(p.name for p in folder.iterdir()) == ["clip.mp4"]      # no half-written files left


# =========================================================================== the catalog and the files
class TestCatalog:
    def test_prices(self) -> None:
        assert catalog.image_cost("grok-imagine-image", "1k", 2) == pytest.approx(0.04)
        assert catalog.image_cost("grok-imagine-image-quality", "2k", 1, inputs=1) == pytest.approx(0.08)
        assert catalog.video_cost("grok-imagine-video-1.5-lite", "480p", 6) == pytest.approx(0.12)
        assert catalog.video_cost("grok-imagine-video-1.5", "1080p", 10, inputs=2) == pytest.approx(2.52)
        assert catalog.usd(0.001) == "<$0.01" and catalog.usd(0.125) == "$0.12" and catalog.usd(0) == "$0.00"
        assert catalog.reported_usd({"cost_in_usd_ticks": 2_500_000_000}) == pytest.approx(0.25)
        assert catalog.reported_usd({"cost_in_usd_ticks": True}) is None and catalog.reported_usd(None) is None

    def test_the_text_that_is_sent(self) -> None:
        assert catalog.image_prompt("  a cat ", "none") == "a cat"
        assert catalog.image_prompt("a cat", "watercolor") == "a cat\n\nStyle: watercolor painting on textured paper"
        assert catalog.change_prompt("a cat", "make it blue").startswith("Change this image as asked")
        assert catalog.video_prompt(scene="a beach", motion=" slow pan ", lighting="") == "Scene: a beach\nMotion: slow pan"
        assert catalog.video_change_prompt("Scene: a beach", "at night") == "Scene: a beach\nChange: at night"

    def test_the_offer_the_screens_show(self) -> None:
        he = catalog.offer("he")
        assert he["image"]["models"][0]["label"] == "רגיל" and he["prices_checked"] == catalog.PRICES_CHECKED  # type: ignore[index]
        assert catalog.offer("en")["video"]["seconds"] == {"min": 1, "max": 15, "default": 6}  # type: ignore[index]
        assert all(m.id in {"grok-imagine-video-1.5-lite", "grok-imagine-video-1.5"} for m in catalog.VIDEO_MODELS)

    def test_file_formats_and_names(self, tmp_path: Path) -> None:
        found = probe(png(5, 7))
        assert found is not None and (found.format, found.width, found.height, found.ext) == ("PNG", 5, 7, ".png")
        assert probe(MP4) is not None and probe(MP4).format == "MP4"  # type: ignore[union-attr]
        assert probe(b"<svg") is None
        assert with_format(tmp_path / "a.jpg", "PNG") == (tmp_path / "a.png", True)
        assert with_format(tmp_path / "a.jpeg", "JPEG") == (tmp_path / "a.jpeg", False)
        assert with_format(tmp_path / "fox", "PNG") == (tmp_path / "fox.png", True)
        assert safe_name('a/b:c*?"<>|d. ') == "a b c d" and safe_name("...") == "jarvis"


# =========================================================================== the tools, behind the gate
async def until(check: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


async def asked(broker: Any) -> Any:
    """The approval the user is being asked for (waits for it)."""
    await until(lambda: bool(broker.pending()))
    return broker.pending()[0]


@pytest.fixture
def mkit(kit: Any) -> Any:
    """The tool layer with the media tools and a made-up xAI key; a test gives it its mock of xAI."""
    from jarvis.security.secrets import SecretStore
    from jarvis.tools.media_tools import CreateImageTool, CreateVideoTool

    kit.registry.register(CreateImageTool())
    kit.registry.register(CreateVideoTool())
    kit.services.secrets = SecretStore(None, redactor=kit.redactor, use_keyring=False, environ={"XAI_API_KEY": FAKE_KEY})
    kit.out = kit.paths.workspace / "media" / "job_test"
    return kit


def use_xai(target: Any, handler: Handler) -> list[httpx.Request]:
    http, seen = mock_http(handler)
    target._http = http()
    return seen


def images_answer(*pictures: bytes, ticks: int = 200_000_000) -> Handler:
    return lambda r: httpx.Response(200, json={"data": [{"b64_json": b64(p)} for p in pictures or (png(),)],
                                               "usage": {"cost_in_usd_ticks": ticks}})


class TestTools:
    async def test_a_description_alone_runs_without_asking_and_lands_in_the_media_folder(self, mkit: Any) -> None:
        seen = use_xai(mkit.services, images_answer(png(3, 2)))
        result = await mkit.executor.execute("create_image", {"prompt": "a fox", "out_dir": str(mkit.out), "name": "fox"},
                                             agent_id="creative", task_id="t1", lang="en")
        assert result.ok, result.error
        (made,) = result.data["files"]
        assert Path(made["path"]).parent == mkit.out and (made["format"], made["width"], made["height"]) == ("PNG", 3, 2)
        assert body(seen[0])["prompt"] == "a fox" and result.data["usd"] == pytest.approx(0.02)
        entry = mkit.audit.recent()[-1]
        assert (entry.tool, entry.agent_id, entry.decision, entry.outcome) == ("create_image", "creative", "allowed", "ok")

    async def test_the_plan_says_what_is_sent_where_and_for_how_much(self, mkit: Any) -> None:
        from jarvis.common.cancel import CancelToken
        from jarvis.common.enums import Mode
        from jarvis.tools.base import ToolContext
        from jarvis.tools.media_tools import CreateImageParams, CreateVideoParams

        ctx = ToolContext(agent_id="creative", task_id="t1", subtask_id=None, mode=Mode.NORMAL, cancel=CancelToken(),
                          services=mkit.services, lang="he")
        image = CreateImageParams(prompt="שועל", count=2, out_dir=str(mkit.out))
        plan = await mkit.registry.get("create_image").plan(image, ctx)
        assert plan.sends_data and plan.destination == "api.x.ai" and plan.outbound == "שועל"
        assert plan.details["to"] == "https://api.x.ai/v1/images/generations" and "$0.04" in plan.details["price"]
        assert plan.risk is None and not plan.always_confirm and "2 תמונות" in plan.summary
        video = CreateVideoParams(prompt="Scene: rain", seconds=10, resolution="720p", out_dir=str(mkit.out))
        plan = await mkit.registry.get("create_video").plan(video, ctx)
        assert "$0.30" in plan.summary and "xAI" in plan.details["if_stopped"]

    async def test_a_file_of_the_users_is_sent_only_after_an_approval_naming_it_and_xai(self, mkit: Any) -> None:
        photo = mkit.user_docs / "cat.png"
        photo.write_bytes(png(4, 4))
        seen = use_xai(mkit.services, images_answer())
        params = {"prompt": "make it night", "source": str(photo), "out_dir": str(mkit.out)}
        for approve in (False, True):
            call = asyncio.create_task(mkit.executor.execute("create_image", params, agent_id="creative", task_id="t2",
                                                             lang="en"))
            req = await asked(mkit.broker)
            assert "cat.png" in req.summary and req.risk == "high" and req.always_ask
            assert any("xAI" in r and "cat.png" in r for r in req.reasons)
            assert req.details["files"] == [str(photo)] and req.details["to"].endswith("/images/edits")
            assert not seen                                     # nothing has left before the answer
            mkit.broker.respond(req.id, approve)
            result = await call
            assert result.ok is approve and (result.denied == "confirmation") is not approve
        assert len(seen) == 1 and body(seen[0])["image"]["url"] == "data:image/png;base64," + b64(photo.read_bytes())

    async def test_a_key_in_the_text_is_never_sent(self, mkit: Any) -> None:
        seen = use_xai(mkit.services, images_answer())
        result = await mkit.executor.execute("create_image", {"prompt": f"my key is {FAKE_KEY}", "out_dir": str(mkit.out)},
                                             agent_id="creative", lang="en")
        assert not result.ok and result.denied == "blocked" and not seen

    async def test_it_writes_only_into_the_media_folder_and_honours_the_network_switch(self, mkit: Any) -> None:
        seen = use_xai(mkit.services, images_answer())
        result = await mkit.executor.execute("create_image", {"prompt": "x", "out_dir": str(mkit.user_docs)},
                                             agent_id="creative", lang="en")
        assert not result.ok and result.denied == "permission"
        mkit.settings.security.network_enabled = False
        result = await mkit.executor.execute("create_image", {"prompt": "x", "out_dir": str(mkit.out)},
                                             agent_id="creative", lang="en")
        assert not result.ok and "Network access is turned off" in (result.error or "") and not seen

    async def test_a_video_is_started_waited_for_and_downloaded(self, mkit: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(catalog, "VIDEO_POLL_S", 0.01)
        polls: list[int] = []

        def answer(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(200, json={"request_id": "vid-7"})
            if request.url.host == "vidgen.x.ai":
                return httpx.Response(200, content=MP4)
            polls.append(1)
            if len(polls) < 3:
                return httpx.Response(200, json={"status": "pending", "progress": 30 * len(polls)})
            return httpx.Response(200, json={"status": "done", "video": {"url": "https://vidgen.x.ai/v/7.mp4", "duration": 4},
                                             "usage": {"cost_in_usd_ticks": 800_000_000}})

        use_xai(mkit.services, answer)
        steps: list[tuple[float, str]] = []

        async def progress(fraction: float, message: str) -> None:
            steps.append((fraction, message))

        result = await mkit.executor.execute("create_video", {"prompt": "Scene: rain", "seconds": 4, "out_dir": str(mkit.out)},
                                             agent_id="creative", lang="en", progress=progress)
        assert result.ok, result.error
        (made,) = result.data["files"]
        assert Path(made["path"]).read_bytes() == MP4 and made["seconds"] == 4.0
        assert result.data["usd"] == pytest.approx(0.08) and result.data["request_id"] == "vid-7"
        assert any("(30%)" in m for _, m in steps) and [f for f, _ in steps] == sorted(f for f, _ in steps)

    async def test_a_video_xai_could_not_make_is_reported_and_nothing_is_kept(self, mkit: Any,
                                                                            monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(catalog, "VIDEO_POLL_S", 0.01)

        def answer(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(200, json={"request_id": "v"})
            return httpx.Response(200, json={"status": "failed", "error": {"message": "the scene was refused"}})

        use_xai(mkit.services, answer)
        result = await mkit.executor.execute("create_video", {"prompt": "Scene: x", "out_dir": str(mkit.out)},
                                             agent_id="creative", lang="en")
        assert not result.ok and "the scene was refused" in (result.error or "") and result.data["kind"] == "failed"
        assert not list(mkit.out.glob("*.mp4"))


# =========================================================================== the service: jobs, demo, save
@pytest.fixture
async def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The whole app (no server, no voice) with a home folder of its own: Documents and Pictures under tmp."""
    from fakes import FakeHost
    from jarvis.ai.router import ProviderRouter
    from jarvis.app import JarvisApp
    from jarvis.config.settings import Settings

    home = tmp_path / "home"
    for name in ("Documents", "Pictures"):
        (home / name).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(catalog, "VIDEO_POLL_S", 0.01)
    settings = Settings()
    jarvis = JarvisApp(settings, host=FakeHost(), router=ProviderRouter(settings.ai, []), voice=False, use_keyring=False,
                       serve=False)
    await jarvis.start()
    try:
        yield jarvis
    finally:
        await jarvis.shutdown()


def connect_xai(app: Any, monkeypatch: pytest.MonkeyPatch, handler: Handler) -> list[httpx.Request]:
    monkeypatch.setenv("XAI_API_KEY", FAKE_KEY)
    return use_xai(app.services, handler)


async def finished(app: Any, job: Any, timeout: float = 15.0) -> Any:
    done = await app.jobs.wait(job.id, timeout)
    assert done is not None and done.status != "running"
    return done


async def real_image(app: Any, monkeypatch: pytest.MonkeyPatch, **kw: Any) -> tuple[dict[str, Any], list[httpx.Request]]:
    from jarvis.media.service import ImageRequest

    seen = connect_xai(app, monkeypatch, images_answer(png(6, 4)))
    done = await finished(app, await app.media.create_image(ImageRequest(description="a fox", lang="en", **kw)))
    assert done.status == "done", done.error
    return done.result["items"][0], seen


class TestService:
    async def test_without_xai_it_says_what_is_missing_and_creates_nothing_real(self, app: Any) -> None:
        from jarvis.media.service import ImageRequest, MediaRequestError, VideoRequest

        status = app.media.status("he")
        assert status["available"] is False and status["demo"] is True
        assert any("XAI_API_KEY" in line for line in status["missing"]) and status["offer"]["image"]["models"]
        for start in (app.media.create_image(ImageRequest(description="שועל")),
                      app.media.create_video(VideoRequest(scene="חוף"))):
            with pytest.raises(MediaRequestError) as err:
                await start
            assert err.value.status == 409 and err.value.code == "unavailable"
        assert not app.jobs.recent(area="images") and not app.jobs.recent(area="video")

    async def test_a_demonstration_says_so_everywhere_and_is_written_through_the_executor(self, app: Any) -> None:
        from jarvis.media import demo
        from jarvis.media.service import ImageRequest

        job = await app.media.create_image(ImageRequest(description="שועל בשלג", count=2, demo=True, lang="he"))
        assert job.real is False and job.sends == [] and job.cost is None and "הדגמה" in job.plan[0]
        done = await finished(app, job)
        assert done.status == "done" and [s.status for s in done.steps] == ["done", "skipped", "done"]
        items = done.result["items"]
        assert len(items) == 2 and all(i["real"] is False and i["format"] == "SVG" for i in items)
        svg = Path(items[0]["path"]).read_text(encoding="utf-8")
        assert demo.DEMO_IMAGE_HE in svg and "שועל בשלג" in svg and "<script" not in svg
        writes = [e for e in app.audit.recent(task_id=job.id) if e.outcome == "ok"]
        assert [(e.tool, e.agent_id) for e in writes] == [("write_file", "creative")] * 2

    async def test_a_real_image_goes_through_the_gate_as_the_creative_agent(self, app: Any,
                                                                           monkeypatch: pytest.MonkeyPatch) -> None:
        item, seen = await real_image(app, monkeypatch, style="watercolor")
        assert body(seen[0])["prompt"] == "a fox\n\nStyle: watercolor painting on textured paper"
        assert (item["format"], item["width"], item["real"]) == ("PNG", 6, True) and app.media.store.inside(item["path"])
        assert item["usd"] == pytest.approx(0.02) and item["cost"] == "≈ $0.02" and item["url"] == f"/api/media/file/{item['id']}"
        job = app.jobs.get(item["job_id"])
        assert job.real and any("api.x.ai" in line for line in job.sends)
        (entry,) = [e for e in app.audit.recent(task_id=job.id) if e.tool == "create_image"]
        assert (entry.agent_id, entry.decision, entry.outcome) == ("creative", "allowed", "ok")

    async def test_refining_sends_the_chosen_preview_with_the_change(self, app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        from jarvis.media.service import RefineImageRequest

        item, seen = await real_image(app, monkeypatch)
        done = await finished(app, await app.media.refine_image(RefineImageRequest(item_id=item["id"], change="make it night",
                                                                                   lang="en")))
        assert done.status == "done" and done.kind == "image.refine"
        sent = body(seen[-1])
        assert seen[-1].url.path == "/v1/images/edits" and sent["image"]["url"].startswith("data:image/png;base64,")
        assert sent["prompt"].startswith("Change this image as asked") and "make it night" in sent["prompt"]
        assert "Original description: a fox" in sent["prompt"] and done.result["items"][0]["parent"] == item["id"]
        assert [e.decision for e in app.audit.recent(task_id=done.id) if e.tool == "create_image"] == ["allowed"]

    async def test_a_file_of_the_users_needs_an_approval_and_a_no_sends_nothing(self, app: Any,
                                                                               monkeypatch: pytest.MonkeyPatch) -> None:
        from jarvis.media.service import ImageRequest

        photo = Path.home() / "Pictures" / "me.png"
        photo.write_bytes(png())
        seen = connect_xai(app, monkeypatch, images_answer())
        job = await app.media.create_image(ImageRequest(description="as an oil painting", source_path=str(photo), lang="en"))
        assert any("me.png" in line for line in job.sends) and any("approval" in line for line in job.plan)
        req = await asked(app.broker)
        assert req.tool == "create_image" and "me.png" in req.summary and req.risk == "high"
        assert any("xAI" in r and "me.png" in r for r in req.reasons) and not seen
        app.broker.respond(req.id, False)
        done = await finished(app, job)
        assert done.status == "failed" and "did not approve" in done.error and not seen

    async def test_xai_failures_reach_the_job_in_words_without_the_key(self, app: Any,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
        from jarvis.media.service import ImageRequest

        connect_xai(app, monkeypatch, lambda r: httpx.Response(401, json={"error": f"Incorrect API key {FAKE_KEY}"}))
        done = await finished(app, await app.media.create_image(ImageRequest(description="שועל", lang="he")))
        assert done.status == "failed" and "xAI דחה את המפתח" in done.error and done.can_retry
        assert FAKE_KEY not in json.dumps(done.view(), ensure_ascii=False) and not app.media.store.items()

    async def test_a_real_video_end_to_end(self, app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        from jarvis.media.service import VideoRequest

        polls: list[int] = []

        def answer(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(200, json={"request_id": "vid-1"})
            if request.url.host == "vidgen.x.ai":
                return httpx.Response(200, content=MP4)
            polls.append(1)
            if len(polls) < 2:
                return httpx.Response(200, json={"status": "pending", "progress": 50})
            return httpx.Response(200, json={"status": "done", "video": {"url": "https://vidgen.x.ai/v/1.mp4", "duration": 6},
                                             "usage": {"cost_in_usd_ticks": 1_200_000_000}})

        seen = connect_xai(app, monkeypatch, answer)
        job = await app.media.create_video(VideoRequest(scene="a lighthouse", motion="slow pan", lang="en"))
        assert job.cost == "≈ $0.12" and any("may still finish" in line for line in job.plan)
        done = await finished(app, job)
        assert done.status == "done", done.error
        (item,) = done.result["items"]
        assert (item["format"], item["seconds"], item["real"]) == ("MP4", 6.0, True)
        assert Path(item["path"]).read_bytes() == MP4 and done.result["usd"] == pytest.approx(0.12)
        assert body(seen[0])["prompt"] == "Scene: a lighthouse\nMotion: slow pan"

    async def test_stopping_a_video_stops_asking_and_keeps_nothing(self, app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        from jarvis.media.service import VideoRequest

        polls: list[int] = []

        def answer(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(200, json={"request_id": "vid-2"})
            polls.append(1)
            return httpx.Response(200, json={"status": "pending"})

        connect_xai(app, monkeypatch, answer)
        job = await app.media.create_video(VideoRequest(scene="rain", lang="en"))
        await until(lambda: len(polls) >= 2)
        app.jobs.cancel(job.id, "stopped by the user")
        done = await finished(app, job)
        assert done.status == "cancelled" and done.can_retry
        asked_so_far = len(polls)
        await asyncio.sleep(0.1)
        assert len(polls) == asked_so_far                       # no more questions to xAI once stopped
        assert not list(app.media.store.job_folder(job.id).glob("*.mp4")) and not app.media.store.items("video")

    async def test_a_video_demonstration_is_a_storyboard_and_nothing_else(self, app: Any) -> None:
        from jarvis.media import demo
        from jarvis.media.service import VideoRequest

        done = await finished(app, await app.media.create_video(VideoRequest(scene="חוף בשקיעה", motion="גלים", demo=True)))
        (item,) = done.result["items"]
        assert done.real is False and item["format"] == "SVG" and item["kind"] == "video"
        svg = Path(item["path"]).read_text(encoding="utf-8")
        assert demo.DEMO_VIDEO_HE in svg and "חוף בשקיעה" in svg and "גלים" in svg

    async def test_the_same_request_twice_runs_once(self, app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        from jarvis.media.service import ImageRequest

        gate = asyncio.Event()

        async def answer(request: httpx.Request) -> httpx.Response:
            await gate.wait()
            return images_answer()(request)

        seen = connect_xai(app, monkeypatch, answer)  # type: ignore[arg-type]
        first = await app.media.create_image(ImageRequest(description="twice", lang="en"))
        second = await app.media.create_image(ImageRequest(description="twice", lang="en"))
        assert second.id == first.id
        gate.set()
        assert (await finished(app, first)).status == "done" and len(seen) == 1

    async def test_previews_are_deleted_only_inside_the_media_folder(self, app: Any, tmp_path: Path) -> None:
        from jarvis.media.service import ImageRequest
        from jarvis.media.store import MediaItem

        done = await finished(app, await app.media.create_image(ImageRequest(description="x", count=2, demo=True)))
        first, second = done.result["items"]
        outside = tmp_path / "keep.png"
        outside.write_bytes(png())
        stray = MediaItem(kind="image", job_id="x", path=str(outside), format="PNG", mime="image/png")
        assert app.media.store.add([stray]) == []                      # never indexed
        app.media.store._items[stray.id] = stray                       # …even if the index were changed by hand
        assert app.media.file(stray.id) is None and await app.media.delete(stray.id) and outside.exists()
        assert await app.media.delete(first["id"]) and not Path(first["path"]).exists()
        assert await app.media.clear("image") == 1 and not Path(second["path"]).exists() and not app.media.items()


class TestSave:
    async def test_saving_copies_once_to_the_place_picked_with_the_real_extension(self, app: Any,
                                                                                monkeypatch: pytest.MonkeyPatch) -> None:
        from jarvis.media.service import SaveRequest

        item, _ = await real_image(app, monkeypatch)
        picked = Path.home() / "Documents" / "fox.jpg"                 # the preview is really a PNG
        for approve in (False, True):
            job = await app.media.save(SaveRequest(item_id=item["id"], target=str(picked), lang="en"))
            assert job.sends == [] and any(".png" in line for line in job.plan)
            req = await asked(app.broker)                              # outside the agents' folders: always asked
            assert req.tool == "copy_path" and str(picked.with_suffix(".png")) in json.dumps(req.details)
            app.broker.respond(req.id, approve)
            done = await finished(app, job)
            assert done.status == ("done" if approve else "failed")
            assert not [g for g in app.permissions.grants("creative", include_expired=True) if g.source == "user"]
        assert picked.with_suffix(".png").read_bytes() == Path(item["path"]).read_bytes() and not picked.exists()
        assert app.media.store.get(item["id"]).saved_to == [str(picked.with_suffix(".png"))]

    async def test_what_is_refused_before_anything_starts(self, app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        from jarvis.media.service import ImageRequest, MediaRequestError, SaveRequest

        item, _ = await real_image(app, monkeypatch)
        existing = Path.home() / "Documents" / "taken.png"
        existing.write_bytes(b"mine")
        demo_done = await finished(app, await app.media.create_image(ImageRequest(description="d", demo=True)))
        cases = [(SaveRequest(item_id=item["id"], target=str(existing)), 409, "exists"),
                 (SaveRequest(item_id=demo_done.result["items"][0]["id"], target=str(existing)), 409, "demo"),
                 (SaveRequest(item_id=item["id"], target="fox.png"), 400, "media_request"),
                 (SaveRequest(item_id="media_nope", target=str(existing)), 404, "not_found")]
        for req, status, code in cases:
            with pytest.raises(MediaRequestError) as err:
                await app.media.save(req)
            assert (err.value.status, err.value.code) == (status, code)
        assert existing.read_bytes() == b"mine" and not app.jobs.recent(kind="image.save")
