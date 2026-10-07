"""Offline copywriting in Hebrew and English: slogans, taglines, product descriptions, social
posts, emails, brainstorms and brand names.

Everything is template based but chosen per domain (coffee shop, clinic, SaaS…) and shuffled
with a seed derived from the input, so the same request always gives the same text while
different topics read differently. Hebrew templates avoid gendered verb forms wherever
possible (first person, plural address, impersonal constructions).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ._util import has_hebrew, keyword_in, make_rng, norm, resolve_lang
from .domains import GENERIC, Domain, detect_domain

TONES = ("friendly", "formal", "luxury", "playful", "minimal")
PLATFORMS = ("instagram", "facebook", "linkedin", "x")

# --------------------------------------------------------------------------------------------
# Topic handling
# --------------------------------------------------------------------------------------------

_EN_REQUEST = re.compile(
    r"^(?:(?:hey\s+)?jarvis[,:]?\s+)?(?:please\s+)?(?:can you\s+|could you\s+)?"
    r"(?:write|create|make|generate|give|come up with|suggest|brainstorm|draft|need|want)\s+(?:me\s+|us\s+)?"
    r"(?:some\s+|a few\s+|a\s+|an\s+|\d+\s+)?(?:catchy\s+|short\s+|good\s+|new\s+)?"
    r"(?:slogans?|taglines?|names?|brand names?|ideas?|posts?|descriptions?|emails?|copy|texts?|headlines?)?\s*"
    r"(?:for|about|on|regarding)?\s*(?:a|an|the|my|our)?\s*",
    re.I,
)
_HE_REQUEST = re.compile(
    r"^(?:ג'?ארוויס[,:]?\s+)?(?:בבקשה\s+)?(?:תכתוב|כתוב|תכתבי|כתבי|צור|תצור|צרי|תצרי|תן|תני|תביא|תביאי|הבא|תציע|הצע|"
    r"תציעי|תמציא|תמציאי|תחשוב על|תחשבי על|אני צריך|אני צריכה|אני רוצה)\s+(?:לי|לנו)?\s*(?:כמה\s+|\d+\s+)?"
    r"(?:סלוגנים|סלוגן|שמות|שם|רעיונות|רעיון|פוסטים|פוסט|תיאור|מייל|טקסט|כותרות|כותרת)?\s*"
)


def clean_topic(topic: str) -> str:
    """Strip request phrasing: 'write me slogans for a coffee shop' -> 'coffee shop',
    'תכתוב לי סלוגנים לבית קפה' -> 'בית קפה'."""
    t = (topic or "").strip().strip(".!?")
    before = t
    t = _EN_REQUEST.sub("", t, count=1) if re.match(r"[A-Za-z]", t) else t
    m = _HE_REQUEST.match(t)
    if m:
        rest = t[m.end():]
        if m.group(0).strip() and rest[:1] in ("ל", "ב") and len(rest) > 3 and m.group(0).rstrip().endswith(
            ("סלוגנים", "סלוגן", "שמות", "שם", "רעיונות", "רעיון", "פוסטים", "פוסט", "תיאור", "מייל", "טקסט", "כותרות",
             "כותרת")
        ):
            rest = rest[1:]
        t = rest
    for prefix in ("על ", "עבור ", "בשביל ", "בנושא "):
        if t.startswith(prefix):
            t = t[len(prefix):]
    t = t.strip(" ,.:;-")
    return t or before


_MEDIUM_EN = re.compile(
    r"^(?:(?:a|an|the|my|our)\s+)?(?:new\s+)?(?:website|web\s?site|site|landing\s+page|web\s?app|mobile\s+app|app|"
    r"application|online\s+store|online\s+shop|e-?shop|blog)\s+(?:for|of|about)\s+(?:(?:a|an|the|my|our)\s+)?",
    re.I,
)
_MEDIUM_EN_SUFFIX = re.compile(r"\s+(?:website|web\s?site|site|landing\s+page|web\s?app|blog)$", re.I)
_MEDIUM_HE = re.compile(
    r"^(?:אתר|אתר אינטרנט|דף נחיתה|אפליקציה|אפליקציית|בלוג|חנות אונליין|חנות אינטרנטית)\s+"
    r"(?:של\s+|עבור\s+|בשביל\s+|ל(?=[א-ת]))"
)


def topic_subject(topic: str) -> str:
    """What a brief is about, without the medium: 'website for a coffee shop' -> 'coffee shop',
    'אתר לבית קפה' -> 'בית קפה', 'fitness app' stays 'fitness app'."""
    t = clean_topic(topic)
    stripped = _MEDIUM_HE.sub("", t, count=1) if has_hebrew(t) else _MEDIUM_EN_SUFFIX.sub("", _MEDIUM_EN.sub("", t, count=1))
    return stripped.strip() or t


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


@dataclass
class _Copy:
    """Domain vocabulary resolved for one topic and language."""

    domain: Domain
    lang: str
    topic: str
    essence: str
    offer: str
    label: str
    audience: tuple[str, ...]
    features: list[tuple[str, str, str]]  # (icon, title, text)


def _copy(topic: str, lang: str) -> _Copy:
    topic = topic_subject(topic)
    d = detect_domain(topic)
    if d is GENERIC and topic:
        short = topic if len(topic.split()) <= 5 else " ".join(topic.split()[:5])
        essence = offer = short if lang == "he" else short.lower()
    else:
        essence, offer = d.t("essence", lang), d.t("offer", lang)
    feats = [(f.icon, f.title[lang], f.text[lang]) for f in d.features]
    return _Copy(d, lang, topic, essence, offer, d.t("label", lang), d.audience[lang], feats)


def _fill(template: str, **kw: str) -> str:
    out = template
    for k, v in kw.items():
        out = out.replace("{" + k + "}", v)
        out = out.replace("{" + k.capitalize() + "}", _cap(v))
    return re.sub(r"\s+([,.!?:])", r"\1", out).strip()


def _pick(rng_parts: tuple[object, ...], templates: list[str], n: int, seed: int | None = None) -> list[str]:
    rng = make_rng(*rng_parts, seed=seed)
    items = list(dict.fromkeys(templates))
    rng.shuffle(items)
    return items[: max(0, n)]


# --------------------------------------------------------------------------------------------
# Slogans and taglines
# --------------------------------------------------------------------------------------------

_SLOGANS = {
    "he": [
        "הכול מתחיל ב{essence}.", "{Offer}, בלי פשרות.", "המקום שלכם ל{essence}.", "כי מגיע לכם {essence}.",
        "{essence} ברמה אחרת.", "הבחירה הטבעית ל{essence}.", "נולדנו בשביל {essence}.", "בשביל רגעים של {essence}.",
        "לא סתם {essence} – {offer}.", "{Offer}. ככה זה צריך להיות.", "יש {essence}, ויש אותנו.",
        "{essence}? אצלנו זה הסטנדרט.", "{Offer} – בדיוק כמו שצריך.",
    ],
    "en": [
        "Everything starts with {essence}.", "{Offer}, no compromises.", "Your place for {essence}.",
        "Because you deserve {essence}.", "{Essence}, taken to the next level.", "The natural choice for {essence}.",
        "Born for {essence}.", "Made for moments of {essence}.", "Not just {essence}. {Offer}.",
        "{Offer}. Just as it should be.", "There's {essence}, and then there's us.", "{Essence}? That's our standard.",
        "{Offer}, done right.",
    ],
}
_SLOGANS_NAMED = {
    "he": ["{name} – כי מגיע לכם {essence}.", "עם {name}, {essence} זה פשוט.", "כשזה מגיע ל{essence}, יש רק {name}.",
           "{name}. {offer}.", "{name}: {essence}, בגובה העיניים.", "פשוט {essence}. פשוט {name}."],
    "en": ["{name}: because you deserve {essence}.", "With {name}, {essence} is simple.",
           "When it comes to {essence}, there's only {name}.", "{name}. {Offer}.", "{name}: {essence}, made personal.",
           "Simply {essence}. Simply {name}."],
}


def slogans(topic: str, *, lang: str | None = None, n: int = 6, name: str | None = None,
            seed: int | None = None) -> list[str]:
    """Short, punchy brand slogans for a topic (Hebrew or English)."""
    lang = resolve_lang(lang, topic)
    c = _copy(topic, lang)
    templates = list(_SLOGANS[lang])
    if name:
        templates = _SLOGANS_NAMED[lang] + templates
    chosen = _pick(("slogans", c.topic, lang, name or ""), templates, len(templates), seed)
    if name:  # make sure the brand appears early
        named = [t for t in chosen if "{name}" in t]
        chosen = named[:2] + [t for t in chosen if t not in named[:2]]
    out = [_fill(t, essence=c.essence, offer=c.offer, name=name or "") for t in chosen]
    return list(dict.fromkeys(out))[: max(0, n)]


_TAGLINES = {
    "he": ["{offer} ל{aud}", "{essence}, {f1} ו{f2}", "{offer}. {f1}. {f2}.", "{f1} ו{f2} – הכול במקום אחד",
           "{offer} – {ftext}", "{label} ל{aud} ול{aud2}"],
    "en": ["{Offer} for {aud}.", "{Essence}: {f1} and {f2}.", "{Offer}. {F1}. {F2}.", "{F1} and {f2}, all in one place.",
           "{Offer}: {ftext}.", "A {label} for {aud} and {aud2}."],
}


def taglines(topic: str, *, lang: str | None = None, n: int = 6, seed: int | None = None) -> list[str]:
    """Descriptive one-line taglines (what you offer, for whom, why)."""
    lang = resolve_lang(lang, topic)
    c = _copy(topic, lang)
    rng = make_rng("taglines", c.topic, lang, seed=seed)
    feats = c.features[:]
    rng.shuffle(feats)
    aud = list(c.audience)
    rng.shuffle(aud)
    f1, f2 = feats[0][1], feats[1][1]
    if lang == "en":
        f1, f2 = f1[0].lower() + f1[1:], f2[0].lower() + f2[1:]
    ftext = feats[2][2] if lang == "he" else feats[2][2][0].lower() + feats[2][2][1:]
    label = c.label
    out = [_fill(t, offer=c.offer, essence=c.essence, aud=aud[0], aud2=aud[1], f1=f1, f2=f2, ftext=ftext, label=label)
           for t in _TAGLINES[lang]]
    rng.shuffle(out)
    return list(dict.fromkeys(out))[: max(0, n)]


# --------------------------------------------------------------------------------------------
# Product description
# --------------------------------------------------------------------------------------------

_PD = {
    "he": {
        "friendly": (["הכירו את {name} – {essence} בגרסה הכי נוחה שיש.", "עם {name} מקבלים בדיוק את מה שצריך, בלי סיבוכים.",
                      "{name} – כי גם הדברים הקטנים צריכים להיות מושלמים."],
                     "מה מקבלים?", ["נסו עוד היום ותבינו למה כולם מדברים על זה.", "רק לנסות, ואין דרך חזרה 😉"]),
        "formal": (["{name}: פתרון מקצועי, אמין ומדויק.", "המוצר {name} פותח מתוך הבנה עמוקה של צורכי הלקוחות.",
                    "בעזרת {name} תוכלו ליהנות מתוצאות עקביות ומדידות."],
                   "יתרונות מרכזיים:", ["לפרטים נוספים ולהזמנה, צרו איתנו קשר.", "נשמח לעמוד לרשותכם בכל שאלה."]),
        "luxury": (["{name}. יצירה שנולדה מתוך תשוקה לפרטים הקטנים.", "{name} – אלגנטיות שקטה ואיכות בלתי מתפשרת."],
                   "הפרטים שעושים את ההבדל:", ["זמין במהדורה מוגבלת, כי מגיע לכם את הטוב ביותר.",
                                               "בהזמנה אישית ובאריזת מתנה מהודרת."]),
        "playful": (["רגע, עוד לא הכרתם את {name}?! 🎉", "{name} כאן כדי להפוך כל יום לקצת יותר כיפי."],
                    "למה תתאהבו?", ["אז למה לחכות? קדימה, לעגלה! 🛒", "ספוילר: אחד לא יספיק לכם."]),
        "minimal": (["{name}. בלי רעש, רק מה שחשוב."], "בקצרה:", ["זמין עכשיו."]),
    },
    "en": {
        "friendly": (["Meet {name}: {essence}, made effortless.", "With {name}, you get exactly what you need, minus the hassle.",
                      "{name}, because the little things should be perfect too."],
                     "What you get:", ["Try it today and see what everyone's talking about.",
                                       "Give it a try. There's no going back 😉"]),
        "formal": (["{name} is a professional, reliable and precise solution.",
                    "{name} was designed around a deep understanding of our customers' needs.",
                    "With {name}, you can expect consistent, measurable results."],
                   "Key benefits:", ["For more details or to place an order, please contact us.",
                                     "We would be happy to answer any questions."]),
        "luxury": (["{name}. Crafted with a passion for the smallest details.",
                    "{name}: quiet elegance and uncompromising quality."],
                   "The details that make the difference:", ["Available in a limited edition, because you deserve the very best.",
                                                             "Made to order and presented in signature gift packaging."]),
        "playful": (["Wait, you haven't met {name} yet?! 🎉", "{name} is here to make every day a little more fun."],
                    "Why you'll love it:", ["So why wait? Add it to your cart! 🛒", "Spoiler: one won't be enough."]),
        "minimal": (["{name}. Nothing extra, just what matters."], "In short:", ["Available now."]),
    },
}


def product_description(name: str, features: list[str] | None = None, *, lang: str | None = None,
                        tone: str = "friendly", seed: int | None = None) -> str:
    """A ready-to-paste product description: opener, feature bullets and a closing line."""
    lang = resolve_lang(lang, f"{name} {' '.join(features or [])}")
    tone = tone if tone in TONES else ("formal" if tone in ("professional", "business") else "friendly")
    c = _copy(f"{name} {' '.join(features or [])}", lang)
    feats = [f.strip() for f in (features or []) if f and f.strip()]
    if not feats:
        feats = [f"{t} – {x}" if lang == "he" else f"{t}: {x}" for _, t, x in c.features[:3]]
    openers, intro, closers = _PD[lang][tone]
    rng = make_rng("product", name, tuple(feats), lang, tone, seed=seed)
    opener = _fill(rng.choice(openers), name=name, essence=c.essence)
    bullets = "\n".join(f"• {f.rstrip('.')}" for f in feats)
    return f"{opener}\n\n{intro}\n{bullets}\n\n{rng.choice(closers)}"


# --------------------------------------------------------------------------------------------
# Social posts
# --------------------------------------------------------------------------------------------

_HOOKS = {
    "he": ["מחפשים {essence}? הגעתם למקום הנכון.", "הכול מתחיל ב{essence}.", "{Offer} – ועכשיו זה גם אצלכם.",
           "יש לנו חדשות טובות לכל מי שאוהב {essence}."],
    "en": ["Looking for {essence}? You're in the right place.", "Everything starts with {essence}.",
           "{Offer}, now closer than ever.", "Good news for everyone who loves {essence}."],
}
_CTA = {
    "instagram": {"he": ["הלינק בביו 👆", "שלחו לנו הודעה ונשמח לעזור 💬", "תייגו מישהו שחייב לראות את זה 👇"],
                  "en": ["Link in bio 👆", "Send us a DM and we'll help 💬", "Tag someone who needs to see this 👇"]},
    "facebook": {"he": ["מה דעתכם? ספרו לנו בתגובות 👇", "לחצו על הקישור ותגלו עוד.", "שתפו עם מי שזה יכול לעזור לו."],
                 "en": ["What do you think? Tell us in the comments 👇", "Click the link to learn more.",
                        "Share this with someone it could help."]},
    "linkedin": {"he": ["אשמח לשמוע: מה הכי חשוב לכם כשאתם בוחרים {label}?", "מוזמנים לכתוב לי בפרטי לשיחה קצרה."],
                 "en": ["I'd love to hear: what matters most to you when choosing a {label}?",
                        "Feel free to message me for a quick chat."]},
    "x": {"he": ["מה אתם אומרים?", "פרטים בלינק."], "en": ["Thoughts?", "Details in the link."]},
}


def hashtags(topic: str, *, lang: str | None = None, n: int = 6) -> list[str]:
    """Hashtags from the topic and its domain (#בית_קפה, #coffee…)."""
    lang = resolve_lang(lang, topic)
    c = _copy(topic, lang)
    words: list[str] = []
    if c.topic and len(c.topic.split()) <= 3:
        words.append(c.topic)
    words.append(c.label)
    if lang == "he":
        words += [k for k in c.domain.keywords if re.search(r"[֐-׿]", k)][:4]
        words += ["המלצה", "חדש"]
    else:
        words += [k for k in c.domain.keywords if re.match(r"[a-z]", k)][:4]
        words += ["smallbusiness", "new"]
    tags: list[str] = []
    for w in words:
        w = re.sub(r"[^\w֐-׿ ]+", "", w).strip()
        if not w:
            continue
        tag = "#" + (w.replace(" ", "_") if lang == "he" else w.replace(" ", "").lower())
        if tag not in tags:
            tags.append(tag)
    return tags[: max(0, n)]


def social_post(topic: str, *, lang: str | None = None, platform: str = "instagram", hashtags: bool = True,
                seed: int | None = None) -> str:
    """A platform-shaped social post (instagram, facebook, linkedin or x)."""
    lang = resolve_lang(lang, topic)
    platform = platform.lower().replace("twitter", "x")
    if platform not in PLATFORMS:
        platform = "instagram"
    c = _copy(topic, lang)
    rng = make_rng("post", c.topic, lang, platform, seed=seed)
    hook = _fill(rng.choice(_HOOKS[lang]), essence=c.essence, offer=c.offer)
    feats = c.features[:]
    rng.shuffle(feats)
    cta = _fill(rng.choice(_CTA[platform][lang]), label=c.label)
    tags = _hashtags_for(topic, lang) if hashtags else []
    emoji = c.domain.emoji
    sep = " – " if lang == "he" else ": "
    if platform == "instagram":
        lines = [f"{emoji} {hook}", "", *(f"✔️ {t}{sep}{x}" for _, t, x in feats[:3]), "", cta]
        if tags:
            lines += ["", " ".join(tags[:8])]
        return "\n".join(lines)
    if platform == "facebook":
        body = (f"{hook} {feats[0][2]}, {feats[1][2][0].lower() + feats[1][2][1:] if lang == 'en' else feats[1][2]}."
                if lang == "en" else f"{hook} {feats[0][2]}, ו{feats[1][2]}.")
        lines = [body, "", cta]
        if tags:
            lines += ["", " ".join(tags[:3])]
        return "\n".join(lines)
    if platform == "linkedin":
        intro = {"he": f"{hook}\n\nבשנים האחרונות למדנו ש{c.audience[0]} מחפשים יותר מסתם {c.essence}. "
                       f"הנה שלושה דברים שעושים את ההבדל:",
                 "en": f"{hook}\n\nOver the years we've learned that {c.audience[0]} want more than just {c.essence}. "
                       f"Here are three things that make the difference:"}[lang]
        bullets = [f"{i}. {t}{sep}{x}" for i, (_, t, x) in enumerate(feats[:3], 1)]
        lines = [intro, "", *bullets, "", cta]
        if tags:
            lines += ["", " ".join(tags[:4])]
        return "\n".join(lines)
    # x / twitter: short, at most 280 characters
    text = f"{emoji} {hook} {feats[0][1]}{sep}{feats[0][2]}. {cta}"
    if tags:
        text += " " + " ".join(tags[:2])
    if len(text) > 280:
        text = text[:277].rsplit(" ", 1)[0] + "…"
    return text


def _hashtags_for(topic: str, lang: str) -> list[str]:
    return hashtags(topic, lang=lang, n=8)


# --------------------------------------------------------------------------------------------
# Emails
# --------------------------------------------------------------------------------------------

@dataclass
class EmailDraft:
    subject: str
    body: str
    lang: str
    kind: str

    @property
    def text(self) -> str:
        label = "נושא" if self.lang == "he" else "Subject"
        return f"{label}: {self.subject}\n\n{self.body}"

    def __str__(self) -> str:
        return self.text


_EMAIL_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("sick", ("sick", "ill", "illness", "flu", "מחלה", "חולה", "יום מחלה", "לא מרגיש", "לא מרגישה", "חום")),
    ("resignation", ("resign", "resignation", "quit", "התפטרות", "להתפטר", "מתפטר", "מתפטרת")),
    ("vacation", ("vacation", "day off", "time off", "leave request", "holiday", "חופשה", "יום חופש", "ימי חופש")),
    ("late", ("late", "running late", "delay", "איחור", "מאחר", "מאחרת", "אאחר")),
    ("meeting", ("meeting", "schedule a call", "call", "פגישה", "לקבוע פגישה", "שיחה", "זום", "zoom")),
    ("follow_up", ("follow up", "follow-up", "following up", "checking in", "מעקב", "בהמשך ל", "המשך")),
    ("complaint", ("complaint", "complain", "refund", "problem with", "תלונה", "להתלונן", "החזר כספי", "בעיה")),
    ("thanks", ("thank", "thanks", "gratitude", "תודה", "להודות", "הוקרה")),
    ("apology", ("apology", "apologize", "apologise", "sorry", "התנצלות", "להתנצל", "סליחה")),
    ("job", ("job application", "apply", "application", "position", "cv", "resume", "מועמדות", "משרה", "קורות חיים")),
    ("invitation", ("invite", "invitation", "הזמנה לאירוע", "להזמין", "הזמנה ל")),
    ("reminder", ("reminder", "remind", "תזכורת", "להזכיר")),
    ("introduction", ("introduce", "introduction", "היכרות", "להציג את עצמי")),
    ("quote", ("quote", "quotation", "price offer", "הצעת מחיר")),
    ("cancellation", ("cancel", "cancellation", "ביטול", "לבטל")),
)

