"""Creative Agent: ideas, UI/UX, design, logos, icons, images, animations, texts and presentations.

Every visual comes from JARVIS's own design library through the `create_design` and
`create_presentation` tools, so it works offline and the files are real (SVG, PNG, PPTX, HTML).
With an AI provider the model writes the words (copy, slide content, ideas) and chooses what to
make; offline the built-in copywriting generators write them.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from jarvis.agents.base import AgentResult, AgentTask, BaseAgent, RunContext, skill, t
from jarvis.agents.common import dependency, slugify
from jarvis.common.enums import AgentId, AgentStatus, ResultStatus
from jarvis.common.errors import ProviderError, ProviderRefusal
from jarvis.core.intents import I
from jarvis.creative._util import keyword_in
from jarvis.creative.texts import clean_topic

DESIGN_ROUTES: list[tuple[str, tuple[str, ...]]] = [
    ("presentation", ("מצגת", "מצגות", "שקפים", "presentation", "slides", "slide deck", "deck", "powerpoint",
                      "פאוורפוינט")),
    ("improve", ("שפר", "תשפר", "לשפר", "שיפור", "improve", "redesign", "polish")),
    ("brand_kit", ("מיתוג", "ערכת מותג", "ערכת מיתוג", "זהות חזותית", "brand kit", "branding", "brand identity")),
    ("logo", ("לוגו", "סמליל", "logo", "emblem")),
    ("palette", ("פלטת צבעים", "פלטה", "צבעים", "palette", "color scheme", "colors", "colours")),
    ("icons", ("אייקונים", "אייקון", "איקונים", "icons", "icon set", "icon")),
    ("social_post", ("פוסט", "אינסטגרם", "פייסבוק", "post", "instagram", "facebook post", "social media")),
    ("banner", ("באנר", "כרזה", "פוסטר", "banner", "cover image", "poster", "header image")),
    ("animation", ("אנימציה", "אנימציות", "טעינה", "animation", "animations", "loader", "spinner")),
    ("mockup", ("מסך", "מסכים", "ממשק", "דשבורד", "לוח בקרה", "דף נחיתה", "ui", "ux", "mockup", "wireframe", "screen",
                "interface", "dashboard", "landing page", "app design")),
    ("names", ("שמות", "שם לעסק", "שם למותג", "שם לחברה", "names", "name ideas", "brand name")),
    ("slogans", ("סלוגן", "סלוגנים", "slogan", "tagline")),
    ("ideas", ("רעיונות", "רעיון", "קונספט", "קונספטים", "סיעור מוחות", "ideas", "brainstorm", "concepts")),
]
TEXT_ROUTES: list[tuple[str, tuple[str, ...]]] = [
    ("email", ("מייל", "אימייל", "מכתב", "email", "e-mail", "letter")),
    ("product", ("תיאור מוצר", "תיאור למוצר", "product description")),
    ("social_post", ("פוסט", "אינסטגרם", "פייסבוק", "לינקדאין", "טוויט", "post", "instagram", "facebook",
                     "linkedin", "tweet")),
    ("hashtags", ("האשטגים", "האשטג", "hashtags", "hashtag")),
    ("slogans", ("סלוגן", "סלוגנים", "slogan", "slogans", "tagline", "taglines")),
    ("names", ("שמות", "names")),
    ("ideas", ("רעיונות", "ideas", "brainstorm")),
    ("landing", ("טקסט לאתר", "טקסטים לאתר", "דף נחיתה", "landing page", "website copy", "copy")),
]
SCREEN_WORDS: list[tuple[str, tuple[str, ...]]] = [
    ("landing", ("דף נחיתה", "עמוד בית", "landing page", "home page", "homepage")),
    ("dashboard", ("דשבורד", "לוח בקרה", "dashboard", "admin")),
    ("login", ("התחברות", "כניסה", "הרשמה", "login", "sign in", "sign up")),
    ("product", ("עמוד מוצר", "product page")),
    ("settings", ("הגדרות", "settings")),
]
_NAMED = re.compile(r"(?:בשם|שנקרא|שנקראת|called|named)\s+[\"'“”״]?([^\"'“”״,.;\n]{2,40})", re.I)
_EMAIL_HE = re.compile(r"(?:מייל|אימייל|מכתב|הודעה)\s+(?:ל|אל\s+)(?P<to>[א-ת\"'׳]{2,20})"
                       r"(?:\s+(?:ש|על|לגבי|בנושא)\s*(?P<about>.+))?")
_EMAIL_EN = re.compile(r"(?:email|e-mail|letter|message)\s+to\s+(?P<to>[A-Za-z][\w'\-]{1,30})"
                       r"(?:\s+(?:about|that|saying|regarding|to say)\s+(?P<about>.+))?", re.I)
_PLATFORMS = (("linkedin", ("linkedin", "לינקדאין")), ("facebook", ("facebook", "פייסבוק")),
              ("x", ("twitter", "tweet", "טוויטר", "טוויט")), ("instagram", ("instagram", "אינסטגרם")))


def route(brief: str, routes: list[tuple[str, tuple[str, ...]]], default: str) -> str:
    for name, words in routes:
        if any(keyword_in(brief, w) for w in words):
            return name
    return default


def brand_name(brief: str) -> str:
    m = _NAMED.search(brief or "")
    return m.group(1).strip() if m else ""


def platform_of(brief: str) -> str:
    for name, words in _PLATFORMS:
        if any(keyword_in(brief, w) for w in words):
            return name
    return "instagram"


def parse_email(brief: str) -> tuple[str, str]:
    """(recipient, what it is about) from 'תכתוב מייל לדנה שאני חולה מחר' / 'email to Dana about the meeting'."""
    m = _EMAIL_HE.search(brief or "") or _EMAIL_EN.search(brief or "")
    if not m:
        return "", ""
    return m.group("to").strip(), (m.group("about") or "").strip(" .")


# ------------------------------------------------------------------------------------------ AI schemas
class SlideDraft(BaseModel):
    title: str
    layout: Literal["title", "section", "bullets", "two_column", "quote", "closing"]
    bullets: list[str] = Field(description="3-5 short points; for title/closing: subtitle lines; for quote: [quote, "
                                           "who said it]")
    notes: str = Field(description="What the speaker says on this slide (2-4 sentences)")
    columns: list[str] = Field(description="Two headings for a two_column slide, otherwise empty")


class DeckDraft(BaseModel):
    title: str
    slides: list[SlideDraft]


DECK_SYSTEM = """You are the Creative Agent of JARVIS. Write a clear, engaging presentation: a title slide, an
agenda, the content slides and a closing slide. Every content slide makes one point with short bullets (no
paragraphs) and speaker notes. Use the user's language. Facts must be accurate; when unsure, keep claims general."""

