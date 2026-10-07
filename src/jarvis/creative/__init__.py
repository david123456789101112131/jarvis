"""Offline creative generators used by the Creative Agent (and by project templates).

Palettes, logos, icons, UI mockups, presentations (HTML + PowerPoint), raster images, copywriting
and animations — real, deterministic output that needs no AI model. Raster images need Pillow
(the `creative` extra), so `jarvis.creative.images` loads only when one of its functions is used.
"""

from __future__ import annotations

from typing import Any

from .animation import animate_logo_svg, animated_loader_svg, css_animation, demo_page, generate_animations
from .assets import Asset, build_gallery, save_assets
from .domains import Domain, detect_domain, get_domain
from .icons import ICON_NAMES, generate_icon_set, icon_svg, icons_for_brief
from .logo import favicon_svg, generate_logos, initials
from .mockup import SCREENS, generate_mockup, wireframe_svg
from .palette import MOODS, Color, Palette, contrast_ratio, generate_palette
from .presentation import (
    Slide,
    build_presentation_html,
    build_presentation_pptx,
    generate_presentation,
    outline_from_topic,
)
from .texts import (
    brainstorm,
    email,
    email_draft,
    hashtags,
    landing_copy,
    names,
    product_description,
    slogans,
    social_post,
    taglines,
    testimonials,
)

_IMAGE_API = ("render_banner", "render_social_post", "render_gradient", "render_icon_png", "render_icon_sheet",
              "visual_order", "has_raqm")


def __getattr__(name: str) -> Any:
    if name in _IMAGE_API:
        from . import images

        return getattr(images, name)
    raise AttributeError(f"module 'jarvis.creative' has no attribute {name!r}")


def images_available() -> bool:
    """True when Pillow is installed (banners, social posts and PNG icons)."""
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True


__all__ = [
    "ICON_NAMES", "MOODS", "SCREENS", "Asset", "Color", "Domain", "Palette", "Slide",
    "animate_logo_svg", "animated_loader_svg", "brainstorm", "build_gallery", "build_presentation_html",
    "build_presentation_pptx", "contrast_ratio", "css_animation", "demo_page", "detect_domain", "email",
    "email_draft", "favicon_svg", "generate_animations", "generate_icon_set", "generate_logos", "generate_mockup",
    "generate_palette", "generate_presentation", "get_domain", "hashtags", "icon_svg", "icons_for_brief",
    "images_available", "initials", "landing_copy", "names", "outline_from_topic", "product_description",
    "save_assets", "slogans", "social_post", "taglines", "testimonials", "wireframe_svg", *_IMAGE_API,
]