# kind -> lang -> (subject, subject_with_details, [body lines], details clause)
_EMAILS: dict[str, dict[str, tuple[str, str, list[str], str]]] = {
    "sick": {
        "he": ("היעדרות {day} עקב מחלה", "היעדרות {day} עקב מחלה", [
            "לצערי אני חולה ולא אוכל להגיע לעבודה {day}.",
            "{details}",
            "אני מקווה לחזור לשגרה {back}, ואעדכן אם יהיה שינוי.",
            "במקרים דחופים אפשר להשיג אותי בטלפון, ואשתדל לענות בהקדם.",
            "תודה על ההבנה.",
        ], ""),
        "en": ("Sick day {day}", "Sick day {day}", [
            "Unfortunately I'm unwell and won't be able to come in to work {day}.",
            "{details}",
            "I hope to be back {back} and will let you know if anything changes.",
            "For anything urgent I can be reached by phone, and I'll reply as soon as I can.",
            "Thank you for understanding.",
        ], ""),
    },
    "meeting": {
        "he": ("בקשה לתיאום פגישה", "בקשה לתיאום פגישה בנושא {details}", [
            "אשמח לתאם פגישה{clause} בימים הקרובים.",
            "הזמנים שלי גמישים, ואשמח להתאים את עצמי ללוח הזמנים שלך.",
            "אפשר להיפגש במשרד או בשיחת וידאו, מה שנוח לך.",
            "אשמח לשמוע אילו מועדים מתאימים לך.",
        ], " בנושא {details}"),
        "en": ("Meeting request", "Meeting request: {details}", [
            "I'd like to schedule a meeting{clause} in the coming days.",
            "My schedule is flexible, and I'm happy to work around yours.",
            "We can meet at the office or over a video call, whichever suits you best.",
            "Please let me know which times work for you.",
        ], " regarding {details}"),
    },
    "follow_up": {
        "he": ("המשך לשיחתנו", "המשך לשיחתנו בנושא {details}", [
            "רציתי לעקוב אחר השיחה האחרונה שלנו{clause} ולבדוק אם יש עדכונים.",
            "אם נדרש ממני מידע נוסף, אשמח לשלוח אותו.",
            "אשמח לשמוע ממך בהקדם.",
        ], " בנושא {details}"),
        "en": ("Following up", "Following up: {details}", [
            "I wanted to follow up on our last conversation{clause} and see if there are any updates.",
            "If you need any further information from me, I'll gladly send it over.",
            "Looking forward to hearing from you.",
        ], " about {details}"),
    },
    "complaint": {
        "he": ("פנייה בנושא תקלה בשירות", "פנייה בנושא {details}", [
            "אני פונה אליכם בנוגע לבעיה שחוויתי{clause}.",
            "לצערי, השירות לא עמד בציפיות ובהתחייבות שניתנה לי.",
            "אבקש לבדוק את העניין ולעדכן אותי על הפתרון המוצע בתוך 7 ימי עסקים.",
            "אשמח להגיע לפתרון הוגן ומהיר.",
        ], " בנושא {details}"),
        "en": ("Complaint about your service", "Complaint: {details}", [
            "I'm writing about a problem I experienced{clause}.",
            "Unfortunately, the service did not meet the expectations and commitments I was given.",
            "Please look into this and let me know the proposed resolution within 7 business days.",
            "I'd appreciate a fair and prompt resolution.",
        ], " with {details}"),
    },
    "thanks": {
        "he": ("תודה רבה", "תודה על {details}", [
            "רציתי להודות לך מכל הלב{clause}.",
            "ההשקעה והמקצועיות שלך עשו את ההבדל, וזה ממש לא מובן מאליו.",
            "מקווה שנמשיך לעבוד יחד גם בעתיד.",
        ], " על {details}"),
        "en": ("Thank you", "Thank you for {details}", [
            "I wanted to sincerely thank you{clause}.",
            "Your effort and professionalism made a real difference, and it doesn't go unnoticed.",
            "I hope we get to keep working together.",
        ], " for {details}"),
    },
    "apology": {
        "he": ("התנצלות", "התנצלות על {details}", [
            "אני רוצה להתנצל{clause}.",
            "האחריות היא שלי, ואני עושה הכול כדי שזה לא יחזור על עצמו.",
            "אשמח לדבר על זה ולמצוא יחד דרך לתקן.",
        ], " על {details}"),
        "en": ("My apologies", "Apologies for {details}", [
            "I want to apologize{clause}.",
            "I take full responsibility, and I'm doing everything I can to make sure it doesn't happen again.",
            "I'd be glad to talk it through and find a way to make it right.",
        ], " for {details}"),
    },
    "vacation": {
        "he": ("בקשת חופשה", "בקשת חופשה ({details})", [
            "אבקש לאשר לי חופשה{clause}.",
            "לפני היציאה אדאג להשלים את המשימות הדחופות ולהעביר את מה שנדרש.",
            "במקרה הצורך אפשר להשיג אותי בטלפון.",
            "תודה מראש.",
        ], " ({details})"),
        "en": ("Time-off request", "Time-off request ({details})", [
            "I'd like to request time off{clause}.",
            "Before I leave I'll wrap up anything urgent and hand over what's needed.",
            "If necessary, I can be reached by phone.",
            "Thank you in advance.",
        ], " ({details})"),
    },
    "late": {
        "he": ("איחור קל", "איחור קל", [
            "רציתי לעדכן שאגיע באיחור של כמה דקות{clause}.",
            "סליחה על אי-הנוחות, אעדכן אם יהיה שינוי.",
        ], " ({details})"),
        "en": ("Running a few minutes late", "Running a few minutes late", [
            "Just a heads-up that I'm running a few minutes late{clause}.",
            "Sorry for the inconvenience. I'll update you if anything changes.",
        ], " ({details})"),
    },
    "job": {
        "he": ("הגשת מועמדות", "הגשת מועמדות למשרת {details}", [
            "ברצוני להגיש את מועמדותי למשרה{clause}.",
            "מצורפים קורות החיים שלי.",
            "יש לי ניסיון רלוונטי, מוטיבציה גבוהה ורצון אמיתי להצטרף לצוות שלכם.",
            "אשמח להזדמנות להיפגש ולספר עוד.",
        ], " – {details}"),
        "en": ("Job application", "Application for the {details} position", [
            "I'd like to apply for the {details_or_position} position.",
            "Please find my resume attached.",
            "I bring relevant experience, strong motivation and a genuine desire to join your team.",
            "I'd welcome the opportunity to meet and tell you more.",
        ], ""),
    },
    "resignation": {
        "he": ("הודעה על התפטרות", "הודעה על התפטרות", [
            "ברצוני להודיע על התפטרותי מתפקידי{clause}.",
            "אני מודה על ההזדמנות, על האמון ועל כל מה שלמדתי כאן.",
            "אעשה כל שביכולתי כדי להבטיח חפיפה מסודרת.",
        ], " – {details}"),
        "en": ("Resignation notice", "Resignation notice", [
            "Please accept this as formal notice of my resignation{clause}.",
            "I'm grateful for the opportunity, the trust and everything I've learned here.",
            "I'll do everything I can to ensure a smooth handover.",
        ], ", {details}"),
    },
    "invitation": {
        "he": ("הזמנה", "הזמנה: {details}", [
            "שמחים להזמין אותך להצטרף אלינו.",
            "{details}",
            "נשמח מאוד לראות אותך!",
            "נשמח לאישור הגעה עד סוף השבוע.",
        ], ""),
        "en": ("You're invited", "You're invited: {details}", [
            "We'd love for you to join us.",
            "{details}",
            "It would be wonderful to see you there!",
            "Please let us know by the end of the week whether you can make it.",
        ], ""),
    },
    "reminder": {
        "he": ("תזכורת ידידותית", "תזכורת: {details}", [
            "תזכורת קטנה לגבי {details_or_default}.",
            "אם כבר טיפלת בזה, אפשר להתעלם מההודעה.",
            "תודה!",
        ], ""),
        "en": ("Friendly reminder", "Reminder: {details}", [
            "Just a friendly reminder about {details_or_default}.",
            "If you've already taken care of it, please disregard this message.",
            "Thanks!",
        ], ""),
    },
    "introduction": {
        "he": ("היכרות קצרה", "היכרות קצרה בנושא {details}", [
            "שמי {sender_name}, ואני פונה אליך{clause}.",
            "אשמח לספר עוד על מה שאני עושה ולבחון אפשרות לשיתוף פעולה.",
            "האם יתאים לך לשיחה קצרה של 15 דקות בשבוע הבא?",
        ], " בנושא {details}"),
        "en": ("A quick introduction", "Introduction: {details}", [
            "My name is {sender_name}, and I'm reaching out{clause}.",
            "I'd love to tell you more about what I do and explore a possible collaboration.",
            "Would you be open to a short 15-minute call next week?",
        ], " regarding {details}"),
    },
    "quote": {
        "he": ("בקשה להצעת מחיר", "בקשה להצעת מחיר עבור {details}", [
            "אבקש לקבל הצעת מחיר{clause}.",
            "אשמח לקבל גם פירוט של זמני אספקה ותנאי תשלום.",
            "תודה מראש על המענה.",
        ], " עבור {details}"),
        "en": ("Request for a quote", "Request for a quote: {details}", [
            "I'd like to request a quote{clause}.",
            "Please also include delivery times and payment terms.",
            "Thank you in advance for your reply.",
        ], " for {details}"),
    },
    "cancellation": {
        "he": ("בקשת ביטול", "בקשת ביטול: {details}", [
            "ברצוני לבטל את {details_or_default}.",
            "אבקש לאשר את הביטול בכתב.",
            "תודה על הטיפול.",
        ], ""),
        "en": ("Cancellation request", "Cancellation request: {details}", [
            "I'd like to cancel {details_or_default}.",
            "Please confirm the cancellation in writing.",
            "Thank you for taking care of this.",
        ], ""),
    },
    "generic": {
        "he": ("{purpose}", "{purpose}", [
            "אני פונה אליך בנושא {purpose}.",
            "{details}",
            "אשמח לשמוע ממך ולעמוד לרשותך בכל שאלה.",
        ], ""),
        "en": ("{purpose}", "{purpose}", [
            "I'm writing to you regarding {purpose}.",
            "{details}",
            "I look forward to hearing from you and am happy to answer any questions.",
        ], ""),
    },
}
_DEFAULTS = {
    "he": {"reminder": "מה שסיכמנו", "cancellation": "ההזמנה שלי", "sender": "[השם שלך]", "position": ""},
    "en": {"reminder": "what we agreed on", "cancellation": "my order", "sender": "[Your name]", "position": "open"},
}


