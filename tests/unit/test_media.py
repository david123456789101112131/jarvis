"""Image and video creation: the xAI client, the catalog, the tools behind the permission gate, the service's jobs
and the save flow.

No test talks to xAI or any other service: every request goes to an `httpx.MockTransport`, and the key is a
made-up one (`FAKE_KEY`). The real environment's XAI_API_KEY is removed before each test.
"""

from __future__ import annotations

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
