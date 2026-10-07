"""Offline creative generators: palettes, logos, icons, mockups, decks, images, texts and animations."""

from __future__ import annotations

import html.parser
import xml.etree.ElementTree as ET

import pytest

from jarvis import creative
from jarvis.creative import animation, icons, logo, mockup, palette, presentation, texts
from jarvis.creative.assets import Asset, build_gallery, save_assets


def _svg(text: str) -> ET.Element:
    root = ET.fromstring(text)
    assert root.tag.endswith("svg")
    return root


def _parses_as_html(text: str) -> None:
    parser = html.parser.HTMLParser()
    parser.feed(text)
    parser.close()
    assert "<html" in text.lower()


# ------------------------------------------------------------------------------------------ palette
def test_palette_is_deterministic_and_readable():
    a = palette.generate_palette("חנות קפה")
    b = palette.generate_palette("חנות קפה")
    assert a.to_dict() == b.to_dict()
    assert palette.contrast_ratio(a.text, a.background) >= 4.5
    assert palette.contrast_ratio(a.on_primary, a.primary) >= 3
    assert "--color-primary" in a.to_css()
    _svg(a.to_svg())


@pytest.mark.parametrize("brief,mood", [("coffee shop", "warm"), ("AI startup", "tech"), ("יוקרה", "luxury")])
def test_palette_follows_the_brief(brief, mood):
    assert palette.detect_mood(brief) == mood


def test_dark_palette_keeps_contrast():
    dark = palette.generate_palette("music app", dark=True)
    assert palette.relative_luminance(dark.background) < 0.2
    assert palette.contrast_ratio(dark.text, dark.background) >= 4.5


# ------------------------------------------------------------------------------------------ logos & icons
@pytest.mark.parametrize("name", ["Nova Labs", "קפה שקד"])
def test_logos_are_valid_svg_with_the_name(name):
    assets = logo.generate_logos(name, count=4)
    assert len(assets) == 4 and len({a.name for a in assets}) == 4
    for a in assets:
        assert a.kind == "svg"
        _svg(a.content)
    assert any(name.split()[0] in a.content for a in assets)
    if name.startswith("ק"):
        assert any('direction="rtl"' in a.content or "rtl" in a.content for a in assets)


def test_icon_library():
    assert len(icons.ICON_NAMES) >= 40
    for name in icons.ICON_NAMES:
        _svg(icons.icon_svg(name))
    assert set(icons.icons_for_brief("חנות קפה", 6)) & {"coffee", "cart", "bag"}
    sets = icons.generate_icon_set(brief="online store", palette=palette.generate_palette("store"))
    assert any(a.name.startswith("icon-") for a in sets)
    for a in sets:
        _svg(a.content)


# ------------------------------------------------------------------------------------------ mockups
@pytest.mark.parametrize("lang,brief", [("he", "אפליקציה לבית קפה"), ("en", "fitness studio app")])
def test_mockups(lang, brief, tmp_path):
    assets = mockup.generate_mockup(brief, lang=lang)
    pages = [a for a in assets if a.kind == "html"]
    frames = [a for a in assets if a.kind == "svg"]
    assert len(pages) == len(mockup.SCREENS) + 1 and len(frames) == len(mockup.SCREENS)
    for a in pages:
        _parses_as_html(a.content)
        assert f'dir="{"rtl" if lang == "he" else "ltr"}"' in a.content
    for a in frames:
        _svg(a.content)
    paths = save_assets(assets, tmp_path)
    index = (tmp_path / "index.html").read_text(encoding="utf-8")
    for page in pages[1:]:
        assert f'href="{page.filename}"' in index
    assert all(p.exists() for p in paths)
    assert mockup.generate_mockup(brief, lang=lang)[1].content == pages[1].content  # deterministic