# Kinds whose template has a slot for what the email is about (the rest put details on their own line).
_SUBJECT_KINDS = ("meeting", "follow_up", "complaint", "thanks", "apology", "vacation", "job", "introduction", "quote",
                  "reminder", "cancellation")
_EN_ABOUT = re.compile(r"\b(?:about|regarding|re|concerning|to discuss)\s*:?\s+(.+)$", re.I)
_EN_FOR = re.compile(r"\bfor\s+(?!(?:my|the|our)\s+(?:boss|manager|team|client|customer|teacher)\b)(.+)$", re.I)
_HE_ABOUT = re.compile(r"(?:^|\s)(?:בנושא|לגבי|בעניין|בקשר ל|עבור)\s*(.+)$")
_HE_ON = re.compile(r"(?:^|\s)על\s+(.+)$")


def _purpose_subject(purpose: str, kind: str) -> str:
    """The topic hiding in a purpose: 'meeting request about the Q3 budget' -> 'the Q3 budget',
    'בקשה להצעת מחיר לעיצוב אתר' -> 'עיצוב אתר'. Empty when there is none."""
    if kind not in _SUBJECT_KINDS:
        return ""
    text = (purpose or "").strip().rstrip(".!?")
    if not has_hebrew(text):
        m = (_EN_ABOUT.search(text) or (_EN_FOR.search(text) if kind in ("thanks", "apology", "quote", "job") else None)
             or (re.search(r"\bon\s+(.+)$", text, re.I) if kind == "follow_up" else None))
        if m:
            return m.group(1).strip()
        if kind in ("cancellation", "reminder"):
            m = re.search(r"\b(?:cancel(?:lation of)?|remind(?:er)?(?: (?:you|them|him|her))?(?: about| of)?)\s+(.+)$",
                          text, re.I)
            if m and not re.match(r"(?:request|email|message|letter)\b", m.group(1), re.I):
                return m.group(1).strip()
        return ""
    m = _HE_ABOUT.search(text) or (_HE_ON.search(text) if kind in ("thanks", "apology") else None)
    if m:
        return m.group(1).strip()
    words = dict(_EMAIL_KINDS).get(kind, ())
    for w in sorted((w for w in words if has_hebrew(w)), key=len, reverse=True):
        i = text.find(w)
        if i < 0:
            continue
        rest = text[i + len(w):].strip()
        if rest[:1] in ("ל", "ב") and len(rest) > 3:
            rest = rest[1:]
        if rest.startswith(("של ", "את ")):
            rest = rest[3:]
        return rest.strip()
    return ""


