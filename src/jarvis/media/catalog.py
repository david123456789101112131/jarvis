"""What the image and video screens can ask xAI for, and what it costs: one place for the facts.

Read on 2026-10-19 (`PRICES_CHECKED`):
* prices: docs.x.ai's pricing page ("Last updated: October 10, 2026");
* endpoints and fields: docs.x.ai's REST reference for images and videos and its video guide, with the enums
  (aspect ratios, resolutions, the 1–15 second duration, the 1–10 images, the 1–7 reference images) from the
  published schema of the same API (`PRICE_SOURCES`).

The screens show the estimated price next to every Create button; a finished job shows what xAI itself reported
it charged (`usage.cost_in_usd_ticks`, 10,000,000,000 ticks to the dollar).

The text JARVIS sends is built here too (`image_prompt`, `video_prompt`, …), so the screen can show the user
exactly what will leave the computer before they press Create.
"""

from __future__ import annotations

from dataclasses import dataclass, field

PRICES_CHECKED = "2026-10-19"
PRICE_SOURCES = (
    "https://docs.x.ai/developers/pricing",
    "https://docs.x.ai/developers/rest-api-reference/inference/images",
    "https://docs.x.ai/developers/rest-api-reference/inference/videos",
    "https://docs.x.ai/developers/model-capabilities/video",
)
SERVICE_NAME = "Grok Imagine (xAI)"
SERVICE_HOST = "api.x.ai"
KEY_NAME = "XAI_API_KEY"
TICKS_PER_USD = 10_000_000_000      # xAI reports what a request cost in these ("usage.cost_in_usd_ticks")


@dataclass(frozen=True)
class ImageModel:
    id: str
    label_he: str
    label_en: str
    usd_per_image: dict[str, float]          # resolution -> price of one image made
    usd_per_input: float                     # each image sent along (an edit's source)


@dataclass(frozen=True)
class VideoModel:
    id: str
    label_he: str
    label_en: str
    usd_per_second: dict[str, float]         # resolution -> price of one second
    usd_per_input: float                     # each image sent along (a first frame or a reference)
    references: bool                         # takes reference images
    resolutions: tuple[str, ...] = field(default=("480p", "720p", "1080p"))


IMAGE_MODELS: tuple[ImageModel, ...] = (
    ImageModel("grok-imagine-image", "רגיל", "Standard", {"1k": 0.02, "2k": 0.02}, 0.002),
    ImageModel("grok-imagine-image-quality", "איכות גבוהה", "High quality", {"1k": 0.05, "2k": 0.07}, 0.01),
)
VIDEO_MODELS: tuple[VideoModel, ...] = (
    VideoModel("grok-imagine-video-1.5-lite", "מהיר וזול", "Lite (fast, low price)",
               {"480p": 0.02, "720p": 0.03, "1080p": 0.14}, 0.01, references=False),
    VideoModel("grok-imagine-video-1.5", "מלא", "Full",
               {"480p": 0.08, "720p": 0.14, "1080p": 0.25}, 0.01, references=True),
)
DEFAULT_IMAGE_MODEL = IMAGE_MODELS[0].id
DEFAULT_VIDEO_MODEL = VIDEO_MODELS[0].id

IMAGE_ASPECTS = ("1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "2:1", "1:2", "19.5:9", "9:19.5", "20:9",
                 "9:20", "auto")
IMAGE_RESOLUTIONS = ("1k", "2k")
MAX_IMAGES = 4                      # per request (xAI takes up to 10; four keeps one click's price small)
VIDEO_ASPECTS = ("16:9", "9:16", "1:1", "4:3", "3:4", "3:2", "2:3")
VIDEO_RESOLUTIONS = ("480p", "720p", "1080p")
REFERENCE_RESOLUTIONS = ("480p", "720p")      # a video made from reference images goes up to 720p
MIN_SECONDS, MAX_SECONDS, DEFAULT_SECONDS = 1, 15, 6
MAX_REFERENCES = 3                  # xAI takes up to 7; three keep the upload (and the price) small
MAX_PROMPT = 4000
MAX_INPUT_BYTES = 10_000_000        # an image sent along (base64 grows it by a third)
INPUT_FORMATS = ("JPEG", "PNG", "WEBP")
VIDEO_POLL_S = 5.0                  # how often a video in the making is asked about (xAI's own examples: 5 s)
VIDEO_WAIT_S = 15 * 60              # …and for how long before JARVIS gives up waiting


