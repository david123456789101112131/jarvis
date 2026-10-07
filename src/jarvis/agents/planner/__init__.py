"""Planner Agent: plans, stages, priorities, progress tracking, blockers and roadmaps.

* `decompose` is how JARVIS splits a request between the five agents: rule plans for the request
  types it knows (build an app, research, design, organize, …), the AI model for anything else,
  and a one-agent plan as the last resort. The plan comes back as data; the orchestrator runs it.
* `make_plan`, `project_brief`, `review_progress` and `folder_review` produce documents a person
  can use. Offline they come from built-in phase templates and the files themselves; with an AI
  provider the model writes the content into the same structure, so the output looks the same.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from jarvis.agents.base import AGENT_INFO, AgentResult, AgentTask, BaseAgent, RunContext, skill, t
from jarvis.agents.common import resolve_folder, slugify
from jarvis.agents.planner import documents
from jarvis.agents.planner.planning import (
    KIND_NAMES,
    TEMPLATE_FOR_KIND,
    fallback_plan,
    llm_plan,
    project_name,
    rule_plan,
)
from jarvis.common.enums import AgentId, AgentStatus, ResultStatus
from jarvis.common.errors import PlanError, ProviderError, ProviderRefusal
from jarvis.common.i18n import plural
from jarvis.core.intents import I

OwnerName = Literal["research", "system", "development", "creative", "planner", "user"]
PriorityName = Literal["critical", "high", "normal", "low"]


# ------------------------------------------------------------------------------------------ AI schemas
class PlanTaskDraft(BaseModel):
    title: str = Field(description="One concrete action")
    priority: PriorityName
    owner: OwnerName = Field(description="Which JARVIS agent can do it, or 'user' when only the user can")


class PlanPhaseDraft(BaseModel):
    title: str
    days: int = Field(description="Calendar days this phase takes (1 or more)")
    tasks: list[PlanTaskDraft]


class PlanDocDraft(BaseModel):
    title: str = Field(description="Short plan title in the user's language")
    summary: str = Field(description="Two or three sentences: the approach and the expected result")
    phases: list[PlanPhaseDraft]
    first_actions: list[str] = Field(description="What to do first, most important first (1 to 3 items)")
    risks: list[str] = Field(description="Risks and blockers, each with how to handle it")
    missing_information: list[str] = Field(description="Facts the user could add to sharpen the plan")


class BriefFeature(BaseModel):
    title: str
    text: str = Field(description="One sentence: what it does for the user")
    must: bool = Field(description="True for must-have, false for nice-to-have")


class BriefPage(BaseModel):
    page: str = Field(description="Page, screen, endpoint or command")
    purpose: str


class BriefStage(BaseModel):
    title: str
    tasks: list[str]


class ProjectBriefDraft(BaseModel):
    audience: list[str] = Field(description="Who it is for (2 to 4 groups)")
    value: str = Field(description="One sentence: why they would use it")
    features: list[BriefFeature]
    pages: list[BriefPage]
    stages: list[BriefStage]
    acceptance: list[str] = Field(description="How to tell it is done (checkable statements)")
    risks: list[str]


PLAN_DOC_SYSTEM = """You are the Planner Agent of JARVIS, a personal AI assistant on the user's Windows computer.
Write a realistic, specific plan for the user's goal: phases in order, each with a duration in days and concrete
tasks (one action each, with a priority and an owner). Owners are JARVIS's agents (research: web research and
comparisons; system: files, programs and computer settings; development: code, apps and websites; creative: design,
texts and presentations; planner: planning and tracking) or "user" for what only the user can do (decisions,
payments, meetings, physical work). Prefer fewer, meaningful tasks over long generic lists. Base durations on the
goal's real size and any deadline the user gave. Write everything in the user's language."""

BRIEF_SYSTEM = """You are the Planner Agent of JARVIS. Write a short, concrete project brief that the Creative and
Development agents will build from. Be specific to the user's request; avoid generic filler. Write in the user's
language."""


# ------------------------------------------------------------------------------------------ project briefs
KIND_PAGES: dict[str, list[tuple[str, str, str]]] = {
    "website": [("index.html", "עמוד הבית: מסר מרכזי, יתרונות והנעה לפעולה", "Home: key message, benefits and a call to action"),
                ("about.html", "אודות: הסיפור, הערכים והצוות", "About: the story, values and team"),
                ("services.html", "שירותים ומוצרים", "Services and products"),
                ("contact.html", "יצירת קשר עם טופס ופרטים", "Contact form and details")],
    "store": [("index.html", "קטלוג עם חיפוש, סינון לפי קטגוריה ומיון", "Catalog with search, category filters and sorting"),
              ("product.html", "עמוד מוצר עם מלאי, דירוג, הוספה לסל ומוצרים קשורים",
               "Product page with stock, rating, add to cart and related products"),
              ("cart.html", "סל קניות עם כמויות, מע\"מ ומשלוח", "Cart with quantities, tax and shipping"),
              ("checkout.html", "קופה: פרטי לקוח, בדיקת שדות ואישור הזמנה", "Checkout: customer details, validation and "
                                                                          "order confirmation")],
    "webapp": [("index.html", "האפליקציה: רשימה, סינון, עריכה, ייצוא ושמירה מקומית",
                "The app: list, filters, editing, export and local storage")],
    "game": [("index.html", "המשחק: לוח, ניקוד, שלבים, שיא ושליטה במקלדת ובמגע",
              "The game: board, score, levels, high score, keyboard and touch controls")],
    "api": [("GET/POST /items", "רשימה ויצירה של פריטים עם חיפוש, תגיות ועימוד", "List and create items with search, tags "
                                                                                  "and paging"),
            ("GET/PATCH/DELETE /items/{id}", "קריאה, עדכון ומחיקה של פריט", "Read, update and delete one item"),
            ("GET /health", "בדיקת תקינות", "Health check"), ("/docs", "תיעוד אינטראקטיבי", "Interactive API docs")],
    "node-api": [("GET/POST /api/items", "רשימה ויצירה של פריטים", "List and create items"),
                 ("GET/PUT/DELETE /api/items/:id", "פריט בודד", "One item"), ("GET /health", "בדיקת תקינות", "Health check")],
    "bot": [("/start, /help", "פתיחה והסבר", "Welcome and help"), ("/todo, /list, /done, /clear", "רשימת משימות אישית",
                                                                   "A personal to-do list"),
            ("/time, /echo", "פקודות עזר", "Utility commands")],
    "script": [("stats", "סטטיסטיקה של תיקייה לפי סוגי קבצים", "Folder statistics by file type"),
               ("dupes", "איתור קבצים כפולים", "Find duplicate files"), ("words", "המילים הנפוצות בקבצי טקסט",
                                                                          "Most common words in text files")],
}
KIND_FEATURES: dict[str, list[tuple[str, str]]] = {
    "website": [("עמודים רספונסיביים בעברית ובאנגלית (RTL/LTR)", "Responsive pages in Hebrew or English (RTL/LTR)"),
                ("טופס יצירת קשר עם בדיקת שדות", "Contact form with field validation"),
                ("תפריט נייד", "Mobile menu")],
    "store": [("חיפוש, סינון ומיון מוצרים", "Product search, filters and sorting"),
              ("סל שנשמר בדפדפן", "Cart saved in the browser"),
              ("חישוב מע\"מ ומשלוח חינם מעל סכום", "Tax and free-shipping threshold"),
              ("קופה עם מספר הזמנה (הדגמה, בלי תשלום אמיתי)", "Checkout with an order number (demo, no real payment)")],
    "webapp": [("משימות עם עדיפות ותאריך יעד", "Tasks with priorities and due dates"),
               ("סינון, מיון חכם וקיצורי מקלדת", "Filters, smart sort and keyboard shortcuts"),
               ("ייצוא וייבוא JSON", "JSON export and import"), ("מצב כהה/בהיר", "Dark and light theme")],
    "game": [("שלבים ומהירות עולה", "Levels with rising speed"), ("שיא שנשמר", "Saved high score"),
             ("שליטה במקלדת, החלקה וכפתורים", "Keyboard, swipe and on-screen controls")],
    "api": [("CRUD מלא עם ולידציה", "Full CRUD with validation"), ("שמירה בקובץ JSON", "JSON file storage"),
            ("תיעוד OpenAPI ובדיקות אוטומטיות", "OpenAPI docs and automated tests")],
    "node-api": [("שרת Node בלי תלויות", "A Node server with no dependencies"), ("ולידציה ושמירה בקובץ", "Validation and "
                                                                                                      "file storage"),
                 ("בדיקות אוטומטיות", "Automated tests")],
    "bot": [("בוט טלגרם עם פקודות", "Telegram bot with commands"), ("רשימת משימות לכל משתמש", "A to-do list per user"),
            ("הטוקן נשמר רק בקובץ ‎.env", "The token lives only in .env")],
    "script": [("כלי שורת פקודה ב-Python", "A Python command-line tool"), ("פלט JSON", "JSON output"),
               ("בדיקות אוטומטיות", "Automated tests")],
}
WEB_KINDS = ("website", "store", "webapp", "game")


def template_kind(kind: str | None) -> str:
    value = (kind or "webapp").strip().lower()
    if value in KIND_PAGES:
        return value
    return TEMPLATE_FOR_KIND.get(value, "webapp")


def acceptance_for(kind: str, lang: str) -> list[str]:
    he = lang == "he"
    if kind in WEB_KINDS:
        items = [("כל עמוד נפתח בלי שגיאות וכל הקישורים והתמונות עובדים", "Every page opens without errors and every "
                                                                          "link and image works"),
                 ("העמודים מתאימים למסך טלפון", "Pages fit a phone screen"),
                 ("הבדיקות האוטומטיות עוברות", "The automated tests pass")]
        if kind == "store":
            items += [("סכום הסל כולל מע\"מ ומשלוח נכון", "The cart total includes tax and shipping correctly"),
                      ("הקופה בודקת שדות ומציגה מספר הזמנה", "Checkout validates fields and shows an order number")]
    elif kind in ("api", "node-api"):
        items = [("הבדיקות האוטומטיות עוברות", "The automated tests pass"),
                 ("בדיקת התקינות מחזירה ok", "The health check returns ok"),
                 ("יצירה, קריאה, עדכון ומחיקה עובדים", "Create, read, update and delete work")]
    elif kind == "bot":
        items = [("הבדיקות האוטומטיות עוברות", "The automated tests pass"),
                 ("הבוט עונה ל-/start אחרי הוספת הטוקן", "The bot answers /start once the token is added")]
    else:
        items = [("הבדיקות האוטומטיות עוברות", "The automated tests pass"),
                 ("‎--help מציג הוראות שימוש", "--help shows how to use it")]
    return [he_text if he else en_text for he_text, en_text in items]


def build_stages(kind: str, lang: str) -> list[dict[str, Any]]:
    he = lang == "he"
    web = kind in WEB_KINDS
    stages = [
        (("תכנון", "Plan"), "planner", [("מטרות, קהל יעד ותכונות", "Goals, audience and features")]),
        (("עיצוב", "Design"), "creative", [("פלטת צבעים, לוגו ואייקונים", "Palette, logo and icons")]
         + ([("טקסטים לעמודים", "Page texts")] if web else [])),
        (("פיתוח", "Build"), "development", [("יצירת הפרויקט מהתבנית", "Create the project from the template"),
                                            ("התאמת תוכן, נתונים וצבעים", "Fit content, data and colors")]),
        (("בדיקות", "Test"), "development", [("הרצת הבדיקות האוטומטיות", "Run the automated tests")]
         + ([("בדיקת קישורים ועמודים", "Check links and pages")] if web else [])),
        (("השקה", "Launch"), "system", [("פתיחה לצפייה", "Open it to review")]
         + ([("העלאה לאחסון (אופציונלי)", "Publish to hosting (optional)")] if web else [])),
    ]
    return [{"title": title[0] if he else title[1], "agent": agent,
             "tasks": [he_text if he else en_text for he_text, en_text in tasks]}
            for title, agent, tasks in stages]


def offline_brief(goal: str, kind: str, name: str, lang: str) -> dict[str, Any]:
    """A project brief from the template and the built-in domain knowledge (no AI model)."""
    from jarvis.creative.texts import landing_copy

    copy = landing_copy(goal or name, lang=lang, name=name)
    he = lang == "he"
    features = [{"title": f["title"], "text": f["text"], "must": i < 2}
                for i, f in enumerate(copy.get("features") or [])]  # type: ignore[union-attr]
    features += [{"title": he_t if he else en_t, "text": "", "must": True} for he_t, en_t in KIND_FEATURES.get(kind, [])]
    pages = [{"page": page, "purpose": he_p if he else en_p} for page, he_p, en_p in KIND_PAGES.get(kind, [])]
    risks = [t(lang, "תוכן אמיתי (תמונות, מחירים, טקסטים) צריך להחליף את תוכן הדוגמה לפני פרסום.",
               "Real content (photos, prices, texts) must replace the sample content before publishing.")]
    if kind == "store":
        risks.append(t(lang, "תשלום אמיתי דורש ספק סליקה (למשל Stripe או PayPal) ואישור משפטי של התקנון.",
                       "Real payments need a payment provider (e.g. Stripe or PayPal) and reviewed terms."))
    if kind == "bot":
        risks.append(t(lang, "צריך טוקן מ-BotFather ב-Telegram; הוא נשמר רק בקובץ ‎.env.",
                       "A token from Telegram's BotFather is needed; it is kept only in .env."))
    return {
        "name": name, "kind": kind, "goal": goal, "audience": list(copy.get("audience") or []),  # type: ignore[arg-type]
        "value": str(copy.get("subheadline") or ""), "features": features, "pages": pages,
        "stages": build_stages(kind, lang), "acceptance": acceptance_for(kind, lang), "risks": risks,
        "source": "template",
    }


def brief_markdown(brief: dict[str, Any], lang: str) -> str:
    he = lang == "he"
    kind_label = KIND_NAMES.get(brief["kind"], ("פרויקט", "Project"))[0 if he else 1]
    lines = [f"# {brief['name']}", "", f"**{'סוג' if he else 'Type'}:** {kind_label}", ""]
    if brief.get("goal"):
        lines += [f"**{'הבקשה' if he else 'Request'}:** {brief['goal']}", ""]
    if brief.get("value"):
        lines += [brief["value"], ""]
    if brief.get("audience"):
        lines += ["## " + ("קהל יעד" if he else "Audience"), "", *[f"- {a}" for a in brief["audience"]], ""]
    must = [f for f in brief.get("features", []) if f.get("must")]
    nice = [f for f in brief.get("features", []) if not f.get("must")]
    lines += ["## " + ("תכונות" if he else "Features"), ""]
    for label, group in ((("חובה", "Must have"), must), (("רצוי", "Nice to have"), nice)):
        if group:
            lines += [f"**{label[0] if he else label[1]}**", ""]
            lines += [f"- {f['title']}" + (f": {f['text']}" if f.get("text") else "") for f in group]
            lines.append("")
    if brief.get("pages"):
        lines += ["## " + ("עמודים / מסכים" if he else "Pages / screens"), "",
                  *[f"- `{p['page']}` — {p['purpose']}" for p in brief["pages"]], ""]
    if brief.get("stages"):
        lines += ["## " + ("שלבים" if he else "Stages"), ""]
        for i, stage in enumerate(brief["stages"], 1):
            owner = documents.AGENT_LABEL.get(stage.get("agent") or "", (None, None))[0 if he else 1]
            lines.append(f"{i}. **{stage['title']}**" + (f" ({owner})" if owner else ""))
            lines += [f"   - {task}" for task in stage.get("tasks", [])]
        lines.append("")
    if brief.get("acceptance"):
        lines += ["## " + ("מתי זה גמור" if he else "Done when"), "", *[f"- [ ] {a}" for a in brief["acceptance"]], ""]
    if brief.get("risks"):
        lines += ["## " + ("סיכונים" if he else "Risks"), "", *[f"- {r}" for r in brief["risks"]], ""]
    return "\n".join(lines).rstrip() + "\n"


# ------------------------------------------------------------------------------------------ plan documents
def draft_to_plan(draft: PlanDocDraft, goal: str, lang: str, start: date | None = None) -> dict[str, Any]:
    phases = documents.schedule([{
        "title": phase.title.strip() or f"{t(lang, 'שלב', 'Phase')} {i}",
        "days": min(max(int(phase.days or 1), 1), 365),
        "tasks": [{"title": task.title.strip(), "priority": task.priority,
                   "agent": task.owner if task.owner != "user" else None, "owner": task.owner}
                  for task in phase.tasks if task.title.strip()],
    } for i, phase in enumerate(draft.phases, 1)], start)
    if not phases:
        raise PlanError("The plan has no phases")
    first = [a.strip() for a in draft.first_actions if a.strip()][:3] or \
        [task["title"] for p in phases for task in p["tasks"]][:1]
    return {"goal": goal, "title": draft.title.strip() or goal, "summary": draft.summary.strip(), "domain": "ai",
            "phases": phases, "first_actions": first, "risks": [r for r in draft.risks if r.strip()][:8],
            "missing": [m for m in draft.missing_information if m.strip()][:6], "finish": phases[-1]["end"],
            "source": "ai"}


_ISO_DATE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_DMY_DATE = re.compile(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b")


def parse_start(value: str | None) -> date | None:
    """A start date from '2026-11-01', '1.11', '1/11/2026', 'tomorrow' or 'מחר'."""
    if not value:
        return None
    text = value.strip().lower()
    if text in ("tomorrow", "מחר"):
        return date.today() + timedelta(days=1)
    if text in ("today", "היום", "now", "עכשיו"):
        return date.today()
    try:
        if m := _ISO_DATE.search(text):
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if m := _DMY_DATE.search(text):
            year = int(m.group(3)) if m.group(3) else date.today().year
            year = year + 2000 if year < 100 else year
            return date(year, int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None
    return None


# ------------------------------------------------------------------------------------------ progress
ACTIVE = ("pending", "planning", "queued", "running", "waiting", "verifying")


def progress_report(tasks: list[dict[str, Any]], lang: str) -> dict[str, Any]:
    """Counts, blockers and next steps from the task history (see the task_history tool)."""
    he = lang == "he"
    counts = Counter(str(task.get("status")) for task in tasks)
    active = [task for task in tasks if task.get("status") in ACTIVE]
    failed = [task for task in tasks if task.get("status") == "failed"]
    interrupted = [task for task in tasks if task.get("status") == "interrupted"]
    waiting = [task for task in tasks if task.get("status") == "waiting"]
    done = [task for task in tasks if task.get("status") == "completed"]
    blockers: list[str] = []
    for task in [*active, *failed, *interrupted]:
        for step in task.get("steps") or []:
            if step.get("status") == "failed" and not step.get("optional"):
                reason = f" — {step['error']}" if step.get("error") else ""
                blockers.append(f"{task['title']}: {step.get('title')}{reason}")
        if task.get("status") == "failed" and not any(b.startswith(f"{task['title']}:") for b in blockers):
            blockers.append(f"{task['title']}" + (f" — {task['error']}" if task.get("error") else ""))
    next_steps: list[str] = []
    for task in waiting[:3]:
        next_steps.append(f"לאשר או לדחות את הפעולה שממתינה במשימה '{task['title']}'" if he
                          else f"Approve or reject the action waiting in '{task['title']}'")
    for task in interrupted[:3]:
        next_steps.append(f"להמשיך את המשימה שנקטעה: '{task['title']}'" if he
                          else f"Resume the interrupted task '{task['title']}'")
    for task in failed[:3]:
        next_steps.append(f"לבדוק ולנסות שוב: '{task['title']}'" if he else f"Check and retry '{task['title']}'")
    labels_he = {"completed": "הושלמו", "failed": "נכשלו", "running": "רצות", "waiting": "ממתינות לאישור",
                 "interrupted": "נקטעו", "cancelled": "בוטלו", "pending": "בהמתנה", "planning": "בתכנון",
                 "queued": "בתור", "verifying": "בבדיקה"}
    parts = [f"{labels_he.get(s, s)} {n}" if he else f"{n} {s}" for s, n in counts.most_common()]
    if not tasks:
        summary = t(lang, "אין עדיין משימות להציג.", "There are no tasks yet.")
    else:
        summary = plural(lang, len(tasks), "משימה אחת", "משימות", "task", "tasks") + ": " + ", ".join(parts) + "."
        if blockers:
            summary += " " + t(lang, f"חסמים: {len(blockers)}.", f"Blockers: {len(blockers)}.")
    lines = [f"# {'סקירת התקדמות' if he else 'Progress review'} — {datetime.now():%Y-%m-%d %H:%M}", "", summary, ""]
    if active:
        lines += ["## " + ("בעבודה" if he else "In progress"), ""]
        lines += [f"- {task['title']} ({task['status']}"
                  + (f", {round(float(task['progress']) * 100)}%" if task.get("progress") is not None else "") + ")"
                  for task in active]
        lines.append("")
    if blockers:
        lines += ["## " + ("חסמים" if he else "Blockers"), "", *[f"- {b}" for b in blockers], ""]
    if done:
        lines += ["## " + ("הושלמו לאחרונה" if he else "Recently completed"), ""]
        lines += [f"- {task['title']}" + (f": {task['summary'][:160]}" if task.get("summary") else "")
                  for task in done[:8]]
        lines.append("")
    if next_steps:
        lines += ["## " + ("הצעדים הבאים" if he else "Next steps"), "", *[f"{i}. {s}" for i, s in
                                                                         enumerate(next_steps, 1)], ""]
    return {"summary": summary, "markdown": "\n".join(lines).rstrip() + "\n", "counts": dict(counts),
            "blockers": blockers, "next_steps": next_steps, "active": len(active)}


# ------------------------------------------------------------------------------------------ folders
PROJECT_MARKERS = {"package.json", "pyproject.toml", "setup.py", "requirements.txt", ".git", "pom.xml", "build.gradle",
                   "cargo.toml", "go.mod", "composer.json", "gemfile"}
LARGE_BYTES = 200 * 1024 * 1024


def analyze_folder(entries: list[dict[str, Any]], lang: str) -> dict[str, Any]:
    """What is in a folder (from a recursive list_dir) and how it could be tidied."""
    from jarvis.tools.organize import category_for

    files = [e for e in entries if not e.get("is_dir")]
    folders = [e for e in entries if e.get("is_dir")]

    def parts(entry: dict[str, Any]) -> list[str]:
        return [p for p in re.split(r"[\\/]", str(entry.get("name") or "")) if p]

    top_names = {parts(e)[0].lower() for e in entries if len(parts(e)) == 1}
    is_project = bool(top_names & PROJECT_MARKERS) or any(n.endswith((".sln", ".csproj")) for n in top_names)
    projects = sorted({"/".join(parts(e)[:-1]) for e in entries
                       if len(parts(e)) > 1 and parts(e)[-1].lower() in PROJECT_MARKERS})
    projects = [p for p in projects if not any(p != q and p.startswith(q + "/") for q in projects)]
    loose = [e for e in files if len(parts(e)) == 1]
    groups = Counter(category_for(Path(str(e["name"])), lang) for e in loose)
    large = sorted(({"name": e["name"], "size_mb": round((e.get("size") or 0) / 1048576, 1)} for e in files
                    if (e.get("size") or 0) >= LARGE_BYTES), key=lambda x: -x["size_mb"])[:10]
    year_ago = (datetime.now() - timedelta(days=365)).isoformat(timespec="seconds")
    old = [e for e in loose if e.get("modified") and str(e["modified"]) < year_ago]
    seen: dict[tuple[str, int | None], list[str]] = {}
    for e in files:
        if (e.get("size") or 0) > 0:
            seen.setdefault((parts(e)[-1].lower(), e.get("size")), []).append(str(e["name"]))
    duplicates = [names for names in seen.values() if len(names) > 1][:10]
    suggestions: list[str] = []
    if is_project:
        suggestions.append(t(lang, "זו תיקיית פרויקט תוכנה: לא כדאי להעביר בה קבצים לפי סוג, כי זה ישבור את הפרויקט.",
                             "This is a software project: moving its files by type would break it."))
        misplaced = [e["name"] for e in loose if category_for(Path(str(e["name"])), "en") in
                     ("Images", "Video", "Audio", "Archives", "Installers")]
        if misplaced:
            k = len(misplaced)
            suggestions.append(t(lang, f"אפשר להעביר {plural(lang, k, 'קובץ מדיה/ארכיון אחד', 'קבצי מדיה/ארכיון', '', '')} "
                                       "מהשורש לתיקיית assets.",
                                 f"{plural(lang, k, '', '', 'media/archive file', 'media/archive files')} at the root "
                                 "could move into an assets folder."))
    elif loose:
        listing = ", ".join(f"{name} ({n})" for name, n in groups.most_common())
        k = len(loose)
        suggestions.append(t(lang, f"לסדר {plural(lang, k, 'קובץ פזור אחד', 'קבצים פזורים', '', '')} בתיקיות לפי סוג: {listing}.",
                             f"Sort {plural(lang, k, '', '', 'loose file', 'loose files')} into folders by type: {listing}."))
    if projects:
        k = len(projects)
        suggestions.append(t(lang, "נמצא פרויקט אחד בתוך התיקייה; הוא יישאר כמו שהוא." if k == 1
                             else f"נמצאו {k} פרויקטים בתוך התיקייה; הם יישארו כמו שהם.",
                             "Found 1 project folder inside; it stays as it is." if k == 1
                             else f"Found {k} project folders inside; they stay as they are."))
    if old:
        k = len(old)
        suggestions.append(t(lang, "קובץ אחד לא שונה יותר משנה; אפשר להעביר אותו לתיקיית ארכיון." if k == 1
                             else f"{k} קבצים לא שונו יותר משנה; אפשר להעביר אותם לתיקיית ארכיון.",
                             "1 file has not changed in over a year; it could go to an archive folder." if k == 1
                             else f"{k} files have not changed in over a year; they could go to an archive folder."))
    if duplicates:
        k = len(duplicates)
        suggestions.append(t(lang, "קובץ אחד מופיע יותר מפעם אחת (אותו שם וגודל); כדאי לבדוק." if k == 1
                             else f"{k} קבצים מופיעים יותר מפעם אחת (אותו שם וגודל); כדאי לבדוק.",
                             "1 file appears more than once (same name and size); worth checking." if k == 1
                             else f"{k} files appear more than once (same name and size); worth checking."))
    if large:
        k = len(large)
        suggestions.append(t(lang, "קובץ אחד גדול מ-200MB תופס מקום." if k == 1 else f"{k} קבצים גדולים מ-200MB תופסים מקום.",
                             "1 file over 200 MB takes up space." if k == 1 else f"{k} files over 200 MB take up space."))
    if not suggestions:
        suggestions.append(t(lang, "התיקייה כבר מסודרת.", "The folder is already tidy."))
    return {"files": len(files), "folders": len(folders), "loose": len(loose), "groups": dict(groups),
            "is_project": is_project, "projects": projects[:20], "large": large, "old": len(old),
            "duplicates": duplicates, "suggestions": suggestions}


# ------------------------------------------------------------------------------------------ the agent
class PlannerAgent(BaseAgent):
    id = AgentId.PLANNER
    persona = (
        "Your job: turn goals into clear, realistic plans and keep them on track. Break work into phases and "
        "concrete tasks, set priorities and owners (one of the five agents, or the user), estimate durations, "
        "follow progress, find blockers and propose the next step. Plans must fit the user's actual situation, not "
        "a generic template. Save plans as Markdown files under workspace/plans so the user can open them."
    )
    llm_tools = ("list_dir", "read_file", "write_file", "find_files", "file_info", "recall", "remember",
                 "project_info", "task_history", "system_info")

    # ------------------------------------------------------------------ task decomposition
    @skill(description="Split a goal into steps for the five agents (the plan JARVIS runs)")
    async def decompose(self, task: AgentTask, run: RunContext, goal: str = "", intent: str | None = None,
                        slots: dict[str, Any] | None = None, context: str = "", max_steps: int = 8) -> AgentResult:
        goal = (goal or task.instruction).strip()
        lang = task.language
        notes: list[str] = []
        await run.progress(0.1, "planning")
        plan = None
        try:
            plan = rule_plan(intent, dict(slots or {}), goal, lang, ai=self.ai_available)
        except PlanError as exc:
            notes.append(exc.message)
        used_llm = False
        if plan is None and task.allow_llm and self.ai_available and self.runtime.router is not None:
            await run.phase(AgentStatus.THINKING)
            try:
                plan = await run.guard(llm_plan(self.runtime.router, goal, lang, context=context,
                                                max_steps=max(1, min(int(max_steps), 12))))
                used_llm = True
            except ProviderRefusal:
                raise
            except (ProviderError, PlanError) as exc:
                notes.append(exc.message)
                await run.say(t(lang, f"תכנון בעזרת מודל ה-AI נכשל ({exc.message}); משתמש בתוכנית פשוטה.",
                                f"Planning with the AI model failed ({exc.message}); using a simple plan."),
                              level="warning")
        if plan is None:
            plan = fallback_plan(goal, lang)
        await run.progress(0.9)
        data = plan.model_dump(mode="json")
        names = ", ".join(AGENT_INFO[a]["name_he" if lang == "he" else "name"] for a in plan.agents)
        k = len(plan.subtasks)
        summary = t(lang, f"תוכנית {'בשלב אחד' if k == 1 else f'ב-{k} שלבים'}: {names}.", f"A {k}-step plan: {names}.")
        return run.result(summary=summary, details=plan.strategy or None, result=data,
                          data={"plan": data, "source": plan.source, "notes": notes}, used_llm=used_llm)

    # ------------------------------------------------------------------ plans
    @skill(description="Write a plan: phases with dates, prioritized tasks with owners, first steps and risks",
           intents=(I.PLAN,))
    async def make_plan(self, task: AgentTask, run: RunContext, goal: str = "", save: bool = True,
                        start: str | None = None) -> AgentResult:
        goal = (goal or task.instruction).strip()
        lang = task.language
        begin = parse_start(start) or parse_start(_find_date(goal))
        doc: dict[str, Any] | None = None
        used_llm = False
        if task.allow_llm and self.ai_available and self.runtime.router is not None:
            await run.phase(AgentStatus.THINKING)
            try:
                draft = await run.guard(self.runtime.router.structured(
                    "planner", self._plan_prompt(goal, task, begin), PlanDocDraft, system=PLAN_DOC_SYSTEM))
                doc, used_llm = draft_to_plan(draft, goal, lang, begin), True
            except ProviderRefusal:
                raise
            except (ProviderError, PlanError) as exc:
                await run.say(t(lang, f"מודל ה-AI לא זמין ({exc.message}); בונה תוכנית מתבנית.",
                                f"The AI model is unavailable ({exc.message}); building the plan from a template."),
                              level="warning")
        if doc is None:
            doc = documents.build_plan(goal, lang, start=begin)
        await run.progress(0.7)
        text = documents.plan_markdown(doc, lang)
        saved = await self._save(run, f"workspace/plans/{datetime.now():%Y%m%d}-{slugify(goal, 40, 'plan')}.md",
                                 text) if save else None
        phases = doc["phases"]
        first = doc["first_actions"][0] if doc.get("first_actions") else ""
        stages_he = "בשלב אחד" if len(phases) == 1 else f"ב-{len(phases)} שלבים"
        summary = t(lang, f"הכנתי תוכנית {stages_he} עד {doc['finish']}." + (f" להתחיל ב: {first}." if first else ""),
                    f"Made a {len(phases)}-phase plan through {doc['finish']}." + (f" Start with: {first}." if first else ""))
        if saved:
            summary += t(lang, f" נשמרה ב-{saved}", f" Saved to {saved}")
        missing = doc.get("missing") or []
        return run.result(
            status=ResultStatus.SUCCESS if saved or not save else ResultStatus.PARTIAL, summary=summary, details=text,
            result={"plan": doc, "path": saved}, used_llm=used_llm,
            next_step=t(lang, "כדי לדייק את התוכנית: " + ", ".join(missing), "To sharpen the plan, add: "
                        + ", ".join(missing)) if missing else None,
        )

    def _plan_prompt(self, goal: str, task: AgentTask, start: date | None) -> str:
        lines = [f"The user's goal: {goal}", f"Start date: {(start or date.today()).isoformat()}",
                 f"Write in {'Hebrew' if task.language == 'he' else 'English'}."]
        if task.context.get("memory"):
            lines.append(f"What JARVIS knows about the user:\n{str(task.context['memory'])[:2000]}")
        if task.context.get("dependencies"):
            lines.append("Results from other agents:\n" + "\n".join(str(d)[:1500] for d in task.context["dependencies"]))
        return "\n\n".join(lines)

    @skill(description="Project brief for a build: audience, features, pages, stages and acceptance criteria")
    async def project_brief(self, task: AgentTask, run: RunContext, goal: str = "", kind: str = "webapp",
                            name: str = "", save: bool = True) -> AgentResult:
        goal = (goal or task.instruction).strip()
        lang = task.language
        kind = template_kind(kind)
        name = (name or project_name(goal, kind, lang)).strip()
        brief: dict[str, Any] | None = None
        used_llm = False
        if task.allow_llm and self.ai_available and self.runtime.router is not None:
            await run.phase(AgentStatus.THINKING)
            base = offline_brief(goal, kind, name, lang)
            prompt = (f"Request: {goal}\nProject name: {name}\nProject type: {kind}\n"
                      f"It will be generated from JARVIS's '{kind}' template, which already has: "
                      + "; ".join(f["title"] for f in base["features"] if f["must"])
                      + "\nTemplate pages: " + "; ".join(f"{p['page']} ({p['purpose']})" for p in base["pages"])
                      + "\nKeep to these pages unless the request clearly needs more. "
                      f"Write in {'Hebrew' if lang == 'he' else 'English'}.")
            try:
                draft = await run.guard(self.runtime.router.structured("planner", prompt, ProjectBriefDraft,
                                                                       system=BRIEF_SYSTEM))
                brief = {**base, "audience": draft.audience[:6] or base["audience"], "value": draft.value or base["value"],
                         "features": [f.model_dump() for f in draft.features][:16] or base["features"],
                         "pages": [p.model_dump() for p in draft.pages][:16] or base["pages"],
                         "stages": [{"title": s.title, "agent": None, "tasks": s.tasks} for s in draft.stages][:8]
                         or base["stages"],
                         "acceptance": draft.acceptance[:10] or base["acceptance"], "risks": draft.risks[:8] or base["risks"],
                         "source": "ai"}
                used_llm = True
            except ProviderRefusal:
                raise
            except ProviderError as exc:
                await run.say(t(lang, f"מודל ה-AI לא זמין ({exc.message}); כותב את מסמך הפרויקט מהתבנית.",
                                f"The AI model is unavailable ({exc.message}); writing the brief from the template."),
                              level="warning")
        if brief is None:
            brief = offline_brief(goal, kind, name, lang)
        text = brief_markdown(brief, lang)
        saved = await self._save(run, f"workspace/plans/{slugify(name, 40, 'project')}-brief.md", text) if save else None
        must = sum(1 for f in brief["features"] if f.get("must"))
        pages, stages = len(brief["pages"]), len(brief["stages"])
        summary = t(lang, f"מסמך הפרויקט '{name}': {plural(lang, must, 'תכונת חובה אחת', 'תכונות חובה', '', '')}, "
                          f"{plural(lang, pages, 'עמוד/מסך אחד', 'עמודים/מסכים', '', '')}, "
                          f"{plural(lang, stages, 'שלב אחד', 'שלבים', '', '')}.",
                    f"Brief for '{name}': {plural(lang, must, '', '', 'must-have feature', 'must-have features')}, "
                    f"{plural(lang, pages, '', '', 'page/screen', 'pages/screens')}, "
                    f"{plural(lang, stages, '', '', 'stage', 'stages')}.")
        return run.result(status=ResultStatus.SUCCESS if saved or not save else ResultStatus.PARTIAL, summary=summary,
                          details=text, result={**brief, "path": saved}, used_llm=used_llm)

    # ------------------------------------------------------------------ tracking
    @skill(description="Progress review: what is done, running, blocked or failed, and what to do next")
    async def review_progress(self, task: AgentTask, run: RunContext, query: str = "", limit: int = 20,
                              save: bool = False) -> AgentResult:
        lang = task.language
        res = await run.tool("task_history", query=query, limit=max(1, min(int(limit), 100)))
        if not res.ok:
            return AgentResult.failed(t(lang, f"לא הצלחתי לקרוא את היסטוריית המשימות: {res.error}",
                                        f"Could not read the task history: {res.error}"))
        tasks = [x for x in (res.data or {}).get("tasks", []) if x.get("id") != task.task_id]
        report = progress_report(tasks, lang)
        saved = await self._save(run, f"workspace/plans/progress-{datetime.now():%Y%m%d-%H%M}.md",
                                 report["markdown"]) if save else None
        return run.result(summary=report["summary"], details=report["markdown"],
                          result={**{k: v for k, v in report.items() if k != "markdown"}, "path": saved},
                          next_step=report["next_steps"][0] if report["next_steps"] else None)

    @skill(description="Analyze a folder and propose a tidy structure (moves nothing)")
    async def folder_review(self, task: AgentTask, run: RunContext, path: str = "", project: str | None = None,
                            location: str | None = None) -> AgentResult:
        lang = task.language
        target = await resolve_folder(self.runtime, path or project or location, default="workspace")
        res = await run.tool("list_dir", path=target, recursive=True, max_entries=2000)
        if not res.ok:
            return AgentResult.failed(t(lang, f"לא הצלחתי לקרוא את התיקייה: {res.error}",
                                        f"Could not read the folder: {res.error}"), data={"path": target})
        data = res.data or {}
        analysis = analyze_folder(data.get("entries") or [], lang)
        analysis["path"] = data.get("path", target)
        analysis["truncated"] = bool(data.get("truncated"))
        head = t(lang, f"בתיקייה {analysis['files']} קבצים ו-{analysis['folders']} תיקיות.",
                 f"The folder has {analysis['files']} files and {analysis['folders']} folders.")
        details = "\n".join([head, *[f"- {s}" for s in analysis["suggestions"]]])
        return run.result(summary=f"{head} {analysis['suggestions'][0]}", details=details, result=analysis)

    # ------------------------------------------------------------------ helpers
    async def _save(self, run: RunContext, path: str, content: str) -> str | None:
        res = await run.tool("write_file", path=path, content=content)
        if not res.ok:
            return None
        return str((res.data or {}).get("path") or path)

    def pick_skill(self, task: AgentTask) -> tuple[str, dict[str, Any]] | None:
        picked = super().pick_skill(task)
        if picked is not None:
            return picked
        # Without an AI model any planning instruction becomes a plan document.
        return "make_plan", {"goal": task.params.get("goal") or task.instruction}


_DATE_WORDS = re.compile(r"(?:from|starting|start(?:ing)? on|מתאריך|החל מ-?|מ-)\s*(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[./]\d{1,2}"
                         r"(?:[./]\d{2,4})?|tomorrow|מחר)", re.I)


def _find_date(text: str) -> str | None:
    m = _DATE_WORDS.search(text or "")
    return m.group(1) if m else None


__all__ = ["PlannerAgent", "analyze_folder", "brief_markdown", "offline_brief", "progress_report"]