def email_kind(purpose: str) -> str:
    text = norm(purpose)
    for kind, words in _EMAIL_KINDS:
        if any(keyword_in(text, w, normalized=True) for w in words):
            return kind
    return "generic"


def email_draft(purpose: str, *, recipient: str = "", details: str = "", lang: str | None = None,
                tone: str = "formal", sender: str = "") -> EmailDraft:
    """A complete email (subject + body) for a purpose such as 'sick day', 'meeting request',
    'follow-up', 'complaint' or 'thank you' (Hebrew or English purpose text)."""
    lang = resolve_lang(lang, f"{purpose} {details}")
    kind = email_kind(purpose)
    friendly = tone in ("friendly", "casual", "informal", "warm")
    subj, subj_d, lines, clause_t = _EMAILS[kind][lang]
    details = (details or "").strip().rstrip(".") or _purpose_subject(purpose, kind)
    purpose_clean = clean_topic(purpose).strip().rstrip(".") or purpose
    sender_name = sender or _DEFAULTS[lang]["sender"]
    clause = clause_t.replace("{details}", details) if details else ""
    tomorrow = bool(re.search(r"\btomorrow\b|\bמחר\b", f"{purpose} {details}", re.I))
    subs = {
        "details": details, "clause": clause, "purpose": purpose_clean, "sender_name": sender_name,
        "details_or_default": details or _DEFAULTS[lang].get(kind, ""),
        "details_or_position": details or _DEFAULTS[lang]["position"],
        "day": ("מחר" if tomorrow else "היום") if lang == "he" else ("tomorrow" if tomorrow else "today"),
        "back": ("מיד כשאחלים" if tomorrow else "כבר מחר") if lang == "he" else ("soon" if tomorrow else "tomorrow"),
    }
    # "I'm sick tomorrow" only restates what the sick-day template already says.
    restated = kind == "sick" and email_kind(details) == "sick" and len(details.split()) <= 6
    subject = subj_d if details else subj
    for k, v in subs.items():
        subject = subject.replace("{" + k + "}", v)
    body_lines = []
    for line in lines:
        if line == "{details}":
            if details and not restated:
                body_lines.append(_cap(details) + ("." if not details.endswith(("!", "?", ".")) else ""))
            continue
        for k, v in subs.items():
            line = line.replace("{" + k + "}", v)
        body_lines.append(re.sub(r"\s{2,}", " ", line).replace(" .", "."))
    if lang == "he":
        greet = (f"היי {recipient}," if recipient else "היי,") if friendly else (
            f"שלום {recipient}," if recipient else "שלום רב,")
        close = "תודה," if friendly else "בברכה,"
    else:
        greet = (f"Hi {recipient}," if recipient else "Hi there,") if friendly else (
            f"Dear {recipient}," if recipient else "Hello,")
        close = "Thanks," if friendly else "Best regards,"
    if friendly and lang == "en":
        body_lines = [ln.replace("I would", "I'd") for ln in body_lines]
    body = f"{greet}\n\n" + "\n".join(body_lines) + f"\n\n{close}\n{sender_name}"
    return EmailDraft(subject=_cap(subject), body=body, lang=lang, kind=kind)