@dataclass(frozen=True)
class Style:
    id: str
    label_he: str
    label_en: str
    fragment: str                   # what is added to the description (English: the model follows it best)


STYLES: tuple[Style, ...] = (
    Style("none", "בלי סגנון מיוחד", "No particular style", ""),
    Style("photo", "צילום מציאותי", "Realistic photo", "photorealistic photograph, natural light, high detail"),
    Style("cinematic", "קולנועי", "Cinematic", "cinematic film still, dramatic lighting, shallow depth of field"),
    Style("illustration", "איור דיגיטלי", "Digital illustration", "detailed digital illustration"),
    Style("watercolor", "צבעי מים", "Watercolor", "watercolor painting on textured paper"),
    Style("oil", "ציור שמן", "Oil painting", "oil painting with visible brush strokes"),
    Style("anime", "אנימה", "Anime", "anime style, clean line art, vibrant colors"),
    Style("3d", "הדמיית תלת־ממד", "3D render", "3D render, soft global illumination"),
    Style("pixel", "פיקסל ארט", "Pixel art", "pixel art, limited color palette"),
    Style("sketch", "סקיצה בעיפרון", "Pencil sketch", "pencil sketch with detailed shading"),
    Style("minimal", "מינימליסטי", "Minimalist", "minimalist flat design, simple shapes"),
)


def image_model(model_id: str) -> ImageModel:
    return next((m for m in IMAGE_MODELS if m.id == model_id), IMAGE_MODELS[0])


def video_model(model_id: str) -> VideoModel:
    return next((m for m in VIDEO_MODELS if m.id == model_id), VIDEO_MODELS[0])


def style(style_id: str) -> Style:
    return next((s for s in STYLES if s.id == style_id), STYLES[0])


# ---------------------------------------------------------------------------- the text that is sent
def image_prompt(description: str, style_id: str = "none") -> str:
    """The description as the user typed it, then the style's words: exactly what goes to xAI."""
    text = description.strip()
    fragment = style(style_id).fragment
    return f"{text}\n\nStyle: {fragment}" if fragment else text


def variation_prompt(original: str) -> str:
    return ("A variation of this image: keep the subject, composition and style, change the details. "
            f"Original description: {original.strip()}")


def change_prompt(original: str, change: str) -> str:
    """A correction in the user's words, applied to the image (or the scene) made from `original`."""
    return (f"Change this image as asked, keep everything else as it is: {change.strip()}\n"
            f"Original description: {original.strip()}")


VIDEO_PARTS = (("scene", "Scene"), ("characters", "Characters"), ("motion", "Motion"), ("lighting", "Lighting"),
               ("style", "Style"))


def video_prompt(*, scene: str, characters: str = "", motion: str = "", lighting: str = "", style: str = "") -> str:
    """One labelled line for each part the user filled in, in a fixed order."""
    given = {"scene": scene, "characters": characters, "motion": motion, "lighting": lighting, "style": style}
    return "\n".join(f"{label}: {given[key].strip()}" for key, label in VIDEO_PARTS if given[key].strip())


def video_change_prompt(original: str, change: str) -> str:
    return f"{original.strip()}\nChange: {change.strip()}"


# ---------------------------------------------------------------------------- prices
def image_cost(model_id: str, resolution: str, count: int, *, inputs: int = 0) -> float:
    """The estimated price: each image made, plus each image sent along (xAI bills inputs too)."""
    m = image_model(model_id)
    per = m.usd_per_image.get(resolution, max(m.usd_per_image.values()))
    return per * count + m.usd_per_input * inputs