# ------------------------------------------------------------------------------------------ presentations
@pytest.mark.parametrize("lang,topic", [("he", "איך לפתוח בית קפה"), ("en", "Quarterly sales review")])
def test_presentation_outline_html_and_pptx(lang, topic, tmp_path):
    pptx = pytest.importorskip("pptx")
    slides = presentation.outline_from_topic(topic, lang=lang, n_slides=8)
    assert 6 <= len(slides) <= 10 and slides[0].layout == "title"
    deck = presentation.build_presentation_html(topic, slides, lang=lang)
    _parses_as_html(deck)
    path = presentation.build_presentation_pptx(topic, slides, tmp_path / "deck.pptx", lang=lang)
    prs = pptx.Presentation(str(path))
    assert len(prs.slides) == len(slides)
    assert any(s.has_notes_slide and s.notes_slide.notes_text_frame.text.strip() for s in prs.slides)
    xml = "".join(shape._element.xml for s in prs.slides for shape in s.shapes)
    assert ('rtl="1"' in xml) == (lang == "he")


def test_generate_presentation_assets():
    assets = presentation.generate_presentation("Team onboarding", lang="en")
    assert [a.kind for a in assets] == ["html", "pptx", "json"]
    assert isinstance(assets[1].content, bytes) and assets[1].content[:2] == b"PK"


# ------------------------------------------------------------------------------------------ images
def test_banner_and_icons_png(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    out = creative.render_banner("קפה שקד", "הקפה הכי טרי בעיר", palette=palette.generate_palette("coffee"),
                                 lang="he", path=tmp_path / "banner.png")
    with Image.open(out) as img:
        assert img.size == (1200, 630)
    post = creative.render_social_post("Big sale", "Up to 50% off", lang="en", path=tmp_path / "post.png")
    with Image.open(post) as img:
        assert img.size == (1080, 1080)
    icon = creative.render_icon_png("rocket", 128, path=tmp_path / "rocket.png")
    with Image.open(icon) as img:
        assert img.size == (128, 128)


def test_visual_order_reverses_hebrew_runs_only():
    order = creative.visual_order
    assert order("abc 123") == "abc 123"
    assert order("שלום") == "םולש"
    out = order("שלום 2026")
    assert "2026" in out and "םולש" in out


# ------------------------------------------------------------------------------------------ texts
@pytest.mark.parametrize("lang,topic", [("he", "חנות פרחים"), ("en", "flower shop")])
def test_copywriting(lang, topic):
    for fn in (texts.slogans, texts.taglines, texts.brainstorm, texts.names):
        out = fn(topic, lang=lang)
        assert out and all(isinstance(s, str) and s.strip() for s in out)
        assert out == fn(topic, lang=lang)
    assert texts.product_description("Rose bouquet", ["fresh", "local"], lang=lang)
    assert texts.social_post(topic, lang=lang, platform="instagram")
    mail = texts.email("sick day", recipient="Dana", lang=lang)
    assert "Dana" in mail
    copy = texts.landing_copy(topic, lang=lang)
    assert copy["headline"] and copy["features"]


# ------------------------------------------------------------------------------------------ animation & gallery
def test_animations():
    css = animation.css_animation("pulse", selector=".logo")
    assert "@keyframes" in css and ".logo" in css
    for kind in animation.LOADERS:
        _svg(animation.animated_loader_svg(kind=kind))
    animated = animation.animate_logo_svg(logo.generate_logos("Acme", count=1)[0].content, "draw")
    _svg(animated)
    _parses_as_html(animation.demo_page(lang="en"))
    with pytest.raises(ValueError):
        animation.css_animation("pulse", selector="x{}</style>")


def test_gallery(tmp_path):
    p = palette.generate_palette("bakery")
    assets = [*logo.generate_logos("Bake House", count=2, palette=p),
              Asset(name="notes", kind="md", content="# Notes"),
              Asset(name="palette", kind="json", content=p.to_json())]
    index = build_gallery(assets, tmp_path, "Bake House", "en", palette=p)
    page = index.read_text(encoding="utf-8")
    _parses_as_html(page)
    assert "Bake House" in page and (tmp_path / assets[2].filename).exists()


def test_public_api_is_importable_without_pillow_side_effects():
    for name in ("generate_palette", "generate_logos", "generate_mockup", "generate_presentation", "slogans",
                 "generate_icon_set", "generate_animations", "build_gallery"):
        assert callable(getattr(creative, name))