def email(purpose: str, *, recipient: str = "", details: str = "", lang: str | None = None, tone: str = "formal",
          sender: str = "") -> str:
    """The email as text: a 'Subject:' line (or 'נושא:'), a blank line and the body."""
    return email_draft(purpose, recipient=recipient, details=details, lang=lang, tone=tone, sender=sender).text


# --------------------------------------------------------------------------------------------
# Brainstorm
# --------------------------------------------------------------------------------------------

_ANGLES: dict[str, dict[str, list[str]]] = {
    "audience": {
        "he": ["התאמה ל{aud}: חבילה, מסרים ומחיר שנבנו במיוחד עבורם", "קמפיין שמדבר ישירות אל {aud} – בשפה שלהם",
               "שבוע בלעדי ל{aud} עם הטבה מיוחדת"],
        "en": ["A tailored offer for {aud}: package, message and price built just for them",
               "A campaign that speaks directly to {aud}, in their own words", "An exclusive week for {aud} with a special perk"],
    },
    "feature": {
        "he": ["להבליט את '{ftitle}' כיתרון המרכזי: {ftext}", "סרטון של 30 שניות שמראה את '{ftitle}' בפעולה"],
        "en": ["Make '{ftitle}' the headline benefit: {ftext}", "A 30-second video showing '{ftitle}' in action"],
    },
    "channel": {
        "he": ["סדרת סרטונים קצרים בטיקטוק ובאינסטגרם מאחורי הקלעים", "ניוזלטר שבועי עם טיפ אחד ומבצע אחד",
               "שיתוף פעולה עם שלושה משפיענים מקומיים בתחום", "קבוצת ווטסאפ לחברי מועדון עם עדכונים ראשונים"],
        "en": ["A behind-the-scenes series of short TikTok and Instagram videos", "A weekly newsletter with one tip and one offer",
               "A collaboration with three local creators in the space", "A members-only WhatsApp group with first access to news"],
    },
    "twist": {
        "he": ["מהדורה מוגבלת שמתחלפת בכל חודש", "'הלקוחות בוחרים': הצבעה פתוחה על המוצר הבא",
               "מבחן עיוור עם פרס למנחשים הנכונים", "אירוע לילה חודשי אחרי שעות הפעילות"],
        "en": ["A limited edition that changes every month", "'You choose': an open vote on the next product",
               "A blind test with a prize for the best guess", "A monthly after-hours night event"],
    },
    "partnership": {
        "he": ["חבילה משותפת עם עסק משלים באזור", "אחוז מכל רכישה לעמותה מקומית, עם דיווח שקוף ללקוחות"],
        "en": ["A joint bundle with a complementary local business",
               "A share of every purchase to a local charity, reported transparently to customers"],
    },
    "content": {
        "he": ["מדריך חינמי ל{aud}: איך לבחור נכון", "סדרת פוסטים 'שאלה של לקוח' עם תשובות קצרות ומועילות",
               "סיפורי לקוחות אמיתיים בפורמט לפני ואחרי"],
        "en": ["A free guide for {aud}: how to choose well", "A 'customer question' post series with short, useful answers",
               "Real customer stories in a before-and-after format"],
    },
    "pricing": {
        "he": ["מנוי חודשי עם הטבה קבועה", "חבילת היכרות במחיר מיוחד ללקוחות חדשים",
               "תוכנית 'חבר מביא חבר' עם הטבה לשניהם"],
        "en": ["A monthly subscription with a built-in perk", "An intro bundle at a special price for new customers",
               "A refer-a-friend program that rewards both sides"],
    },
    "experience": {
        "he": ["סדנה מעשית ל{aud}", "הפתעה קטנה בכל הזמנה, עם פתק בכתב יד", "חוויית הזמנה של 30 שניות מהנייד"],
        "en": ["A hands-on workshop for {aud}", "A small surprise with every order, plus a handwritten note",
               "A 30-second ordering experience on mobile"],
    },
    "tech": {
        "he": ["צ'אטבוט שעונה על שאלות נפוצות 24/7", "המלצות אישיות לפי רכישות קודמות",
               "אפליקציה עם צבירת נקודות ומשחקיות"],
        "en": ["A chatbot that answers common questions 24/7", "Personal recommendations based on past purchases",
               "An app with points and light gamification"],
    },
    "sustainability": {
        "he": ["אריזות ממוחזרות והטבה על החזרת אריזה", "קיזוז פחמן לכל הזמנה, עם מונה שקוף באתר"],
        "en": ["Recycled packaging and a reward for returning it", "Carbon offset for every order, with a public counter"],
    },
    "event": {
        "he": ["השקה חגיגית עם הדגמות חיות", "יום פתוח עם הנחה למשתתפים ומפגש עם הצוות"],
        "en": ["A launch party with live demos", "An open day with a discount for visitors and a meet-the-team session"],
    },
}
_ANGLE_LABELS = {
    "he": {"audience": "קהל יעד", "feature": "יתרון", "channel": "ערוץ", "twist": "טוויסט", "partnership": "שיתוף פעולה",
           "content": "תוכן", "pricing": "תמחור", "experience": "חוויה", "tech": "טכנולוגיה", "sustainability": "קיימות",
           "event": "אירוע"},
    "en": {"audience": "Audience", "feature": "Feature", "channel": "Channel", "twist": "Twist", "partnership": "Partnership",
           "content": "Content", "pricing": "Pricing", "experience": "Experience", "tech": "Tech",
           "sustainability": "Sustainability", "event": "Event"},
}


