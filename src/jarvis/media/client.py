"""xAI's Grok Imagine over its REST API: images (made from a description, or from an image sent along) and
videos (started, then asked about until they are ready, then downloaded).

The API key is read from the secret store for each request and goes only into the `Authorization` header;
every error the user may read is built here in their language and passed through the redactor, so it never
carries the key. Nothing is retried on its own: every request may cost money, so a failure is reported and the
user decides whether to try again.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from jarvis.common.errors import ToolError
from jarvis.common.i18n import t
from jarvis.media import catalog
from jarvis.media.files import data_uri, probe

BASE_URL = "https://api.x.ai/v1"
IMAGE_TIMEOUT_S = 180.0
VIDEO_START_TIMEOUT_S = 90.0
POLL_TIMEOUT_S = 30.0
DOWNLOAD_TIMEOUT_S = 300.0
MAX_DOWNLOAD_BYTES = 500_000_000
_CREDIT_WORDS = ("credit", "spending limit", "billing", "insufficient", "quota")
_REFUSAL_WORDS = ("moderation", "content polic", "blocked", "safety", "not allowed", "violat", "nsfw")


class MediaError(ToolError):
    """A creation that did not work, with a reason a person can act on (`details["kind"]` classifies it)."""

    code = "media_error"


@dataclass
class Picture:
    data: bytes
    mime: str = ""


@dataclass
class ImageBatch:
    pictures: list[Picture]
    usd: float | None = None             # what xAI reported it charged
    model: str = ""


@dataclass
class VideoState:
    status: str                          # "pending" | "done" | "failed" | "expired"
    progress: float | None = None        # 0..1 when xAI says
    url: str = ""
    seconds: float | None = None
    usd: float | None = None
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


Http = Callable[[], httpx.AsyncClient]


class XaiMedia:
    def __init__(self, key: Callable[[], str | None], http: Http, *, base_url: str = BASE_URL,
                 redact: Callable[[str], str] = lambda s: s) -> None:
        self.key = key
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.redact = redact
        self.requests = 0                # requests sent (each may cost money)

    # ------------------------------------------------------------------ images
    async def images(self, *, prompt: str, model: str, count: int, aspect_ratio: str, resolution: str,
                     source: Picture | None = None, lang: str = "he") -> ImageBatch:
        """Make `count` images from `prompt`; with `source`, from that image (an edit) instead."""
        body: dict[str, Any] = {"model": model, "prompt": prompt, "response_format": "b64_json"}
        if source is not None:
            body["image"] = {"url": _uri(source), "type": "image_url"}
            if count > 1:
                body["n"] = count
            path = "/images/edits"
        else:
            body.update({"n": count, "aspect_ratio": aspect_ratio, "resolution": resolution})
            path = "/images/generations"
        data = await self._post(path, body, timeout=IMAGE_TIMEOUT_S, lang=lang)
        if "data" not in data and data.get("request_id"):
            data = await self._wait_image(str(data["request_id"]), lang)
        blocked = data.get("block_reason")
        if blocked:
            raise MediaError(t(lang, f"xAI חסם את הבקשה לפי מדיניות התוכן שלו: {self._clip(str(blocked))}",
                               f"xAI blocked the request under its content policy: {self._clip(str(blocked))}"),
                             details={"kind": "refused"})
        pictures = []
        for item in data.get("data") or []:
            if not isinstance(item, dict):
                continue
            mime = str(item.get("mime_type") or "")
            if item.get("b64_json"):
                try:
                    pictures.append(Picture(base64.b64decode(item["b64_json"], validate=False), mime))
                except (ValueError, TypeError):
                    continue
            elif item.get("url"):
                pictures.append(Picture(await self._fetch(str(item["url"]), lang), mime))
        if not pictures:
            raise MediaError(t(lang, "xAI ענה בלי תמונות. לא נוצר דבר; אפשר לנסות שוב או לשנות את התיאור.",
                               "xAI answered without any images. Nothing was made; try again or change the description."),
                             details={"kind": "bad_response"})
        return ImageBatch(pictures, catalog.reported_usd(data.get("usage")), str(data.get("model") or model))

    async def _wait_image(self, request_id: str, lang: str) -> dict[str, Any]:
        """A deferred image request: ask about it until it is done (or failed)."""
        for _ in range(int(IMAGE_TIMEOUT_S / 2)):
            await asyncio.sleep(2.0)
            data = await self._get(f"/images/{request_id}", lang=lang)
            status = str(data.get("status") or "")
            if status == "done" or (data.get("data") and status != "pending"):
                return data
            if status in ("failed", "expired"):
                raise MediaError(self._failed(data, lang), details={"kind": status})
        raise MediaError(t(lang, "xAI לא סיים את התמונות בזמן.", "xAI did not finish the images in time."),
                         details={"kind": "timeout"})

    # ------------------------------------------------------------------ videos
    async def start_video(self, *, prompt: str, model: str, seconds: int, aspect_ratio: str, resolution: str,
                          image: Picture | None = None, references: list[Picture] | None = None,
                          lang: str = "he") -> str:
        """Start making a video; returns the request id to ask about."""
        body: dict[str, Any] = {"model": model, "prompt": prompt, "duration": seconds, "resolution": resolution}
        if image is not None:
            body["image"] = {"url": _uri(image)}
        else:
            body["aspect_ratio"] = aspect_ratio
        if references:
            body["reference_images"] = [{"url": _uri(p)} for p in references]
        data = await self._post("/videos/generations", body, timeout=VIDEO_START_TIMEOUT_S, lang=lang)
        request_id = str(data.get("request_id") or "")
        if not request_id:
            raise MediaError(t(lang, "xAI לא החזיר מספר בקשה לסרטון.", "xAI did not return a request id for the video."),
                             details={"kind": "bad_response"})
        return request_id

    async def video_state(self, request_id: str, lang: str = "he") -> VideoState:
        data = await self._get(f"/videos/{request_id}", lang=lang, accept_pending=True)
        raw_video = data.get("video")
        video: dict[str, Any] = raw_video if isinstance(raw_video, dict) else {}
        status = str(data.get("status") or ("done" if video.get("url") else "pending")).lower()
        progress = data.get("progress")
        fraction = None
        if isinstance(progress, int | float) and not isinstance(progress, bool):
            fraction = max(0.0, min(1.0, float(progress) / 100.0 if progress > 1 else float(progress)))
        duration = video.get("duration")
        state = VideoState(status=status, progress=fraction, url=str(video.get("url") or ""),
                           seconds=float(duration) if isinstance(duration, int | float) else None,
                           usd=catalog.reported_usd(data.get("usage")), raw=data)
        if status in ("failed", "expired"):
            state.error = self._failed(data, lang)
        return state

    async def download(self, url: str, dest: Path, *, lang: str = "he", max_bytes: int = MAX_DOWNLOAD_BYTES) -> int:
        """Save the finished video at `url` to `dest` (written to a temporary name first); its size in bytes."""
        if not url.startswith("https://"):
            raise MediaError(t(lang, "כתובת הסרטון שהתקבלה לא מאובטחת, ולכן לא הורדתי אותה.",
                               "The video's address was not a secure (https) one, so it was not downloaded."),
                             details={"kind": "bad_response"})
        tmp = dest.with_name(dest.name + ".part")
        size = 0
        try:
            async with self.http().stream("GET", url, timeout=DOWNLOAD_TIMEOUT_S) as response:
                if response.status_code != 200:
                    raise MediaError(t(lang, f"הורדת הסרטון מ-xAI נכשלה (HTTP {response.status_code}).",
                                       f"Downloading the video from xAI failed (HTTP {response.status_code})."),
                                     details={"kind": "unavailable", "status": response.status_code})
                await asyncio.to_thread(dest.parent.mkdir, parents=True, exist_ok=True)
                with tmp.open("wb") as fh:
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise MediaError(t(lang, "הסרטון גדול מדי להורדה.", "The video is too large to download."),
                                             details={"kind": "bad_response"})
                        fh.write(chunk)
            os.replace(tmp, dest)
        except httpx.TimeoutException:
            raise MediaError(t(lang, "הורדת הסרטון לקחה יותר מדי זמן.", "Downloading the video took too long."),
                             details={"kind": "timeout"}) from None
        except httpx.HTTPError as exc:
            raise MediaError(t(lang, f"אין חיבור להורדת הסרטון: {type(exc).__name__}",
                               f"No connection to download the video: {type(exc).__name__}"),
                             details={"kind": "network"}) from None
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
        return size

    # ------------------------------------------------------------------ HTTP
    def _headers(self, lang: str) -> dict[str, str]:
        key = (self.key() or "").strip()
        if not key:
            raise MediaError(t(lang, "אין מפתח API של xAI. אפשר להוסיף אותו בהגדרות ← מפתחות API.",
                               "There is no xAI API key. Add it in Settings → API keys."), details={"kind": "no_key"})
        if not key.isascii() or not key.isprintable():
            raise MediaError(t(lang, "במפתח xAI השמור יש תווים שאין במפתחות (אולי הוקלד במקלדת עברית); "
                                     "צריך להזין אותו שוב בהגדרות ← מפתחות API.",
                               "The saved xAI key has characters keys never contain (typed with a Hebrew keyboard?); "
                               "enter it again in Settings → API keys."), details={"kind": "bad_key"})
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    async def _post(self, path: str, body: dict[str, Any], *, timeout: float, lang: str) -> dict[str, Any]:
        headers = self._headers(lang)
        self.requests += 1
        try:
            response = await self.http().post(self.base_url + path, content=json.dumps(body), headers=headers,
                                              timeout=timeout)
        except httpx.TimeoutException:
            raise MediaError(t(lang, "xAI לא ענה בזמן. אולי הבקשה עוד תסתיים אצלם, אבל JARVIS לא שמר דבר.",
                               "xAI did not answer in time. It may still finish on their side, but JARVIS saved nothing."),
                             details={"kind": "timeout"}) from None
        except httpx.HTTPError as exc:
            raise MediaError(t(lang, f"אין חיבור ל-xAI ({type(exc).__name__}). כדאי לבדוק את האינטרנט.",
                               f"No connection to xAI ({type(exc).__name__}). Check the internet connection."),
                             details={"kind": "network"}) from None
        return self._json(response, lang)

    async def _get(self, path: str, *, lang: str, accept_pending: bool = False) -> dict[str, Any]:
        headers = self._headers(lang)
        try:
            response = await self.http().get(self.base_url + path, headers=headers, timeout=POLL_TIMEOUT_S)
        except httpx.TimeoutException:
            return {"status": "pending"} if accept_pending else {}
        except httpx.HTTPError as exc:
            raise MediaError(t(lang, f"אין חיבור ל-xAI ({type(exc).__name__}).", f"No connection to xAI ({type(exc).__name__})."),
                             details={"kind": "network"}) from None
        if response.status_code == 202:
            return {"status": "pending", **_body(response)}
        return self._json(response, lang)

    async def _fetch(self, url: str, lang: str) -> bytes:
        """An image xAI returned as a link instead of its bytes."""
        try:
            response = await self.http().get(url, timeout=DOWNLOAD_TIMEOUT_S)
        except httpx.HTTPError as exc:
            raise MediaError(t(lang, f"הורדת התמונה מ-xAI נכשלה ({type(exc).__name__}).",
                               f"Downloading the image from xAI failed ({type(exc).__name__})."),
                             details={"kind": "network"}) from None
        if response.status_code != 200:
            raise MediaError(t(lang, f"הורדת התמונה מ-xAI נכשלה (HTTP {response.status_code}).",
                               f"Downloading the image from xAI failed (HTTP {response.status_code})."),
                             details={"kind": "unavailable"})
        return response.content

    def _json(self, response: httpx.Response, lang: str) -> dict[str, Any]:
        if 200 <= response.status_code < 300:
            data = _body(response)
            if not data and response.content:
                raise MediaError(t(lang, "xAI ענה בתשובה שאי אפשר לקרוא.", "xAI sent an answer that could not be read."),
                                 details={"kind": "bad_response"})
            return data
        raise self.error(response.status_code, _message(response), lang)

    def error(self, status: int, reason: str, lang: str) -> MediaError:
        """The failure in words: what happened and what the user can do about it."""
        why = self._clip(reason)
        low = why.lower()
        tail = f" ({why})" if why else ""
        if status == 401:
            return MediaError(t(lang, f"xAI דחה את המפתח (401). צריך לבדוק אותו בהגדרות ← מפתחות API.{tail}",
                                f"xAI refused the API key (401). Check it in Settings → API keys.{tail}"),
                              details={"kind": "auth", "status": status})
        if status in (402, 403) and any(w in low for w in _CREDIT_WORDS):
            return MediaError(t(lang, "אין מספיק קרדיטים בחשבון xAI, או שהגעת למגבלת ההוצאה החודשית. "
                                      "אפשר להוסיף ב-console.x.ai ← Billing.",
                                "The xAI account has no credits left, or reached its monthly spending limit. "
                                "Add credits at console.x.ai → Billing."), details={"kind": "credits", "status": status})
        if any(w in low for w in _REFUSAL_WORDS) and status in (400, 403, 422):
            return MediaError(t(lang, f"xAI סירב לבקשה לפי מדיניות התוכן שלו: {why}",
                                f"xAI refused the request under its content policy: {why}"),
                              details={"kind": "refused", "status": status})
        if status == 403:
            return MediaError(t(lang, f"xAI לא מתיר את הבקשה הזו (403).{tail}",
                                f"xAI does not allow this request (403).{tail}"), details={"kind": "forbidden", "status": status})
        if status == 429:
            return MediaError(t(lang, "נשלחו יותר מדי בקשות ל-xAI כרגע. כדאי לנסות שוב בעוד דקה.",
                                "Too many requests to xAI right now. Try again in a minute."),
                              details={"kind": "rate_limit", "status": status})
        if status >= 500:
            return MediaError(t(lang, f"השירות של xAI לא זמין כרגע (HTTP {status}). אפשר לנסות שוב מאוחר יותר.",
                                f"xAI's service is not available right now (HTTP {status}). Try again later."),
                              details={"kind": "unavailable", "status": status})
        return MediaError(t(lang, f"xAI לא קיבל את הבקשה (HTTP {status}).{tail}",
                            f"xAI did not accept the request (HTTP {status}).{tail}"),
                          details={"kind": "bad_request", "status": status})

    def _failed(self, data: dict[str, Any], lang: str) -> str:
        status = str(data.get("status") or "")
        reason = self._clip(_reason(data.get("error")))
        if status == "expired":
            return t(lang, "הבקשה פגה אצל xAI לפני שהסרטון הוכן. אפשר לנסות שוב.",
                     "The request expired at xAI before the video was ready. Try again.")
        return t(lang, f"xAI לא הצליח ליצור את זה{': ' + reason if reason else '.'}",
                 f"xAI could not make it{': ' + reason if reason else '.'}")

    def _clip(self, text: str, limit: int = 300) -> str:
        text = " ".join(str(text or "").split())
        return self.redact(text[:limit] + ("…" if len(text) > limit else ""))


def _uri(picture: Picture) -> str:
    """The image as the data URI xAI takes, typed by what its bytes really are."""
    found = probe(picture.data)
    return data_uri(picture.data, picture.mime or (found.mime if found else "image/png"))


def _body(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except (ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _reason(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("message") or value.get("error") or value.get("code") or "")
    return str(value or "")


def _message(response: httpx.Response) -> str:
    data = _body(response)
    for key in ("error", "message", "detail", "code"):
        if data.get(key):
            return _reason(data[key])
    return response.text[:300] if response.text and not response.text.lstrip().startswith("<") else ""


__all__ = ["BASE_URL", "ImageBatch", "MediaError", "Picture", "VideoState", "XaiMedia"]