DESIGN_HINT = (
    "Make real files with create_design (brand_kit, logo, palette, icons, mockup, animation, banner, social_post) "
    "and create_presentation. Pick clear parameters from the request: the brand name, a fitting base_color, dark "
    "when asked, the language, and short, strong title/subtitle texts for images. When the user wants several "
    "things, pass the palette returned by the first call to the next calls so everything matches. For texts, write "
    "them yourself and save them under workspace/texts/ with write_file. Open the main result for the user with "
    "open_path (the gallery index.html or the presentation)."
)


# ------------------------------------------------------------------------------------------ the agent
class CreativeAgent(BaseAgent):
    id = AgentId.CREATIVE
    persona = (
        "Your job: ideas, UI/UX, visual design, logos, icons, images, animations, marketing texts and "
        "presentations. Produce finished, usable files, not descriptions of them. Fit the brand and the audience, "
        "keep designs accessible (readable contrast, clear hierarchy) and right-to-left correct for Hebrew. Write "
        "copy that is specific, short and natural in the user's language; never invent facts, prices or reviews "
        "about a real business."
    )
    llm_tools = ("create_design", "create_presentation", "write_file", "read_file", "list_dir", "find_files",
                 "file_info", "open_path", "fetch_url", "recall", "remember", "project_info")

    # ------------------------------------------------------------------ design
    @skill(description="Design: a brand kit, logos, palette, icons, UI mockups, banner, social post or animations",
           intents=(I.DESIGN,))
    async def design(self, task: AgentTask, run: RunContext, brief: str = "", kind: str = "", name: str = "",
                     open_result: bool = True) -> AgentResult:
        brief = (brief or task.instruction).strip()
        chosen = kind or route(brief, DESIGN_ROUTES, "brand_kit")
        if chosen == "presentation":
            return await self.presentation(task, run, topic=brief, open_result=open_result)
        if chosen in ("names", "slogans", "ideas"):
            return await self.ideas(task, run, topic=brief, kind=chosen)
        if task.allow_llm and self.ai_available:
            return await self.run_llm(task, run, extra_system=DESIGN_HINT)
        if chosen == "improve":
            await run.say(t(task.language, "שיפור עיצוב של קוד קיים צריך מודל AI; מכין במקום זה הצעת עיצוב: פלטה ומסכים.",
                            "Improving an existing design's code needs an AI model; making a proposal instead: a "
                            "palette and screens."), level="warning")
            result = await self._design_files(task, run, "mockup", brief, name, open_result)
            if result.ok:
                result.status = ResultStatus.PARTIAL
                result.next_step = t(task.language, "להוסיף מפתח AI כדי שאוכל לערוך את קבצי העיצוב עצמם.",
                                     "Add an AI key so I can edit the design files themselves.")
            return result
        return await self._design_files(task, run, chosen, brief, name, open_result)

    @skill(description="Brand kit for a project: palette, logos, favicon, icons, slogans and a banner")
    async def brand_kit(self, task: AgentTask, run: RunContext, brief: str = "", name: str = "",
                        open_result: bool = False) -> AgentResult:
        return await self._design_files(task, run, "brand_kit", (brief or task.instruction).strip(), name, open_result)

    async def _design_files(self, task: AgentTask, run: RunContext, kind: str, brief: str, name: str,
                            open_result: bool) -> AgentResult:
        lang = task.language
        params: dict[str, Any] = {"kind": kind, "brief": brief, "language": lang}
        name = name or brand_name(brief)
        if name:
            params["name"] = name
        earlier = dependency(task, agent=AgentId.CREATIVE.value) or {}
        palette = earlier["result"].get("palette") if isinstance(earlier.get("result"), dict) else None
        if isinstance(palette, dict) and palette.get("colors"):
            params["palette"] = palette
        if kind in ("banner", "social_post"):
            params["title"] = name or clean_topic(brief)[:60]
        if kind == "mockup":
            screens = [screen for screen, words in SCREEN_WORDS if any(keyword_in(brief, w) for w in words)]
            if screens:
                params["screens"] = screens + (["mobile"] if "mobile" not in screens else [])
        await run.progress(0.2, f"creating {kind}")
        res = await run.tool("create_design", **params)
        if not res.ok:
            return run.result(status=ResultStatus.FAILED, progress=0.0,
                              summary=t(lang, f"לא הצלחתי ליצור את העיצוב: {res.error}",
                                        f"Could not create the design: {res.error}"))
        data = dict(res.data or {})
        if open_result and data.get("gallery"):
            await run.tool("open_path", path=data["gallery"])
        summary = self._design_summary(kind, data, lang)
        return run.result(summary=summary, result=data, data=data,
                          details="\n".join(f"- {f}" for f in data.get("files", [])[:40]) or None)

    @staticmethod
    def _design_summary(kind: str, data: dict[str, Any], lang: str) -> str:
        files = data.get("files") or []
        name = data.get("name") or ""
        folder = data.get("folder") or ""
        colors = {c["role"]: c["hex"] for c in (data.get("palette") or {}).get("colors", [])}
        logos = sum(1 for f in files if "logo-" in str(f).replace("\\", "/").rsplit("/", 1)[-1])
        icons = sum(1 for f in files if "icon-" in str(f).replace("\\", "/").rsplit("/", 1)[-1])
        he = {
            "brand_kit": f"הכנתי ערכת מיתוג ל'{name}': פלטת צבעים, {logos} לוגואים, {icons} אייקונים, סלוגנים"
                         + (" ובאנר" if any(str(f).endswith(".png") for f in files) else "") + ".",
            "logo": f"עיצבתי {logos} קונספטים ללוגו של '{name}'.",
            "palette": f"פלטת צבעים: ראשי {colors.get('primary', '')}, משני {colors.get('secondary', '')}, "
                       f"הדגשה {colors.get('accent', '')}.",
            "icons": f"עיצבתי {icons} אייקונים בסגנון אחיד.",
            "mockup": f"עיצבתי {sum(1 for f in files if str(f).endswith('.html')) - 1} מסכים (HTML) ושרטוטים.",
            "animation": "הכנתי ערכת אנימציות: CSS, אנימציות טעינה ועמוד הדגמה.",
            "banner": "עיצבתי באנר בגודל 1200×630.",
            "social_post": "עיצבתי פוסט בגודל 1080×1080.",
        }
        en = {
            "brand_kit": f"Made a brand kit for '{name}': palette, {logos} logos, {icons} icons, slogans"
                         + (" and a banner" if any(str(f).endswith(".png") for f in files) else "") + ".",
            "logo": f"Designed {logos} logo concepts for '{name}'.",
            "palette": f"Palette: primary {colors.get('primary', '')}, secondary {colors.get('secondary', '')}, "
                       f"accent {colors.get('accent', '')}.",
            "icons": f"Designed a matching set of {icons} icons.",
            "mockup": f"Designed {sum(1 for f in files if str(f).endswith('.html')) - 1} screens (HTML) with wireframes.",
            "animation": "Made an animation kit: CSS utilities, loaders and a demo page.",
            "banner": "Designed a 1200×630 banner.",
            "social_post": "Designed a 1080×1080 post.",
        }
        text = (he if lang == "he" else en).get(kind, "")
        return text + t(lang, f" הקבצים ב-{folder}", f" Files are in {folder}")

    # ------------------------------------------------------------------ presentations
    @skill(description="Create a presentation (PowerPoint + HTML deck) about a topic")
    async def presentation(self, task: AgentTask, run: RunContext, topic: str = "", n_slides: int = 8,
                           audience: str = "", open_result: bool = True) -> AgentResult:
        lang = task.language
        topic = clean_topic((topic or task.instruction).strip()) or task.instruction
        params: dict[str, Any] = {"topic": topic, "language": lang, "n_slides": max(3, min(int(n_slides), 14)),
                                  "audience": audience}
        used_llm = False
        if task.allow_llm and self.ai_available and self.runtime.router is not None:
            await run.phase(AgentStatus.THINKING)
            prompt = (f"Topic: {topic}\nAudience: {audience or 'general'}\nAbout {params['n_slides']} slides.\n"
                      f"Language: {'Hebrew' if lang == 'he' else 'English'}.\nRequest: {task.instruction}")
            deps = task.context.get("dependencies") or []
            if deps:
                prompt += "\n\nMaterial from the other agents:\n" + "\n".join(str(d)[:3000] for d in deps)
            try:
                deck = await run.guard(self.runtime.router.structured("agent", prompt, DeckDraft, system=DECK_SYSTEM))
                params["title"] = deck.title
                params["slides"] = [s.model_dump() for s in deck.slides if s.title.strip()][:20]
                used_llm = bool(params["slides"])
            except ProviderRefusal:
                raise
            except ProviderError as exc:
                await run.say(t(lang, f"מודל ה-AI לא זמין ({exc.message}); כותב את המצגת מתבנית.",
                                f"The AI model is unavailable ({exc.message}); writing the deck from a template."),
                              level="warning")
        await run.progress(0.5, "building the deck")
        res = await run.tool("create_presentation", **params)
        if not res.ok:
            return run.result(status=ResultStatus.FAILED, progress=0.0,
                              summary=t(lang, f"לא הצלחתי ליצור את המצגת: {res.error}",
                                        f"Could not create the presentation: {res.error}"))
        data = dict(res.data or {})
        if open_result and data.get("html"):
            await run.tool("open_path", path=data["html"])
        return run.result(
            summary=t(lang, f"הכנתי מצגת של {data.get('slides')} שקפים: PowerPoint ({data.get('pptx')}) וגרסת HTML.",
                      f"Made a {data.get('slides')}-slide presentation: PowerPoint ({data.get('pptx')}) and an HTML "
                      "version."),
            result=data, data=data, used_llm=used_llm)

    # ------------------------------------------------------------------ words
    @skill(description="Ideas, brand names or slogans for a topic")
    async def ideas(self, task: AgentTask, run: RunContext, topic: str = "", kind: str = "ideas",
                    n: int = 10) -> AgentResult:
        from jarvis.creative import texts

        lang = task.language
        topic = (topic or task.instruction).strip()
        if task.allow_llm and self.ai_available:
            return await self.run_llm(task, run, extra_system="Answer with a numbered list in your report (no files "
                                                              "needed unless asked).")
        subject = clean_topic(topic)
        if kind == "names":
            items = texts.names(subject, lang=lang, n=n)
            head = t(lang, f"רעיונות לשמות עבור {subject}:", f"Name ideas for {subject}:")
        elif kind == "slogans":
            items = texts.slogans(subject, lang=lang, n=min(n, 8), name=brand_name(topic) or None)
            head = t(lang, f"סלוגנים עבור {subject}:", f"Slogans for {subject}:")
        else:
            items = texts.brainstorm(subject, lang=lang, n=n)
            head = t(lang, f"רעיונות עבור {subject}:", f"Ideas for {subject}:")
        body = "\n".join(f"{i}. {item}" for i, item in enumerate(items, 1))
        return run.result(summary=f"{head} {items[0]}" if items else head, details=f"{head}\n{body}",
                          result={"kind": kind, "items": items})

    @skill(description="Write a text: email, social post, product description, website copy, slogans",
           intents=(I.WRITE_TEXT,))
    async def write_text(self, task: AgentTask, run: RunContext, brief: str = "", kind: str = "",
                         recipient: str = "", save: bool = True) -> AgentResult:
        from jarvis.creative import texts

        lang = task.language
        brief = (brief or task.instruction).strip()
        if task.allow_llm and self.ai_available:
            return await self.run_llm(task, run, extra_system=(
                "Write the text yourself, ready to use. Unless the user asked otherwise, save it with write_file as "
                "a .md file under workspace/texts/, and put the full text in your report. Do not send anything: "
                "JARVIS only drafts messages."))
        chosen = kind or route(brief, TEXT_ROUTES, "landing")
        if chosen in ("names", "slogans", "ideas"):
            return await self.ideas(task, run, topic=brief, kind=chosen)
        subject = clean_topic(brief)
        if chosen == "email":
            to, about = parse_email(brief)
            draft = texts.email_draft(about or subject, recipient=recipient or to, details=about, lang=lang)
            title, text = draft.subject, draft.text
        elif chosen == "product":
            title = subject
            text = texts.product_description(subject, lang=lang)
        elif chosen == "social_post":
            platform = platform_of(brief)
            title = f"{platform} — {subject}"
            text = texts.social_post(subject, lang=lang, platform=platform)
        elif chosen == "hashtags":
            title = subject
            text = " ".join(texts.hashtags(subject, lang=lang, n=12))
        else:
            copy = texts.landing_copy(subject, lang=lang, name=brand_name(brief) or None)
            title = str(copy.get("name") or subject)
            lines = [f"# {copy.get('headline', '')}", "", str(copy.get("subheadline", "")), "",
                     f"**{copy.get('cta', '')}**", ""]
            for feature in copy.get("features") or []:  # type: ignore[union-attr]
                lines += [f"## {feature['title']}", str(feature["text"]), ""]
            if copy.get("about"):
                lines += ["## " + t(lang, "אודות", "About"), str(copy["about"]), ""]
            text = "\n".join(lines).strip() + "\n"
        saved = None
        if save:
            res = await run.tool("write_file", content=text if text.endswith("\n") else text + "\n",
                                 path=f"workspace/texts/{datetime.now():%Y%m%d-%H%M}-{slugify(title, 40, 'text')}.md")
            if res.ok:
                saved = str((res.data or {}).get("path"))
        first = text.strip().splitlines()[0] if text.strip() else ""
        return run.result(
            status=ResultStatus.SUCCESS if saved or not save else ResultStatus.PARTIAL,
            summary=t(lang, f"כתבתי את הטקסט{f' ושמרתי ב-{saved}' if saved else ''}: {first[:160]}",
                      f"Wrote the text{f' and saved it to {saved}' if saved else ''}: {first[:160]}"),
            details=text, result={"kind": chosen, "title": title, "text": text, "path": saved})

    # ------------------------------------------------------------------ offline routing
    def pick_skill(self, task: AgentTask) -> tuple[str, dict[str, Any]] | None:
        picked = super().pick_skill(task)
        if picked is not None:
            return picked
        text = task.instruction
        chosen = route(text, DESIGN_ROUTES, "")
        if chosen:
            return "design", {"brief": text, **task.params}
        if route(text, TEXT_ROUTES, ""):
            return "write_text", {"brief": text, **task.params}
        return "design", {"brief": text, **task.params}


__all__ = ["CreativeAgent", "parse_email", "route"]