def brainstorm(topic: str, *, lang: str | None = None, n: int = 10, seed: int | None = None) -> list[str]:
    """Idea list from several angles (audience, feature, channel, twist, partnership…), each
    prefixed by its angle: 'ערוץ: ניוזלטר שבועי…' / 'Channel: A weekly newsletter…'."""
    lang = resolve_lang(lang, topic)
    c = _copy(topic, lang)
    rng = make_rng("brainstorm", c.topic, lang, seed=seed)
    angles = list(_ANGLES)
    rng.shuffle(angles)
    pools = {a: _ANGLES[a][lang][:] for a in angles}
    for pool in pools.values():
        rng.shuffle(pool)
    feats = c.features[:]
    rng.shuffle(feats)
    auds = list(c.audience)
    rng.shuffle(auds)
    ideas: list[str] = []
    round_ = 0
    while len(ideas) < n and round_ < 6:
        for a in angles:
            if len(ideas) >= n:
                break
            pool = pools[a]
            if round_ >= len(pool):
                continue
            f = feats[(len(ideas) + round_) % len(feats)]
            idea = _fill(pool[round_], aud=auds[(len(ideas)) % len(auds)], ftitle=f[1], ftext=f[2])
            if lang == "en" and "{" not in idea:
                idea = _cap(idea)
            ideas.append(f"{_ANGLE_LABELS[lang][a]}: {idea}")
        round_ += 1
    return ideas[: max(0, n)]


