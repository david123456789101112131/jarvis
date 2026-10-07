"""Fast, rule-based intent detection for Hebrew and English commands.

No network, no model: a command like "JARVIS, פתח לי את כרום" is classified in well under a
millisecond, which is what lets simple voice commands start acting immediately. Anything the
rules don't recognise falls through to `chat` (and, when an AI provider is configured, the
core may ask the model to route it).

Each rule yields an `Intent(name, slots, agents, complexity)`:
  * instant  – answered by JARVIS itself (time, help, agent listing, mode switch…)
  * simple   – one agent skill, no planning (open/close app, create folder, volume…)
  * complex  – planned and orchestrated across agents (build an app, research, design…)
  * chat     – conversation
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import quote_plus

from jarvis.common.enums import AgentId, Mode
from jarvis.common.text import detect_language, normalize, strip_wake_word
from jarvis.host.catalog import find_app, find_site, looks_like_url, to_url

Complexity = Literal["instant", "simple", "complex", "chat"]


class I:  # noqa: E742 - short namespace for intent names
    KILL_SWITCH = "kill_switch"
    STOP_ALL_AGENTS = "stop_all_agents"
    AGENT_ACTIVATE = "agent_activate"
    AGENT_STOP = "agent_stop"
    AGENT_RESTART = "agent_restart"
    AGENT_ASSIGN = "agent_assign"
    STOP_TASK = "stop_task"
    RESUME_TASK = "resume_task"
    SET_MODE = "set_mode"
    SHOW_AGENTS = "show_agents"
    SHOW_TASKS = "show_tasks"
    SHOW_HISTORY = "show_history"
    SYSTEM_STATUS = "system_status"
    TIME = "time"
    DATE = "date"
    HELP = "help"
    GREETING = "greeting"
    THANKS = "thanks"
    VOLUME = "volume"
    BRIGHTNESS = "brightness"
    MEDIA = "media"
    LOCK_SCREEN = "lock_screen"
    POWER = "power"
    SCREENSHOT = "screenshot"
    CREATE_FOLDER = "create_folder"
    OPEN_FOLDER = "open_folder"
    FIND_FILES = "find_files"
    RUN_PROJECT = "run_project"
    WEATHER = "weather"
    DESIGN = "design"
    WRITE_TEXT = "write_text"
    BUILD_APP = "build_app"
    WRITE_CODE = "write_code"
    ORGANIZE_PROJECT = "organize_project"
    PLAN = "plan"
    RESEARCH = "research"
    WEB_SEARCH = "web_search"
    OPEN_WEBSITE = "open_website"
    OPEN_APP = "open_app"
    CLOSE_APP = "close_app"
    REMEMBER = "remember"
    RECALL = "recall"
    CHAT = "chat"


@dataclass
class Intent:
    name: str
    slots: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.9
    language: str = "en"
    agents: list[AgentId] = field(default_factory=list)
    complexity: Complexity = "simple"
    text: str = ""          # command text with the wake word removed
    had_wake_word: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "slots": self.slots,
            "confidence": self.confidence,
            "language": self.language,
            "agents": [a.value for a in self.agents],
            "complexity": self.complexity,
            "text": self.text,
        }


# --------------------------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------------------------
def _alt(*words: str) -> str:
    return "(?:" + "|".join(sorted(words, key=len, reverse=True)) + ")"


LI = r"(?:לי\s+)?"
ET = r"(?:את\s+)?"

V_OPEN_HE = _alt("תפתח", "פתח", "תפתחי", "פתחי", "תפעיל", "הפעל", "תפעילי", "הפעילי", "תריץ", "הרץ", "תריצי", "הריצי",
                 "תעלה", "תדליק", "הדלק", "תדליקי", "לפתוח", "להפעיל", "תכנס ל", "תיכנס ל", "כנס ל", "היכנס ל", "תעבור ל")
V_CLOSE_HE = _alt("תסגור", "סגור", "תסגרי", "סגרי", "תכבה", "כבה", "תכבי", "כבי", "לסגור", "לכבות", "תהרוג", "הרוג")
V_CREATE_HE = _alt("תיצור", "תצור", "צור", "תיצרי", "צרי", "תצרי", "תעשה", "עשה", "תעשי", "תכין", "הכן", "תכיני", "תייצר",
                   "ייצר", "ליצור", "תוסיף", "הוסף", "תפתח", "פתח")
V_BUILD_STRONG_HE = _alt("תבנה", "בנה", "תבני", "בני", "לבנות", "תכתוב", "כתוב", "תכתבי", "כתבי", "לכתוב", "תיצור", "צור",
                         "תצור", "תיצרי", "צרי", "תכין", "הכן", "תכיני", "תייצר", "ייצר", "תפתח", "פתח", "תפתחי",
                         "פתחי", "לפתח", "תתכנת", "תכנת")
V_RESEARCH_HE = _alt("תחקור", "חקור", "תחקרי", "חקרי", "תחפש", "חפש", "תחפשי", "חפשי", "תבדוק", "בדוק", "תבדקי", "בדקי",
                     "תמצא", "מצא", "תמצאי", "מצאי", "תברר", "ברר", "תבררי", "בררי", "תשווה", "השווה", "תשווי", "השווי",
                     "תסכם", "סכם", "תסכמי", "סכמי", "לחקור", "לבדוק", "לחפש")
V_SEARCH_HE = _alt("תחפש", "חפש", "תחפשי", "חפשי", "תמצא", "מצא", "תמצאי", "מצאי", "תאתר", "אתר")
V_DESIGN_HE = _alt("תעצב", "עצב", "תעצבי", "עצבי", "לעצב", "תצייר", "צייר", "תציירי", "ציירי")
V_ORGANIZE_HE = _alt("תארגן", "ארגן", "תארגני", "ארגני", "לארגן", "תסדר", "סדר", "תסדרי", "סדרי")
V_STOP_HE = _alt("תעצור", "עצור", "תעצרי", "עצרי", "תפסיק", "הפסק", "תפסיקי", "הפסיקי", "תבטל", "בטל", "תבטלי", "בטלי",
                 "לעצור", "להפסיק", "לבטל", "תכבה", "כבה")
V_RESTART_HE = _alt("תפעיל מחדש", "הפעל מחדש", "תפעילי מחדש", "הפעילי מחדש", "תאתחל", "אתחל", "תאתחלי", "אתחלי",
                    "תריסטרט", "ריסטרט")
V_ACTIVATE_HE = _alt("תפעיל", "הפעל", "תפעילי", "הפעילי", "תדליק", "הדלק", "תעיר", "העיר", "תתחיל", "התחל")
V_SHOW_HE = _alt("תראה לי", "הראה לי", "תראי לי", "הראי לי", "תציג", "הצג", "תציגי", "הציגי", "תציג לי", "הצג לי",
                 "תפרט", "פרט", "תפתח", "פתח")
V_PLAN_HE = _alt("תתכנן", "תכנן", "תתכנני", "תכנני", "לתכנן")
V_REMEMBER_HE = _alt("תזכור", "זכור", "תזכרי", "זכרי", "תרשום", "רשום", "תרשמי", "רשמי")
V_RESUME_HE = _alt("תמשיך", "המשך", "תמשיכי", "המשיכי", "להמשיך", "תחדש", "חדש")
V_WRITE_HE = _alt("תכתוב", "כתוב", "תכתבי", "כתבי", "לכתוב", "תנסח", "נסח", "תנסחי", "נסחי", "תחבר", "חבר")
V_UP_HE = _alt("תגביר", "הגבר", "תגבירי", "הגבירי", "תעלה", "העלה", "תעלי", "העלי")
V_DOWN_HE = _alt("תנמיך", "הנמך", "תנמיכי", "הנמיכי", "תוריד", "הורד", "תורידי", "הורידי")

BUILD_NOUNS_HE = _alt("אפליקציה", "אפליקציית", "אפליקצייה", "אתר", "אתר אינטרנט", "מערכת", "תוכנה", "תוכנית", "משחק",
                      "בוט", "סקריפט", "דף נחיתה", "ממשק", "כלי", "שרת", "תוסף", "api", "אפי", "חנות אונליין",
                      "חנות אינטרנטית", "חנות", "בלוג", "דשבורד", "פלטפורמה", "שירות", "מחשבון", "יישום", "פרויקט")
BUILD_NOUNS_EN = _alt("app", "application", "website", "web site", "site", "web app", "webapp", "system", "program", "tool",
                      "game", "bot", "script", "api", "service", "dashboard", "landing page", "store", "shop", "online store",
                      "e-commerce site", "extension", "plugin", "cli", "backend", "frontend", "server", "platform", "software",
                      "calculator", r"blog(?!\s+post)", "portfolio", "project")
DESIGN_NOUNS_HE = _alt("מסך", "לוגו", "סמל", "אייקון", "אייקונים", "עיצוב", "פלטת צבעים", "פלטה", "באנר", "פוסטר",
                       "מצגת", "כרטיס ביקור", "תמונה", "איור", "אנימציה", "ערכת נושא", "טיפוגרפיה", "מוקאפ", "מיתוג",
                       "סקיצה", "קונספט")
DESIGN_NOUNS_EN = _alt("screen", "ui", "ux", "interface", "logo", "icon", "icons", "design", "color palette", "palette",
                       "banner", "poster", "presentation", "slides", "slide deck", "deck", "mockup", "wireframe",
                       "page layout", "theme", "illustration", "image", "picture", "animation", "brand", "branding",
                       "style guide", "concept", "moodboard", "business card")
TEXT_NOUNS_EN = _alt("blog post", "post", "article", "email", "e-mail", "letter", "story", "poem", "essay", "speech",
                     "caption", "slogan", "tagline", "product description", "description", "bio", "text", "copy",
                     "ad copy", "newsletter", "tweet", "message", "cover letter", "summary of", "video script")
TEXT_NOUNS_HE = _alt("פוסט", "מאמר", "מייל", "אימייל", "מכתב", "סיפור", "שיר", "חיבור", "נאום", "כיתוב", "סלוגן",
                     "תיאור", "ביוגרפיה", "טקסט", "מודעה", "ניוזלטר", "הודעה", "ציוץ", "תסריט", "מכתב מקדים", "ברכה")
PLAN_NOUNS_HE = (
    r"(?:תוכנית|תכנית)\s+(?:עבודה|פעולה|אימונים|לימודים|תזונה|עסקית|שיווק|שיווקית|שבועית|יומית|חודשית|שנתית|"
    r"פרויקט|טיול|חיסכון|ל(?:שבוע|חודש|יום|שנה|השבוע|החודש|היום|השנה)\b)"
    # "a plan for <something>" (a wedding, a trip, the exam) — but "a program for calculating …" is software.
    r"|(?:תוכנית|תכנית)\s+(?:ל|עבור\s+)(?!(?:ה)?(?:חישוב|המרת|המרה|עיבוד|סריקת|גיבוי|הורדת|הצפנת|מחשב|מחשבון|פייתון|"
    r"python|ווינדוס|windows|אנדרואיד|אייפון|טלפון|ניהול\s+(?:מלאי|לקוחות|הזמנות|משתמשים))\b)\S+"
    r"|(?:תוכנית|תכנית)$|רודמאפ|מפת\s+דרכים|לוח\s+זמנים|לו\"ז|גאנט"
)

AGENT_REF_EN = (
    r"(?P<agent>(?:research|researcher|system|development|developer|dev|creative|design|designer|planner|planning)\s+agent"
    r"|agent\s*(?:number\s+|no\.?\s*|#\s*)?[1-5])"
)
AGENT_REF_HE = (
    r"(?P<agent>(?:ה)?סוכן\s+(?:ה)?(?:מחקר|מערכת|פיתוח|קריאייטיב|קריאטיב|יצירה|יצירתי|עיצוב|תכנון|מתכנן|[1-5])"
    r"|(?:ה)?סוכן\s+מספר\s+[1-5])"
)

MODE_WORDS = {
    Mode.CHAT: ("chat", "conversation", "צ'אט", "צאט", "שיחה"),
    Mode.NORMAL: ("normal", "default", "regular", "רגיל", "נורמלי", "ברירת מחדל"),
    Mode.TASK: ("task", "tasks", "משימה", "משימות"),
    Mode.AUTONOMOUS: ("autonomous", "auto", "autopilot", "אוטונומי", "אוטונומית", "עצמאי", "אוטומטי", "טייס אוטומטי"),
    Mode.DEBUG: ("debug", "debugging", "דיבאג", "דיבג", "ניפוי שגיאות", "באגים"),
}

FOLDER_LOCATIONS = {
    "desktop": ("desktop", "the desktop", "my desktop", "שולחן העבודה", "שולחן עבודה", "דסקטופ"),
    "documents": ("documents", "my documents", "the documents folder", "מסמכים", "המסמכים", "התיקייה מסמכים"),
    "downloads": ("downloads", "my downloads", "הורדות", "ההורדות"),
    "pictures": ("pictures", "photos", "תמונות", "התמונות"),
    "music": ("music", "מוזיקה", "המוזיקה"),
    "videos": ("videos", "סרטונים", "הסרטונים", "וידאו"),
    "workspace": ("workspace", "the workspace", "סביבת העבודה", "הוורקספייס", "תיקיית העבודה"),
    "projects": ("projects", "my projects", "פרויקטים", "הפרויקטים"),
    "home": ("home", "home folder", "תיקיית הבית", "הבית"),
}

FILLER_PREFIXES = (
    "please ", "can you ", "could you ", "would you ", "will you ", "i want you to ", "i need you to ", "i'd like you to ",
    "go ahead and ", "now ", "quickly ", "kindly ", "hey ", "ok ", "okay ",
    "בבקשה ", "תוכל ל", "תוכלי ל", "אתה יכול ל", "את יכולה ל", "אפשר ל", "אני רוצה ש", "אני צריך ש", "אני צריכה ש",
    "תעשה לי טובה ו", "בוא ", "בואי ", "יאללה ", "עכשיו ", "מהר ",
)
FILLER_SUFFIXES = (" please", " for me", " now", " right now", " בבקשה", " עכשיו", " תודה", " מהר")

# Infinitive -> imperative so "תוכל לפתוח את כרום" becomes "פתח את כרום".
_INFINITIVES = {
    "לפתוח": "פתח", "להפעיל": "הפעל", "לסגור": "סגור", "ליצור": "צור", "לבנות": "בנה", "לכתוב": "כתוב", "לחפש": "חפש",
    "לבדוק": "בדוק", "לעצב": "עצב", "לארגן": "ארגן", "לתכנן": "תכנן", "לעצור": "עצור", "להריץ": "הרץ", "לחקור": "חקור",
    "לפתח": "פתח", "לנעול": "נעל", "להגביר": "הגבר", "להנמיך": "הנמך", "להשתיק": "השתק", "לצלם": "צלם", "להמשיך": "המשך",
    "לזכור": "זכור", "להציג": "הצג", "להראות": "הראה", "לכבות": "כבה", "להשוות": "השווה", "לסכם": "סכם", "לברר": "ברר",
    "למצוא": "מצא", "לנסח": "נסח", "להכין": "הכן", "לתקן": "תקן",
}

# Agent hints per kind of build request (the planner may still adjust the final plan).
_BUILD_AGENTS: dict[str, tuple[AgentId, ...]] = {
    "website": (AgentId.PLANNER, AgentId.RESEARCH, AgentId.CREATIVE, AgentId.DEVELOPMENT, AgentId.SYSTEM),
    "store": (AgentId.PLANNER, AgentId.RESEARCH, AgentId.CREATIVE, AgentId.DEVELOPMENT, AgentId.SYSTEM),
    "game": (AgentId.PLANNER, AgentId.CREATIVE, AgentId.DEVELOPMENT),
    "app": (AgentId.PLANNER, AgentId.DEVELOPMENT),
    "api": (AgentId.PLANNER, AgentId.DEVELOPMENT),
    "bot": (AgentId.PLANNER, AgentId.DEVELOPMENT),
}


def _strip_fillers(text: str) -> str:
    changed = True
    t = text.strip(" ,.!?")
    while changed:
        changed = False
        low = t.lower()
        for p in FILLER_PREFIXES:
            if low.startswith(p) and len(t) > len(p):
                t = t[len(p):].lstrip(" ,")
                if p.endswith("ל") and t.split(" ", 1)[0] in {w[1:] for w in _INFINITIVES}:
                    t = "ל" + t
                changed = True
                break
        low = t.lower()
        for s in FILLER_SUFFIXES:
            if low.endswith(s) and len(t) > len(s):
                t = t[: -len(s)].rstrip(" ,")
                changed = True
                break
    first, _, rest = t.partition(" ")
    if first in _INFINITIVES:
        t = _INFINITIVES[first] + (" " + rest if rest else "")
    return t.strip(" ,.!?")


def _clean_object(text: str) -> str:
    t = text.strip(" .,!?:\"'")
    for p in ("לי את ה", "לי את ", "את ה", "את ", "לי ", "the ", "a ", "an ", "my ", "this ", "that "):
        if t.lower().startswith(p):
            t = t[len(p):]
            break
    return t.strip(" .,!?:\"'")


def _agent_from_ref(ref: str) -> AgentId | None:
    r = normalize(ref)
    r = re.sub(r"^ה?סוכן\s+(?:מספר\s+)?", "", r)
    r = re.sub(r"^ה(?=(מחקר|מערכת|פיתוח|קריאייטיב|קריאטיב|יצירה|יצירתי|עיצוב|תכנון|מתכנן))", "", r)
    r = re.sub(r"^agent\s*(?:number\s+|no\.?\s*|#\s*)?(?=[1-5])", "", r)
    try:
        return AgentId.parse(r)
    except (ValueError, KeyError):
        return None


def _location_key(text: str) -> str | None:
    t = normalize(text).strip(" .,")
    t = re.sub(r"^(?:on|in|at|inside|to|into|על|בתוך|לתוך)\s+", "", t)
    candidates = {t}
    if t[:1] in {"ב", "ל"}:
        candidates.add(t[1:])
    for c in list(candidates):
        candidates.add(re.sub(r"^(?:the|my)\s+", "", c))
        if c.startswith("ה"):
            candidates.add(c[1:])
    for key, words in FOLDER_LOCATIONS.items():
        for w in words:
            wn = normalize(w)
            if wn in candidates or wn.removeprefix("ה") in candidates:
                return key
    return None


# --------------------------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------------------------
Builder = Callable[[re.Match[str], str], dict[str, Any] | None]


@dataclass
class Rule:
    name: str
    patterns: list[re.Pattern[str]]
    complexity: Complexity
    agents: tuple[AgentId, ...] = ()
    build: Builder | None = None
    confidence: float = 0.92


def _p(*patterns: str) -> list[re.Pattern[str]]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


def _b_agent(extra: str | None = None) -> Builder:
    def builder(m: re.Match[str], _: str) -> dict[str, Any] | None:
        agent = _agent_from_ref(m.group("agent"))
        if agent is None:
            return None
        out: dict[str, Any] = {"agent": agent.value}
        if extra:
            instruction = (m.groupdict().get(extra) or "").strip(" :,.-–—\"'")
            if not instruction:
                return None
            out["instruction"] = instruction
        return out

    return builder


def _b_mode(m: re.Match[str], _: str) -> dict[str, Any] | None:
    word = normalize(m.group("mode")).strip(" .")
    for mode, words in MODE_WORDS.items():
        if word in {normalize(w) for w in words}:
            return {"mode": mode.value}
    return None


def _level_action(text: str, up_words: tuple[str, ...], down_words: tuple[str, ...]) -> dict[str, Any] | None:
    t = normalize(text)
    level = re.search(r"(\d{1,3})\s*(?:%|percent|אחוז)?", t)
    if level:
        return {"action": "set", "level": max(0, min(100, int(level.group(1))))}
    if any(w in t for w in up_words):
        return {"action": "up"}
    if any(w in t for w in down_words):
        return {"action": "down"}
    return None


def _b_volume(m: re.Match[str], text: str) -> dict[str, Any] | None:
    t = normalize(text)
    if any(w in t for w in ("unmute", "בטל השתקה", "בטל את ההשתקה", "תבטל השתקה", "תבטל את ההשתקה", "החזר קול",
                            "תחזיר את הקול")):
        return {"action": "unmute"}
    if any(w in t for w in ("mute", "השתק", "תשתיק", "השתיקי", "תשתיקי")):
        return {"action": "mute"}
    return _level_action(
        text,
        ("up", "raise", "increase", "louder", "הגבר", "תגביר", "הגבירי", "תגבירי", "חזק", "תעלה", "העלה"),
        ("down", "lower", "decrease", "quieter", "softer", "הנמך", "תנמיך", "הנמיכי", "תנמיכי", "חלש", "תוריד", "הורד"),
    )


def _b_brightness(m: re.Match[str], text: str) -> dict[str, Any] | None:
    return _level_action(
        text,
        ("up", "raise", "increase", "brighter", "הגבר", "תגביר", "תעלה", "העלה", "תעלי", "העלי", "יותר בהיר"),
        ("down", "lower", "decrease", "dimmer", "dim", "הנמך", "תנמיך", "תוריד", "הורד", "תורידי", "יותר כהה"),
    )


def _b_media(m: re.Match[str], text: str) -> dict[str, Any] | None:
    t = normalize(text)
    if any(w in t for w in ("next", "skip", "הבא", "דלג")):
        return {"action": "next"}
    if any(w in t for w in ("previous", "prev", "הקודם", "אחורה")):
        return {"action": "previous"}
    return {"action": "play_pause"}


def _b_power(m: re.Match[str], text: str) -> dict[str, Any] | None:
    t = normalize(text)
    if any(w in t for w in ("restart", "reboot", "מחדש", "אתחל", "תאתחל")):
        return {"action": "restart"}
    if any(w in t for w in ("hibernate", "תרדים", "הרדם", "שינה", "sleep")):
        return {"action": "sleep"}
    if any(w in t for w in ("log off", "log out", "sign out", "logout", "התנתק", "תנתק", "יציאה מהמשתמש")):
        return {"action": "logoff"}
    return {"action": "shutdown"}


def _b_create_folder(m: re.Match[str], text: str) -> dict[str, Any] | None:
    gd = m.groupdict()
    name = (gd.get("name") or "").strip(" .,\"'")
    loc = (gd.get("loc") or "").strip()
    location = _location_key(loc) if loc else None
    if loc and not location:
        candidate = re.sub(r"^(?:on|in|at|inside|into|על|בתוך|לתוך)\s+", "", loc, flags=re.I)
        candidate = re.sub(r"^ב(?=[a-z]:\\)", "", candidate, flags=re.I)
        if re.match(r"^(?:[a-z]:\\|/|~)", candidate, re.I):
            location = candidate
        else:
            # Not a known place: the words belong to the folder name ("בשם דוחות בדיקה").
            name = f"{name} {loc}".strip()
    name = re.sub(r"^(?:named|called|בשם|שנקראת|שקוראים לה)\s+", "", name, flags=re.I).strip(" \"'")
    return {"name": name or None, "location": location or "workspace"}


def _b_open_folder(m: re.Match[str], text: str) -> dict[str, Any] | None:
    loc = _location_key(m.group("x"))
    return {"location": loc} if loc else None


_SITE_PREFIX = re.compile(
    r"^(?:(?:the\s+)?(?:website|web\s+site|site|page)\s+(?:of\s+|for\s+)?|(?:ה)?אתר\s+(?:של\s+)?(?:ה)?|(?:ה)?דף\s+של\s+)",
    re.I,
)
_APP_PREFIX = re.compile(r"^(?:(?:the\s+)?(?:app|application|program)\s+|(?:ה)?(?:אפליקציה|תוכנה)\s+(?:של\s+)?)", re.I)
_APP_SUFFIX = re.compile(r"\s+(?:app|application|program|browser)$", re.I)


def _b_open_target(m: re.Match[str], text: str) -> dict[str, Any] | None:
    target = _clean_object(m.group("x"))
    if not target:
        return None
    norm = normalize(target)
    if norm in {"הפרויקט", "the project", "project", "this project", "my project", "פרויקט"}:
        return None

    site_name = _SITE_PREFIX.sub("", target, count=1).strip()
    explicit_site = site_name != target
    site = find_site(site_name)
    if site:
        return {"_intent": I.OPEN_WEBSITE, "url": site.url, "site": site.display}
    if looks_like_url(site_name):
        return {"_intent": I.OPEN_WEBSITE, "url": to_url(site_name), "site": site_name}
    if explicit_site and site_name:
        # "open the website of <name>" for a site we don't know: open the search results for it.
        return {"_intent": I.OPEN_WEBSITE, "url": f"https://www.google.com/search?q={quote_plus(site_name)}",
                "site": site_name, "search": True}

    folderish = any(w in norm for w in ("folder", "תיקי", "directory"))
    loc = _location_key(re.sub(r"\b(?:folder|directory)\b|תיקיית|תיקייה|תיקיה", "", target).strip())
    if loc and folderish:
        return {"_intent": I.OPEN_FOLDER, "location": loc}
    if re.match(r"^(?:[a-z]:\\|/|~/|\\\\)", target, re.I):
        return {"_intent": I.OPEN_FOLDER, "path": target}

    app_name = _APP_SUFFIX.sub("", _APP_PREFIX.sub("", target, count=1)).strip()
    app = find_app(app_name) or find_app(target)
    if app:
        return {"app": app.id, "app_name": app.display}
    if len(app_name.split()) > 4 or not app_name:
        return None  # too long to be an app name; let other rules / chat handle it
    return {"app": app_name, "app_name": app_name}


def _b_close_target(m: re.Match[str], text: str) -> dict[str, Any] | None:
    target = _clean_object(m.group("x"))
    target = _APP_SUFFIX.sub("", _APP_PREFIX.sub("", target, count=1)).strip()
    if not target or len(target.split()) > 4:
        return None
    if normalize(target) in {"task", "the task", "משימה", "המשימה", "הכל", "הכול", "everything", "it", "זה"}:
        return None
    app = find_app(target)
    return {"app": app.id if app else target, "app_name": app.display if app else target}


def _b_web_search(m: re.Match[str], text: str) -> dict[str, Any] | None:
    q = _clean_object(m.group("q"))
    if not q:
        return None
    engine = normalize(m.groupdict().get("engine") or "google")
    if "youtube" in engine or "יוטיוב" in engine:
        return {"query": q, "engine": "youtube", "url": f"https://www.youtube.com/results?search_query={quote_plus(q)}"}
    return {"query": q, "engine": "google", "url": f"https://www.google.com/search?q={quote_plus(q)}"}


def _b_build(m: re.Match[str], text: str) -> dict[str, Any] | None:
    desc = text.strip()
    noun = normalize(m.groupdict().get("noun") or "")
    kind = "app"
    if any(w in noun for w in ("website", "site", "אתר", "דף נחיתה", "landing", "בלוג", "blog", "portfolio")):
        kind = "website"
    if any(w in noun for w in ("store", "shop", "חנות", "e-commerce")):
        kind = "store"
    if any(w in noun for w in ("game", "משחק")):
        kind = "game"
    if any(w in noun for w in ("api", "אפי", "server", "שרת", "backend", "service", "שירות")):
        kind = "api"
    if any(w in noun for w in ("bot", "בוט")):
        kind = "bot"
    if any(w in noun for w in ("script", "סקריפט", "cli")):
        # A single script is a coding request, not a multi-agent build.
        return {"_intent": I.WRITE_CODE, "request": desc, "language": _code_language(desc)}
    # "a site for my store" / "אתר לחנות" is a store website.
    if kind == "website" and re.search(r"\b(?:store|shop|e-?commerce)\b|לחנות|חנות", normalize(desc)):
        kind = "store"
    return {"description": desc, "kind": kind, "_agents": _BUILD_AGENTS.get(kind, _BUILD_AGENTS["app"])}


_LANGS = {
    "python": ("python", "פייתון", "פיתון", "py"), "javascript": ("javascript", "js", "node", "ג'אווהסקריפט", "גאווהסקריפט"),
    "typescript": ("typescript", "ts", "טייפסקריפט"), "html": ("html",), "css": ("css",), "java": ("java", "ג'אווה"),
    "powershell": ("powershell", "פאוורשל"), "bash": ("bash", "shell script"), "sql": ("sql",), "csharp": ("c#", "csharp"),
    "cpp": ("c++", "cpp"),
}


def _code_language(text: str) -> str | None:
    t = f" {normalize(text)} "
    for lang, words in _LANGS.items():
        if any(re.search(rf"(?<![\w]){re.escape(w)}(?![\w])", t) for w in words):
            return lang
    return None


def _b_code(m: re.Match[str], text: str) -> dict[str, Any] | None:
    return {"request": text.strip(), "language": _code_language(text)}


def _b_text(name: str) -> Builder:
    def builder(m: re.Match[str], text: str) -> dict[str, Any] | None:
        val = (m.groupdict().get("x") or "").strip(" .,!?:\"'")
        return {name: val or text.strip()}

    return builder


def _b_research(m: re.Match[str], text: str) -> dict[str, Any] | None:
    q = (m.groupdict().get("q") or "").strip(" .,!?:\"'")
    if len(q) < 2:
        return None
    return {"query": q}


def _b_weather(m: re.Match[str], text: str) -> dict[str, Any] | None:
    loc = (m.groupdict().get("loc") or "").strip(" .,!?:\"'")
    t = normalize(text)
    when = "tomorrow" if ("tomorrow" in t or "מחר" in t) else "today"
    return {"location": loc or None, "when": when}


def _b_find_files(m: re.Match[str], text: str) -> dict[str, Any] | None:
    q = _clean_object(m.group("x"))
    if not q:
        return None
    loc = m.groupdict().get("loc")
    location = _location_key(loc) if loc else None
    return {"query": q, "location": location}


def _b_status_focus(m: re.Match[str], text: str) -> dict[str, Any] | None:
    t = normalize(text)
    for key, words in {
        "cpu": ("cpu", "processor", "מעבד"),
        "memory": ("ram", "memory", "זיכרון", "ראם"),
        "disk": ("disk", "storage", "space", "דיסק", "אחסון", "מקום"),
        "network": ("network", "internet", "wifi", "רשת", "אינטרנט"),
        "battery": ("battery", "סוללה", "בטריה"),
    }.items():
        if any(w in t for w in words):
            return {"focus": key}
    return {}


def _b_history(m: re.Match[str], text: str) -> dict[str, Any] | None:
    t = normalize(text)
    return {"last": any(w in t for w in ("last", "previous", "אחרונה", "קודמת", "האחרונה", "הקודמת"))}


def _b_organize(m: re.Match[str], text: str) -> dict[str, Any] | None:
    """A known folder ("the downloads") becomes a location; anything else names a project or a path."""
    target = (m.groupdict().get("x") or "").strip(" .,\"'")
    key = _location_key(re.sub(r"^(?:folder|תיקיית|תיקייה|תיקיה)\s+", "", target)) if target else None
    if key:
        return {"location": key}
    return _b_project(m, text)


def _b_project(m: re.Match[str], text: str) -> dict[str, Any] | None:
    name = (m.groupdict().get("x") or "").strip(" .,\"'")
    if normalize(name) in {"", "this", "the", "my", "this project", "הזה", "שלי", "הנוכחי", "current"}:
        name = ""
    return {"project": name or None}


_RULES: list[Rule] = [
    Rule(I.KILL_SWITCH, _p(
        r"^(?:emergency stop|kill switch|stop everything|stop all(?: tasks)?(?: and agents)?|abort (?:everything|all)|shut it all down|halt everything)$",
        rf"^(?:{V_STOP_HE}\s+(?:את\s+)?(?:ה)?(?:כל|כול)(?:\s+הדברים)?|עצירת חירום|מתג חירום|עצור מיד הכל|עצור הכל מיד)$",
    ), "instant"),
    Rule(I.STOP_ALL_AGENTS, _p(
        r"^(?:stop|halt|pause|deactivate|shut down) (?:all|every) (?:the )?agents?$",
        rf"^{V_STOP_HE}\s+(?:את\s+)?(?:כל\s+)?ה?סוכנים(?:\s+כולם)?$",
    ), "instant"),
    Rule(I.AGENT_RESTART, _p(
        rf"^(?:restart|reboot|reset|reload)\s+(?:the\s+)?{AGENT_REF_EN}$",
        rf"^{V_RESTART_HE}\s+{ET}{AGENT_REF_HE}$",
    ), "instant", build=_b_agent()),
    Rule(I.AGENT_ASSIGN, _p(
        rf"^(?:give|assign|send|pass|hand)\s+(?:the\s+)?{AGENT_REF_EN}\s+(?:this|the following|a)?\s*(?:task|job)?\s*[:,\-–—]?\s*(?P<task>.+)$",
        rf"^(?:assign|give|send)\s+(?P<task>.+?)\s+to\s+(?:the\s+)?{AGENT_REF_EN}$",
        rf"^(?:ask|tell|have|let|get)\s+(?:the\s+)?{AGENT_REF_EN}\s+(?:to\s+)?(?P<task>.+)$",
        rf"^(?:תן|תני|תעביר|העבר|תעבירי|העבירי|שלח|תשלח)\s+(?:ל|אל\s+)?{AGENT_REF_HE}\s+(?:את\s+)?(?:ה)?(?:משימה|המשימה הזאת|הזאת|הבאה)?\s*[:,\-–—]?\s*(?P<task>.+)$",
        rf"^(?:תבקש|בקש|תבקשי|בקשי)\s+מ{AGENT_REF_HE}\s+(?P<task>.+)$",
        rf"^(?:תגיד|תגידי|תאמר|אמור)\s+ל{AGENT_REF_HE}\s+(?P<task>.+)$",
    ), "complex", build=_b_agent("task")),
    Rule(I.AGENT_STOP, _p(
        rf"^(?:stop|halt|pause|deactivate|disable|kill|shut down)\s+(?:the\s+)?{AGENT_REF_EN}$",
        rf"^{V_STOP_HE}\s+{ET}{AGENT_REF_HE}$",
    ), "instant", build=_b_agent()),
    Rule(I.AGENT_ACTIVATE, _p(
        rf"^(?:activate|start|enable|wake up|turn on|launch|resume)\s+(?:the\s+)?{AGENT_REF_EN}$",
        rf"^{V_ACTIVATE_HE}\s+{ET}{AGENT_REF_HE}$",
    ), "instant", build=_b_agent()),
    Rule(I.STOP_TASK, _p(
        r"^(?:stop|cancel|abort|halt|kill|end)\s+(?:the\s+|this\s+|current\s+|that\s+|my\s+)?(?:task|job|operation|process|work)(?:\s+(?P<task_id>task_[a-z0-9]+))?$",
        r"^(?:stop|cancel|abort)(?:\s+(?:it|that|this))?$",
        rf"^{V_STOP_HE}(?:\s+{ET}(?:ה)?(?:משימה|משימה הנוכחית|המשימה הזאת|פעולה|זה|את זה|עבודה))?$",
    ), "instant"),
    Rule(I.RESUME_TASK, _p(
        r"^(?:resume|continue|restart)\s+(?:the\s+|my\s+|last\s+|previous\s+|interrupted\s+)*(?:task|job|work)(?:\s+(?P<task_id>task_[a-z0-9]+))?$",
        r"^continue (?:where|from where) (?:you|we) (?:stopped|left off)$",
        rf"^{V_RESUME_HE}\s+(?:את\s+)?(?:ה)?(?:משימה|עבודה)(?:\s+(?:האחרונה|הקודמת|שנקטעה|שעצרת))?$",
        rf"^{V_RESUME_HE}\s+מאיפה\s+ש(?:עצרת|עצרנו|הפסקת|הפסקנו)$",
    ), "complex"),
    Rule(I.SET_MODE, _p(
        r"^(?:switch|change|go|move|set)(?:\s+mode)?\s+(?:to|into)\s+(?P<mode>[\w' ]+?)\s+mode$",
        r"^(?:enable|activate|enter|turn on)\s+(?P<mode>[\w' ]+?)\s+mode$",
        r"^(?P<mode>chat|normal|task|autonomous|debug)\s+mode(?:\s+on)?$",
        r"^(?:תעבור|עבור|תעברי|עברי|תחליף|החלף|תחליפי|החליפי|תכנס|היכנס|הפעל|תפעיל)\s+(?:ל|אל\s+)?מצב\s+(?P<mode>[\w' ]+)$",
        r"^מצב\s+(?P<mode>[\w' ]+)$",
    ), "instant", build=_b_mode),
    Rule(I.SHOW_AGENTS, _p(
        r"^(?:show|list|display|get)(?:\s+me)?\s+(?:all\s+|the\s+|active\s+|running\s+|my\s+)*agents(?:\s+status)?$",
        r"^(?:which|what) agents are (?:active|running|working|online)$",
        r"^agents? status$",
        rf"^{V_SHOW_HE}\s+(?:את\s+)?(?:כל\s+)?ה?סוכנים(?:\s+(?:ה)?(?:פעילים|זמינים))?$",
        r"^(?:מי|אילו|איזה)\s+(?:ה)?סוכנים(?:\s+(?:פעילים|עובדים|זמינים|יש))?$",
        r"^(?:מה\s+)?(?:ה)?מצב\s+(?:של\s+)?ה?סוכנים$",
    ), "instant"),
    Rule(I.SHOW_TASKS, _p(
        r"^(?:show|list|display)(?:\s+me)?\s+(?:all\s+|the\s+|active\s+|running\s+|my\s+|current\s+)*tasks$",
        r"^what are you (?:working on|doing)$",
        rf"^{V_SHOW_HE}\s+(?:את\s+)?(?:כל\s+)?ה?משימות(?:\s+(?:ה)?(?:פעילות|הנוכחיות))?$",
        r"^(?:על\s+)?מה\s+(?:אתה\s+|את\s+)?(?:עובד|עובדת|עושה)(?:\s+עכשיו)?$",
    ), "instant"),
    Rule(I.SHOW_HISTORY, _p(
        r"^(?:show|open|display)(?:\s+me)?\s+(?:the\s+|my\s+)?(?:command\s+|task\s+|activity\s+)?(?:history|log)$",
        r"^(?:show|open|display)(?:\s+me)?\s+(?:the\s+)?(?:last|previous)\s+(?:task|command)$",
        r"^what (?:did you do|have you done)(?:\s+today)?$",
        rf"^{V_SHOW_HE}\s+(?:את\s+)?(?:ה)?היסטוריה(?:\s+(?:של\s+)?(?:ה)?(?:פקודות|משימות))?$",
        rf"^{V_SHOW_HE}\s+(?:את\s+)?(?:ה)?משימה\s+(?:ה)?(?:אחרונה|קודמת)$",
        r"^מה\s+עשית(?:\s+היום)?$",
    ), "instant", build=_b_history),
    Rule(I.POWER, _p(
        r"^(?:shut\s?down|turn off|power off|restart|reboot|hibernate|put to sleep|sleep|log off|log out|sign out of)\s+(?:the\s+|my\s+)?(?:computer|pc|system|machine|windows|laptop)$",
        r"^(?:תכבה|כבה|תכבי|כבי)\s+(?:את\s+)?(?:ה)?מחשב$",
        rf"^(?:{V_RESTART_HE})\s+(?:את\s+)?(?:ה)?מחשב$",
        r"^(?:תרדים|הרדם|תרדימי|הרדימי)\s+(?:את\s+)?(?:ה)?מחשב$",
        r"^(?:תכניס|הכנס|תכניסי)\s+(?:את\s+)?(?:ה)?מחשב\s+(?:ל)?מצב\s+שינה$",
        r"^(?:תנתק|נתק|תתנתק|התנתק)\s+(?:את\s+)?(?:ה)?משתמש$",
    ), "simple", (AgentId.SYSTEM,), build=_b_power),
    Rule(I.SYSTEM_STATUS, _p(
        r"^(?:check|scan|diagnose|inspect|analyze)\s+(?:my\s+|the\s+)?(?:system|computer|pc|machine|laptop)(?:\s+(?:health|status))?$",
        r"^(?:show|get|display)(?:\s+me)?\s+(?:the\s+)?(?:system|computer|pc)\s+(?:status|health|info|stats|resources)$",
        r"^(?:system|pc|computer)\s+(?:status|health|check|report)$",
        r"^how(?:'s| is) my (?:system|computer|pc) (?:doing|running)$",
        r"^(?:check|show|what(?:'s| is))\s+(?:the\s+|my\s+)?(?:cpu|ram|memory|disk|storage|network|battery|internet)(?:\s+(?:usage|load|space|status|speed|level|connection))?$",
        r"^how much (?:ram|memory|disk space|storage|space|battery)(?: is| do i have)?(?: left| free| available| used)?$",
        rf"^(?:{V_RESEARCH_HE}|{V_SHOW_HE})\s+(?:לי\s+)?(?:את\s+)?(?:מצב\s+)?(?:ה)?(?:מחשב|מערכת)(?:\s+שלי)?$",
        r"^(?:מה\s+)?(?:ה)?מצב\s+(?:ה)?(?:מחשב|מערכת)$",
        r"^איך\s+(?:ה)?(?:מחשב|מערכת)(?:\s+(?:עובד|מתפקד|מרגיש))?$",
        r"^(?:בדיקת|סטטוס)\s+(?:מערכת|מחשב)$",
        rf"^(?:{V_RESEARCH_HE}|{V_SHOW_HE}|מה)\s+(?:לי\s+)?(?:את\s+)?(?:ה)?(?:צריכת|עומס|מצב)\s+(?:ה)?(?:מעבד|זיכרון|דיסק|רשת|סוללה|אינטרנט)$",
        r"^כמה\s+(?:זיכרון|מקום|סוללה|ראם)(?:\s+(?:פנוי|פנויה|נשאר|נשארה|יש|יש לי))*(?:\s+(?:ב)?(?:מחשב|דיסק))?$",
    ), "simple", (AgentId.SYSTEM,), build=_b_status_focus),
    Rule(I.TIME, _p(
        r"^(?:what(?:'s| is) the time|what time is it|time|current time|tell me the time|(?:tell me )?what time it is)$",
        r"^(?:מה\s+)?(?:ה)?שעה(?:\s+עכשיו)?$",
        r"^כמה\s+(?:ה)?שעה$",
    ), "instant"),
    Rule(I.DATE, _p(
        r"^(?:what(?:'s| is) (?:the |today's )?date|what day is (?:it|today)|today's date|date)$",
        r"^(?:מה\s+)?(?:ה)?תאריך(?:\s+היום)?$",
        r"^(?:איזה|מה)\s+יום\s+(?:היום|זה)$",
    ), "instant"),
    Rule(I.HELP, _p(
        r"^(?:help|commands|what can you do|what are your (?:capabilities|commands)|how can you help(?: me)?)$",
        r"^(?:עזרה|פקודות|מה\s+(?:אתה|את)\s+(?:יכול|יכולה)\s+לעשות|במה\s+(?:אתה|את)\s+(?:יכול|יכולה)\s+לעזור(?:\s+לי)?|איך\s+(?:אתה|את)\s+(?:יכול|יכולה)\s+לעזור(?:\s+לי)?)$",
    ), "instant"),
    Rule(I.GREETING, _p(
        r"^(?:hi|hello|hey|good (?:morning|afternoon|evening)|yo|what's up|howdy|are you there|you there)$",
        r"^(?:שלום|היי|הי|הלו|בוקר טוב|ערב טוב|צהריים טובים|מה נשמע|מה קורה|מה שלומך|אתה שם|את שם|אתה פה)$",
    ), "instant", confidence=0.85),
    Rule(I.THANKS, _p(
        r"^(?:thanks|thank you|thx|great job|well done|good job|perfect|awesome)(?: jarvis)?$",
        r"^(?:תודה|תודה רבה|תודה לך|יופי|מעולה|כל הכבוד|אחלה|מושלם|סבבה)$",
    ), "instant", confidence=0.85),
    Rule(I.VOLUME, _p(
        r"^(?:turn|crank)\s+(?:the\s+)?(?:volume|sound)\s+(?:up|down)$",
        r"^(?:turn|crank)\s+(?:it\s+)?(?:up|down)$",
        r"^(?:volume|sound)\s+(?:up|down|\d{1,3}\s*%?)$",
        r"^(?:set|change)\s+(?:the\s+)?(?:volume|sound)\s+to\s+\d{1,3}\s*%?(?:\s*percent)?$",
        r"^(?:increase|decrease|raise|lower)\s+(?:the\s+)?(?:volume|sound)(?:\s+by\s+\d+)?$",
        r"^(?:un)?mute(?:\s+(?:the\s+)?(?:sound|audio|volume|computer|pc))?$",
        r"^(?:louder|quieter|softer)$",
        rf"^(?:{V_UP_HE}|{V_DOWN_HE})\s+(?:לי\s+)?(?:את\s+)?(?:ה)?(?:ווליום|וליום|עוצמה|עוצמת\s+(?:ה)?(?:שמע|קול)|קול|סאונד)(?:\s+ל[-\s]?\d{{1,3}}\s*(?:אחוז|%)?)?$",
        r"^(?:תשתיק|השתק|תשתיקי|השתיקי)(?:\s+(?:את\s+)?(?:ה)?(?:מחשב|קול|סאונד|שמע))?$",
        r"^(?:תבטל|בטל|תבטלי|בטלי)\s+(?:את\s+)?(?:ה)?השתקה$",
        r"^(?:ווליום|וליום|עוצמה|עוצמת\s+שמע)\s+(?:ל[-\s]?)?\d{1,3}\s*(?:אחוז|%)?$",
        r"^(?:תקבע|קבע|תשים|שים)\s+(?:את\s+)?(?:ה)?(?:ווליום|וליום|עוצמה|עוצמת\s+(?:ה)?(?:שמע|קול))\s+(?:על|ל)[-\s]?\d{1,3}\s*(?:אחוז|%)?$",
        r"^(?:יותר חזק|יותר חלש|חזק יותר|חלש יותר)$",
    ), "simple", (AgentId.SYSTEM,), build=_b_volume),
    Rule(I.BRIGHTNESS, _p(
        r"^(?:set|change)\s+(?:the\s+|screen\s+)?brightness\s+to\s+\d{1,3}\s*%?(?:\s*percent)?$",
        r"^(?:increase|decrease|raise|lower|turn up|turn down|dim)\s+(?:the\s+)?(?:screen\s+)?brightness$",
        r"^(?:screen\s+)?brightness\s+(?:up|down|\d{1,3}\s*%?)$",
        r"^(?:dim|brighten)\s+(?:the\s+)?screen$",
        rf"^(?:{V_UP_HE}|{V_DOWN_HE})\s+(?:לי\s+)?(?:את\s+)?(?:ה)?בהירות(?:\s+(?:ה)?מסך)?(?:\s+ל[-\s]?\d{{1,3}}\s*(?:אחוז|%)?)?$",
        r"^(?:בהירות|בהירות מסך)\s+(?:ל[-\s]?)?\d{1,3}\s*(?:אחוז|%)?$",
        r"^(?:תקבע|קבע|תשים|שים)\s+(?:את\s+)?(?:ה)?בהירות\s+(?:על|ל)[-\s]?\d{1,3}\s*(?:אחוז|%)?$",
    ), "simple", (AgentId.SYSTEM,), build=_b_brightness),
    Rule(I.MEDIA, _p(
        r"^(?:pause|play|resume)(?:\s+(?:the\s+)?(?:music|song|track|media|video))?$",
        r"^(?:next|previous|skip)(?:\s+(?:song|track))?$",
        r"^(?:תשהה|השהה|תעצור|עצור|תנגן|נגן|תמשיך|המשך|תפעיל|הפעל|תשמיע|השמע)\s+(?:לי\s+)?(?:את\s+)?(?:ה)?(?:מוזיקה|שיר|נגינה|סרטון)$",
        r"^(?:ה)?(?:שיר|רצועה)\s+(?:ה)?(?:הבא|הבאה|הקודם|הקודמת)$",
        r"^(?:תדלג|דלג)(?:\s+על\s+(?:ה)?שיר)?$",
    ), "simple", (AgentId.SYSTEM,), build=_b_media),
    Rule(I.LOCK_SCREEN, _p(
        r"^lock\s+(?:the\s+|my\s+)?(?:computer|screen|pc|workstation|windows)$",
        r"^(?:תנעל|נעל|תנעלי|נעלי)\s+(?:את\s+)?(?:ה)?(?:מחשב|מסך)$",
    ), "simple", (AgentId.SYSTEM,)),
    Rule(I.SCREENSHOT, _p(
        r"^(?:take|grab|capture)\s+(?:a\s+)?screen\s?shot$",
        r"^screen\s?shot$",
        r"^(?:תצלם|צלם|תצלמי|צלמי)\s+(?:את\s+)?(?:ה)?מסך$",
        r"^(?:תעשה|עשה|תעשי)\s+צילום\s+מסך$",
        r"^צילום\s+מסך$",
    ), "simple", (AgentId.SYSTEM,)),
    Rule(I.CREATE_FOLDER, _p(
        r"^(?:create|make|add|new)\s+(?:a\s+)?(?:new\s+)?(?:folder|directory|dir)(?:\s+(?:named|called)?\s*(?P<name>\"[^\"]+\"|[^\s].*?))?(?:\s+(?P<loc>(?:on|in|at|inside|into)\s+.+))?$",
        rf"^{V_CREATE_HE}\s+{LI}(?:ה)?(?:תיקייה|תיקיה|תיקיית)(?:\s+חדשה)?(?:\s+(?:בשם|שנקראת)?\s*(?P<name>\"[^\"]+\"|[^\s].*?))?(?:\s+(?P<loc>(?:על\s+|בתוך\s+|לתוך\s+|ב)\S.*))?$",
        r"^(?:תיקייה|תיקיה)\s+חדשה(?:\s+(?:בשם\s+)?(?P<name>.+?))?(?:\s+(?P<loc>(?:על\s+|בתוך\s+|ב)\S.*))?$",
    ), "simple", (AgentId.SYSTEM,), build=_b_create_folder),
    Rule(I.OPEN_FOLDER, _p(
        r"^(?:open|show)\s+(?:the\s+|my\s+)?(?P<x>desktop|documents|downloads|pictures|music|videos|workspace|projects|home)\s+(?:folder|directory)$",
        r"^(?:open|show)\s+(?:the\s+|my\s+)?(?:folder|directory)\s+(?P<x>desktop|documents|downloads|pictures|music|videos|workspace|projects)$",
        rf"^{V_OPEN_HE}\s+{LI}(?:את\s+)?(?:ה)?(?:תיקיית|תיקייה|תיקיה)\s+(?P<x>.+)$",
    ), "simple", (AgentId.SYSTEM,), build=_b_open_folder),
    Rule(I.FIND_FILES, _p(
        r"^(?:find|search for|locate|look for|where is)\s+(?:the\s+|my\s+|a\s+)?(?:file|files|folder|document|documents)\s+(?:named\s+|called\s+)?(?P<x>.+?)(?:\s+(?P<loc>(?:on|in)\s+(?:the\s+|my\s+)?(?:desktop|documents|downloads|pictures|music|videos|workspace|projects)))?$",
        rf"^{V_SEARCH_HE}\s+{LI}(?:את\s+)?(?:ה)?(?:קובץ|קבצים|מסמך|מסמכים|תיקייה|תיקיה)\s+(?:בשם\s+|שנקרא\s+)?(?P<x>.+?)(?:\s+(?P<loc>(?:על\s+|ב)(?:שולחן העבודה|מסמכים|הורדות|תמונות|מוזיקה|סרטונים|פרויקטים)))?$",
        r"^(?:איפה|היכן)\s+(?:ה)?(?:קובץ|מסמך|תיקייה)\s+(?P<x>.+)$",
    ), "simple", (AgentId.SYSTEM,), build=_b_find_files),
    Rule(I.RUN_PROJECT, _p(
        r"^(?:run|start|launch|execute|build and run)\s+(?:this|the|my|current)?\s*project(?:\s+(?P<x>.+))?$",
        rf"^{V_OPEN_HE}\s+{LI}(?:את\s+)?(?:ה)?פרויקט(?:\s+(?P<x>.+))?$",
    ), "simple", (AgentId.DEVELOPMENT,), build=_b_project),
    Rule(I.ORGANIZE_PROJECT, _p(
        r"^(?:organize|organise|clean up|tidy up|tidy|structure|sort out)\s+(?:my|the|this)?\s*(?P<x>desktop|downloads|documents|pictures|music|videos|workspace|projects)(?:\s+folder)?$",
        r"^(?:organize|organise|clean up|tidy up|structure|sort out)\s+(?:my|the|this)?\s*(?:project|folder|files|workspace)(?:\s+(?P<x>.+))?$",
        rf"^{V_ORGANIZE_HE}\s+{LI}(?:את\s+)?(?:ה)?(?:פרויקט|תיקיית|תיקייה|תיקיה|קבצים|תיקיות)(?:\s+(?P<x>.+))?$",
        rf"^{V_ORGANIZE_HE}\s+{LI}(?:את\s+)?(?P<x>(?:ה)?(?:שולחן העבודה|הורדות|מסמכים|תמונות|מוזיקה|סרטונים|פרויקטים))$",
    ), "complex", (AgentId.PLANNER, AgentId.SYSTEM), build=_b_organize),
    Rule(I.WEATHER, _p(
        r"^(?:what(?:'s| is) the |how(?:'s| is) the )?weather(?:\s+(?:like|forecast))?(?:\s+(?:in|at|for)\s+(?P<loc>.+?))?(?:\s+(?:today|tomorrow|now))?$",
        r"^(?:will it rain|is it going to rain)(?:\s+(?:in|at)\s+(?P<loc>.+?))?(?:\s+(?:today|tomorrow))?$",
        r"^(?:מה\s+)?(?:ה)?(?:מזג\s+(?:ה)?אוויר|תחזית(?:\s+מזג\s+(?:ה)?אוויר)?)(?:\s+(?:היום|מחר|עכשיו))?(?:\s+ב(?P<loc>\S.*?))?(?:\s+(?:היום|מחר|עכשיו))?$",
        r"^(?:ירד|יהיה)\s+(?:גשם|חם|קר)(?:\s+(?:היום|מחר))?(?:\s+ב(?P<loc>\S.*?))?(?:\s+(?:היום|מחר))?$",
        r"^(?:כמה\s+)?(?:מעלות|חם|קר)\s+(?:יש\s+)?(?:היום|עכשיו|בחוץ|מחר)(?:\s+ב(?P<loc>\S.*))?$",
    ), "simple", (AgentId.RESEARCH,), build=_b_weather),
    Rule(I.WEB_SEARCH, _p(
        r"^(?:google|search google for|search on google for|look up on google)\s+(?P<q>.+)$",
        r"^search\s+(?P<engine>youtube|google)\s+for\s+(?P<q>.+)$",
        r"^(?:play|find|search for)\s+(?P<q>.+?)\s+on\s+(?P<engine>youtube)$",
        r"^(?:חפש|תחפש|חפשי|תחפשי)\s+(?:לי\s+)?ב(?P<engine>גוגל|יוטיוב)\s+(?P<q>.+)$",
        r"^(?:תשמיע|השמע|תנגן|נגן|תפעיל|הפעל|תשמיעי|תנגני)\s+(?:לי\s+)?(?:את\s+)?(?P<q>.+?)\s+ב(?P<engine>יוטיוב)$",
        r"^(?:תגגל|גגל|תגגלי|גגלי)\s+(?P<q>.+)$",
    ), "simple", (AgentId.SYSTEM,), build=_b_web_search),
    Rule(I.PLAN, _p(
        rf"^(?:{V_CREATE_HE}|{V_BUILD_STRONG_HE}|{V_WRITE_HE})\s+(?:לי\s+)?(?:את\s+)?(?:ה)?(?:{PLAN_NOUNS_HE})(?:\s+.+)?$",
        r"^(?:create|make|build|write|draft|prepare)\s+(?:me\s+)?(?:a\s+|an\s+)?(?:\w+\s+)?(?:plan|roadmap|schedule|timeline|action plan|work plan|study plan|workout plan)(?:\s+.+)?$",
    ), "complex", (AgentId.PLANNER,), build=_b_text("goal")),
    Rule(I.WRITE_TEXT, _p(
        rf"^(?:write|draft|compose|create|generate|prepare)\s+(?:me\s+)?(?:a\s+|an\s+|the\s+|some\s+)?(?:short\s+|long\s+|formal\s+|friendly\s+|professional\s+)?(?:{TEXT_NOUNS_EN})\b.*$",
        rf"^(?:{V_WRITE_HE}|{V_CREATE_HE})\s+{LI}(?:את\s+)?(?:ה)?(?:{TEXT_NOUNS_HE})(?:\s.*)?$",
    ), "complex", (AgentId.CREATIVE,), build=_b_text("brief")),
    Rule(I.DESIGN, _p(
        rf"^(?:design|draw|sketch|create|make|generate|produce|craft|prepare)\s+(?:me\s+)?(?:a\s+|an\s+|the\s+|some\s+|new\s+)*(?:[\w\-]+\s+){{0,2}}?(?:{DESIGN_NOUNS_EN})\b.*$",
        rf"^{V_DESIGN_HE}\s+.+$",
        rf"^(?:{V_CREATE_HE}|{V_BUILD_STRONG_HE})\s+{LI}(?:את\s+)?(?:ה)?(?:{DESIGN_NOUNS_HE})(?:\s.*)?$",
        r"^(?:improve|redesign|polish)\s+(?:the\s+|my\s+)?(?:ui|ux|design|interface|look)\b.*$",
        r"^(?:give me|suggest|brainstorm|come up with)\s+(?:some\s+|a few\s+)?(?:ideas|names|concepts|slogans)\b.*$",
        r"^(?:תשפר|שפר|תשפרי|שפרי)\s+(?:את\s+)?(?:ה)?(?:עיצוב|ממשק|חוויית\s+(?:ה)?משתמש|נראות)(?:\s.*)?$",
        r"^(?:תן|תני|תציע|הצע|תציעי)\s+(?:לי\s+)?(?:כמה\s+)?(?:רעיונות|שמות|קונספטים|סלוגנים)\b.*$",
    ), "complex", (AgentId.CREATIVE,), build=_b_text("brief")),
    Rule(I.BUILD_APP, _p(
        rf"^(?:build|create|make|develop|code|program|write|generate|set up|scaffold)\s+(?:me\s+|us\s+)?(?:a\s+|an\s+|the\s+|this\s+|my\s+|new\s+|simple\s+|small\s+|full\s+|complete\s+)*(?:[\w\-]+\s+){{0,3}}?(?P<noun>{BUILD_NOUNS_EN})\b.*$",
        rf"^(?:{V_BUILD_STRONG_HE})\s+{LI}(?:(?!את\s)(?!ה)(?P<noun>{BUILD_NOUNS_HE}))(?:\s.*)?$",
        rf"^(?:תבנה|בנה|תבני|בני|תכתוב|כתוב|תכתבי|כתבי)\s+{LI}(?:את\s+)?(?:ה)?(?P<noun>{BUILD_NOUNS_HE})(?:\s.*)?$",
        r"^(?:build|develop|make) (?:it|this|that)$",
        r"^(?:תבנה|בנה|תבני|בני|תפתח|תפתחי)\s+(?:לי\s+)?(?:את\s+)?(?:זה|את זה)$",
    ), "complex", (AgentId.PLANNER, AgentId.DEVELOPMENT), build=_b_build),
    Rule(I.WRITE_CODE, _p(
        r"^(?:write|create|generate|code)\s+(?:me\s+)?(?:a\s+|an\s+|the\s+|some\s+)?(?:python|javascript|js|typescript|ts|java|html|css|c#|c\+\+|sql|bash|powershell|node)?\s*(?:script|function|class|module|program|code|snippet|regex|query|component|test|tests)\b.*$",
        r"^(?:fix|debug|refactor|optimize|test|review)\s+(?:the\s+|my\s+|this\s+)?(?:bug|bugs|code|error|errors|function|script|tests?|issue|module|class|file)\b.*$",
        r"^(?:תתקן|תקן|תתקני|תקני|דבג|תדבג|תשכתב|שכתב|ריפקטור|תעשה ריפקטור|תבדוק את הקוד)\b.*$",
        r"^(?:כתוב|תכתוב|כתבי|תכתבי)\s+(?:לי\s+)?(?:קוד|פונקציה|סקריפט|מחלקה|בדיקות|טסטים|שאילתה)\b.*$",
    ), "complex", (AgentId.DEVELOPMENT,), build=_b_code),
    Rule(I.PLAN, _p(
        r"^(?:plan|outline|break down|make a plan for|create a roadmap for|roadmap|schedule)\s+(?P<x>.+)$",
        rf"^{V_PLAN_HE}\s+(?:לי\s+)?(?P<x>.+)$",
        r"^(?:תפרק|פרק|תפרקי|פרקי)\s+(?:לי\s+)?(?:את\s+)?(?P<x>.+?)\s+(?:ל)?(?:שלבים|משימות|צעדים)$",
    ), "complex", (AgentId.PLANNER,), build=_b_text("goal")),
    Rule(I.REMEMBER, _p(
        r"^(?:remember|note|keep in mind|don't forget)\s+(?:that\s+)?(?P<x>.+)$",
        rf"^{V_REMEMBER_HE}\s+(?:ש|את\s+זה\s+ש|לך\s+ש|לעצמך\s+ש|ש-)?(?P<x>.+)$",
    ), "instant", build=_b_text("fact")),
    Rule(I.RECALL, _p(
        r"^what do you (?:remember|know) about\s+(?P<x>.+)$",
        r"^(?:מה\s+)?(?:אתה\s+|את\s+)?(?:זוכר|זוכרת|יודע|יודעת)\s+על\s+(?P<x>.+)$",
    ), "instant", build=_b_text("query")),
    Rule(I.RESEARCH, _p(
        r"^(?:research|investigate|look up|look into|find out|find information (?:about|on)|find info (?:about|on)|learn about|study)\s+(?P<q>.+)$",
        r"^(?:search|check|look)\s+(?:the\s+)?(?:web|internet|online|net)\s+(?:for\s+|about\s+)?(?P<q>.+)$",
        r"^(?:search|check|look)\s+(?:for\s+)?(?P<q>.+?)\s+(?:online|on the (?:web|internet))$",
        r"^(?:search|look)\s+(?:for|up)\s+(?P<q>.+)$",
        r"^(?:compare|summarize|summarise)\s+(?P<q>.+)$",
        r"^(?P<q>(?:what(?:'s| is| are) the (?:price|cost|prices) of|how much (?:is|does|do|are|did))\s+.+)$",
        r"^(?:find|get)\s+(?:me\s+)?(?:the\s+)?(?P<q>(?:best|cheapest|latest|top|newest|recommended)\s+.+)$",
        rf"^(?:{V_RESEARCH_HE})\s+(?:לי\s+)?(?:ב)?(?:אינטרנט|רשת|גוגל|מקוון)\s+(?P<q>.+)$",
        r"^(?:תחקור|חקור|תחקרי|חקרי)\s+(?:לי\s+)?(?:את\s+)?(?P<q>.+)$",
        r"^(?:תמצא|מצא|תמצאי|מצאי|תחפש|חפש|תחפשי|חפשי)\s+(?:לי\s+)?(?:מידע|פרטים|נתונים|מקורות)\s+(?:על|לגבי|בנושא)\s+(?P<q>.+)$",
        r"^(?:תשווה|השווה|תשווי|השווי)\s+(?:לי\s+)?(?:בין\s+)?(?P<q>.+)$",
        r"^(?:תסכם|סכם|תסכמי|סכמי)\s+(?:לי\s+)?(?:את\s+)?(?P<q>.+)$",
        r"^(?P<q>(?:מה|כמה)\s+(?:ה)?(?:מחיר|עולה|עלות|עולים)\s+.+)$",
        r"^(?:תברר|ברר|תבררי|בררי)\s+(?:לי\s+)?(?P<q>.+)$",
        r"^(?:תמצא|מצא|תמצאי|מצאי|תחפש|חפש|תחפשי|חפשי)\s+(?:לי\s+)?(?:את\s+)?(?P<q>.+)$",
    ), "complex", (AgentId.RESEARCH,), build=_b_research),
    Rule(I.CLOSE_APP, _p(
        r"^(?:close|quit|exit|kill|terminate|shut down|shut)\s+(?P<x>.+)$",
        rf"^{V_CLOSE_HE}\s+(?P<x>.+)$",
    ), "simple", (AgentId.SYSTEM,), build=_b_close_target),
    Rule(I.OPEN_APP, _p(
        r"^(?:open|launch|start|run|fire up|bring up|go to|navigate to|show me)\s+(?P<x>.+)$",
        rf"^{V_OPEN_HE}\s*(?P<x>.+)$",
    ), "simple", (AgentId.SYSTEM,), build=_b_open_target),
]

_INTENT_DEFAULTS: dict[str, tuple[Complexity, tuple[AgentId, ...]]] = {
    I.OPEN_WEBSITE: ("simple", (AgentId.SYSTEM,)),
    I.OPEN_FOLDER: ("simple", (AgentId.SYSTEM,)),
    I.WRITE_CODE: ("complex", (AgentId.DEVELOPMENT,)),
}

# Intents that change something on the machine (used by CHAT mode to refuse actions).
ACTION_INTENTS = frozenset({
    I.VOLUME, I.BRIGHTNESS, I.MEDIA, I.LOCK_SCREEN, I.POWER, I.SCREENSHOT, I.CREATE_FOLDER, I.OPEN_FOLDER, I.RUN_PROJECT,
    I.DESIGN, I.WRITE_TEXT, I.BUILD_APP, I.WRITE_CODE, I.ORGANIZE_PROJECT, I.PLAN, I.RESEARCH, I.WEB_SEARCH,
    I.OPEN_WEBSITE, I.OPEN_APP, I.CLOSE_APP, I.AGENT_ASSIGN, I.RESUME_TASK, I.FIND_FILES, I.WEATHER, I.SYSTEM_STATUS,
})


class IntentDetector:
    """Stateless matcher; safe to share between voice and text paths."""

    def __init__(self, rules: list[Rule] | None = None) -> None:
        self.rules = rules or _RULES

    def detect(self, text: str) -> Intent:
        raw = (text or "").strip()
        command, had_wake = strip_wake_word(raw)
        language = detect_language(command or raw)
        if not command:
            return Intent(I.GREETING if had_wake else I.CHAT, {}, 0.8, language, [], "instant", "", had_wake)
        cleaned = _strip_fillers(normalize(command, lower=False))
        cleaned = re.sub(r"\s*[,،]\s*", " ", cleaned).strip()

        for rule in self.rules:
            for pattern in rule.patterns:
                m = pattern.match(cleaned)
                if not m:
                    continue
                slots = rule.build(m, cleaned) if rule.build else {}
                if slots is None:
                    continue
                name = slots.pop("_intent", rule.name)
                complexity, agents = rule.complexity, rule.agents
                if name != rule.name and name in _INTENT_DEFAULTS:
                    complexity, agents = _INTENT_DEFAULTS[name]
                agents = slots.pop("_agents", agents)
                if m.groupdict().get("task_id"):
                    slots["task_id"] = m.group("task_id")
                return Intent(name, slots, rule.confidence, language, list(agents), complexity, command, had_wake)

        return Intent(I.CHAT, {"text": command}, 0.4, language, [], "chat", command, had_wake)


_default = IntentDetector()


def detect_intent(text: str) -> Intent:
    return _default.detect(text)