def video_cost(model_id: str, resolution: str, seconds: int, *, inputs: int = 0) -> float:
    m = video_model(model_id)
    per = m.usd_per_second.get(resolution, max(m.usd_per_second.values()))
    return per * seconds + m.usd_per_input * inputs


def usd(amount: float) -> str:
    return "<$0.01" if 0 < amount < 0.005 else f"${amount:.2f}"


def reported_usd(usage: object) -> float | None:
    """What xAI said the request cost, from its `usage` object (None when it did not say)."""
    if not isinstance(usage, dict):
        return None
    ticks = usage.get("cost_in_usd_ticks")
    if isinstance(ticks, bool) or not isinstance(ticks, int | float) or ticks < 0:
        return None
    return ticks / TICKS_PER_USD


def offer(lang: str) -> dict[str, object]:
    """What the screens may choose from, with labels in `lang` and the prices (shown before every Create)."""
    he = lang == "he"
    return {
        "image": {
            "models": [{"id": m.id, "label": m.label_he if he else m.label_en, "usd_per_image": m.usd_per_image,
                        "usd_per_input": m.usd_per_input} for m in IMAGE_MODELS],
            "aspect_ratios": list(IMAGE_ASPECTS),
            "resolutions": list(IMAGE_RESOLUTIONS),
            "max_images": MAX_IMAGES,
            "styles": [{"id": s.id, "label": s.label_he if he else s.label_en, "fragment": s.fragment} for s in STYLES],
            "input_formats": list(INPUT_FORMATS),
            "max_input_bytes": MAX_INPUT_BYTES,
        },
        "video": {
            "models": [{"id": m.id, "label": m.label_he if he else m.label_en, "usd_per_second": m.usd_per_second,
                        "usd_per_input": m.usd_per_input, "references": m.references,
                        "resolutions": list(m.resolutions)} for m in VIDEO_MODELS],
            "aspect_ratios": list(VIDEO_ASPECTS),
            "resolutions": list(VIDEO_RESOLUTIONS),
            "reference_resolutions": list(REFERENCE_RESOLUTIONS),
            "seconds": {"min": MIN_SECONDS, "max": MAX_SECONDS, "default": DEFAULT_SECONDS},
            "max_references": MAX_REFERENCES,
            "parts": [key for key, _label in VIDEO_PARTS],
            "part_labels": dict(VIDEO_PARTS),            # the labels the text sent to xAI uses (`video_prompt`)
            "input_formats": list(INPUT_FORMATS),
            "max_input_bytes": MAX_INPUT_BYTES,
            "wait_minutes": VIDEO_WAIT_S // 60,
        },
        "prices_checked": PRICES_CHECKED,
        "price_sources": list(PRICE_SOURCES),
        "max_prompt": MAX_PROMPT,
    }


__all__ = [
    "DEFAULT_IMAGE_MODEL", "DEFAULT_SECONDS", "DEFAULT_VIDEO_MODEL", "IMAGE_ASPECTS", "IMAGE_MODELS",
    "IMAGE_RESOLUTIONS", "INPUT_FORMATS", "KEY_NAME", "MAX_IMAGES", "MAX_INPUT_BYTES", "MAX_PROMPT",
    "MAX_REFERENCES", "MAX_SECONDS", "MIN_SECONDS", "PRICES_CHECKED", "PRICE_SOURCES", "REFERENCE_RESOLUTIONS",
    "SERVICE_HOST", "SERVICE_NAME", "STYLES", "TICKS_PER_USD", "VIDEO_ASPECTS", "VIDEO_MODELS", "VIDEO_PARTS",
    "VIDEO_POLL_S", "VIDEO_RESOLUTIONS", "VIDEO_WAIT_S", "ImageModel", "Style", "VideoModel", "change_prompt",
    "image_cost", "image_model", "image_prompt", "offer", "reported_usd", "style", "usd", "variation_prompt",
    "video_change_prompt", "video_cost", "video_model", "video_prompt",
]