# --------------------------------------------------------------------------------------------
# Names
# --------------------------------------------------------------------------------------------

_EN_SUFFIX_FUSED = ("ly", "ify", "io")
_EN_COMPOUND = ("Lab", "Hub", "Works", "Craft", "House", "Co", "Nest", "Spot", "Loop", "Base", "Studio", "Box")
_EN_ADJ = ("Golden", "Little", "Urban", "Wild", "True", "Bright", "Bold", "Daily", "North", "Blue", "Happy", "Good",
           "Slow", "Second", "Kind", "Clever")
_EN_PLACE = ("House", "Room", "Corner", "Collective", "Club", "Society", "Studio", "Market", "Company")
_HE_WORDS = ("שקד", "גפן", "אופק", "ענבר", "שחר", "זיו", "אלה", "תמר", "רימון", "דבש", "כרמל", "צבר", "אורן", "אביב",
             "נוף", "טל", "קשת", "מרווה", "זית", "תאנה", "ברוש", "גל", "יסמין", "לילך", "כלנית", "סהר")
_HE_PARTS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "coffee": (("קפה", "בית הקלייה"), ()), "food": (("מסעדת", "ביסטרו", "המטבח של"), ()),
    "fashion": (("בוטיק", "סטודיו"), ("אופנה",)), "electronics": ((), ("טק", "דיגיטל")),
    "books": (("ספרי", "הוצאת"), ()), "tech": ((), ("טכנולוגיות", "לאבס", "דיגיטל")),
    "fitness": (("סטודיו",), ("פיט", "כושר")), "health": (("קליניקת", "מרכז"), ("בריאות",)),
    "education": (("אקדמיית", "מרכז הלמידה"), ()), "kids": (("עולם",), ("קידס",)),
    "beauty": (("סלון", "סטודיו"), ("ביוטי",)), "travel": ((), ("טיולים", "תיירות")),
    "realestate": ((), ("נכסים", "נדל\"ן")), "finance": ((), ("פיננסים", "השקעות", "ייעוץ")),
    "legal": (("משרד עו\"ד",), ("ושות'",)), "music": (("להקת",), ("הפקות",)), "pets": ((), ("פטס",)),
    "nature": (("משתלת",), ()), "luxury": (("תכשיטי",), ()), "wedding": (("פרחי",), ("אירועים",)),
    "studio": (("סטודיו",), ("עיצוב",)), "auto": (("מוסך",), ("מוטורס",)), "games": ((), ("גיימס", "סטודיוס")),
    "generic": ((), ("פלוס", "פרו", "גרופ")),
}


def names(topic: str, *, lang: str | None = None, n: int = 10, seed: int | None = None) -> list[str]:
    """Brand / product name ideas (curated per domain plus generated combinations)."""
    lang = resolve_lang(lang, topic)
    c = _copy(topic, lang)
    rng = make_rng("names", c.topic, lang, seed=seed)
    out: list[str] = []
    if lang == "he":
        prefixes, suffixes = _HE_PARTS.get(c.domain.key, _HE_PARTS["generic"])
        curated = list(c.domain.names_he)
        rng.shuffle(curated)
        words = list(_HE_WORDS)
        rng.shuffle(words)
        generated = [f"{p} {w}" for p in prefixes for w in words[:6]] + [f"{w} {s}" for s in suffixes for w in words[6:12]]
        rng.shuffle(generated)
        for a, b in zip(curated, generated, strict=False):
            out += [a, b]
        out += curated + generated
    else:
        roots = list(c.domain.roots)
        if c.domain is GENERIC:
            extra = [w.capitalize() for w in re.findall(r"[A-Za-z]{3,}", c.topic) if w.lower() not in ("and", "the", "for")]
            roots = extra[:4] + roots
        rng.shuffle(roots)
        pool: list[str] = []
        for r in roots:
            low = r.lower()
            if low[-1] not in "aeiouy":
                pool.append(r + rng.choice(_EN_SUFFIX_FUSED))
            pool.append(r + rng.choice(_EN_COMPOUND))
            pool.append(f"{rng.choice(_EN_ADJ)} {r}")
            pool.append(f"The {r} {rng.choice(_EN_PLACE)}")
        for a, b in zip(roots[::2], roots[1::2], strict=False):
            pool.append(f"{a} & {b}")
        rng.shuffle(pool)
        out = pool
    return list(dict.fromkeys(x.strip() for x in out if x.strip()))[: max(0, n)]


# --------------------------------------------------------------------------------------------
# Landing-page copy (used by mockups and website templates)
# --------------------------------------------------------------------------------------------

_PEOPLE = {
    "he": [("נועה כהן", "f"), ("דניאל לוי", "m"), ("מיכל אברהם", "f"), ("יוסי מזרחי", "m"), ("שירה פרץ", "f"),
           ("אורי ביטון", "m")],
    "en": [("Emma Johnson", "f"), ("Liam Carter", "m"), ("Olivia Brown", "f"), ("Noah Davis", "m"), ("Ava Miller", "f"),
           ("Ethan Wilson", "m")],
}
_QUOTES = {
    "he": {"m": ["{ftitle} זה בדיוק מה שחיפשתי. ממליץ בחום!", "שירות מעולה ויחס אישי. כבר המלצתי לכל החברים.",
                 "מהפעם הראשונה הרגשתי שמבינים מה אני צריך."],
           "f": ["{ftitle} זה בדיוק מה שחיפשתי. ממליצה בחום!", "שירות מעולה ויחס אישי. כבר המלצתי לכל החברות.",
                 "מהפעם הראשונה הרגשתי שמבינים מה אני צריכה."]},
    "en": {"m": ["{ftitle} is exactly what I was looking for. Highly recommended!",
                 "Great service and a personal touch. I've already told all my friends.",
                 "From day one I felt they understood what I needed."],
           "f": ["{ftitle} is exactly what I was looking for. Highly recommended!",
                 "Great service and a personal touch. I've already told all my friends.",
                 "From day one I felt they understood what I needed."]},
}


def testimonials(topic: str, *, lang: str | None = None, n: int = 3, seed: int | None = None) -> list[dict[str, str]]:
    """Plausible customer quotes ({'name', 'quote', 'role'}) for mockups and templates."""
    lang = resolve_lang(lang, topic)
    c = _copy(topic, lang)
    rng = make_rng("testimonials", c.topic, lang, seed=seed)
    people = _PEOPLE[lang][:]
    rng.shuffle(people)
    out = []
    for i, (person, g) in enumerate(people[: max(0, n)]):
        quotes = _QUOTES[lang][g]
        q = _fill(quotes[i % len(quotes)], ftitle=c.features[i % len(c.features)][1])
        aud = c.audience[i % len(c.audience)]
        role = {"he": f"מתוך {aud}", "en": f"one of our {aud}"}[lang] if c.audience else ""
        out.append({"name": person, "quote": q, "role": role})
    return out


def landing_copy(topic: str, *, lang: str | None = None, name: str | None = None,
                 seed: int | None = None) -> dict[str, object]:
    """Hero, features, CTA and about text for a landing page about `topic`."""
    lang = resolve_lang(lang, topic)
    c = _copy(topic, lang)
    brand = name or names(topic, lang=lang, n=1, seed=seed)[0]
    sl = slogans(topic, lang=lang, n=3, seed=seed)
    tl = taglines(topic, lang=lang, n=2, seed=seed)
    he = lang == "he"
    return {
        "name": brand,
        "headline": sl[0],
        "subheadline": tl[0],
        "cta": "בואו נתחיל" if he else "Get started",
        "secondary_cta": "לפרטים נוספים" if he else "Learn more",
        "features": [{"icon": i, "title": t, "text": x} for i, t, x in c.features],
        "about": (f"{brand} הוא {c.label} שנולד מתוך אהבה ל{c.essence}. אנחנו מאמינים ב{c.offer}, ביחס אישי ובשקיפות מלאה."
                  if he else f"{brand} is a {c.label} built on a love of {c.essence}. We believe in {c.offer}, a personal "
                             f"touch and complete transparency."),
        "audience": list(c.audience),
        "testimonials": testimonials(topic, lang=lang, seed=seed),
        "label": c.label,
        "essence": c.essence,
        "offer": c.offer,
        "domain": c.domain.key,
        "emoji": c.domain.emoji,
    }
