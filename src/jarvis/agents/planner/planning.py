"""How JARVIS splits a goal between the five agents.

* Rule-based plans for the request types JARVIS knows well (build an app or site, research,
  design, write, code, plan, organize) — deterministic and offline.
* An LLM plan (structured output) for anything else when an AI provider is configured.
* A single-agent fallback plan when neither applies.
"""

from __future__ import annotations

import re
from typing import Any

from jarvis.agents.base import AGENT_INFO, t
from jarvis.agents.planner.documents import request_topic
from jarvis.common.enums import AgentId, Priority
from jarvis.common.text import normalize
from jarvis.core.intents import I
from jarvis.orchestration.models import PlanDraft, SubTask, TaskPlan, plan_from_draft

A = AgentId
TEMPLATE_FOR_KIND = {"website": "website", "store": "store", "app": "webapp", "game": "game", "api": "api",
                     "bot": "bot"}
KIND_NAMES = {
    "website": ("אתר", "Website"), "store": ("חנות אונליין", "Online store"), "webapp": ("אפליקציית ווב", "Web app"),
    "game": ("משחק", "Game"), "api": ("שרת API", "API service"), "bot": ("בוט", "Bot"),
}
# Hebrew needs the definite form ("בניית אפליקציית הווב", not "בניית האפליקציית ווב") and the gender (מלא / מלאה).
KIND_DEFINITE_HE = {"website": "האתר", "store": "החנות", "webapp": "אפליקציית הווב", "game": "המשחק",
                    "api": "שרת ה-API", "bot": "הבוט"}
KIND_FEMININE_HE = frozenset({"store", "webapp"})
_NAME_STRIP = re.compile(
    r"^(?:jarvis[,\s]*)?(?:please\s+)?(?:build|create|make|develop|code|write|generate|set up|scaffold)\s+(?:me\s+|us\s+)?"
    r"(?:a\s+|an\s+|the\s+|my\s+|new\s+|simple\s+|small\s+|full\s+|complete\s+)*|"
    r"^(?:ג'ארוויס[,\s]*)?(?:תבנה|בנה|תבני|בני|לבנות|תיצור|צור|תכתוב|כתוב|תכין|הכן|תפתח)\s+(?:לי\s+|לנו\s+)?(?:את\s+)?",
    re.I,
)


def project_name(goal: str, template: str, lang: str) -> str:
    """A short, readable project name from the request ("תבנה לי אתר לחנות קפה" → "אתר לחנות קפה")."""
    text = _NAME_STRIP.sub("", goal.strip()).strip(" .,!?:\"'")
    text = re.split(r"\s+(?:with|that|which|using|עם|ש|שיש|שכולל|הכולל)\s+", text, maxsplit=1)[0]
    words = text.split()
    if not words:
        he, en = KIND_NAMES.get(template, ("פרויקט", "Project"))
        return he if lang == "he" else en
    name = " ".join(words[:6])
    return name[:1].upper() + name[1:] if lang != "he" else name


def research_query(goal: str, template: str, lang: str) -> str:
    topic = project_name(goal, template, lang)
    if template == "store":
        return t(lang, f"{topic}: מה חנויות מצליחות כוללות, מחירים מקובלים ושיטות עבודה מומלצות",
                 f"{topic}: what successful online stores include, typical prices and best practices")
    if template in ("website", "webapp"):
        return t(lang, f"{topic}: דוגמאות מוצלחות, תכונות חשובות ושיטות עבודה מומלצות",
                 f"{topic}: good examples, must-have features and best practices")
    if template == "game":
        return t(lang, f"{topic}: מכניקות משחק, רמות קושי וחוויית משתמש", f"{topic}: game mechanics, difficulty and UX")
    return t(lang, f"{topic}: שיטות עבודה מומלצות וספריות נפוצות", f"{topic}: best practices and common libraries")


def _sub(agent: AgentId, key: str, title: str, instruction: str, **kw: Any) -> SubTask:
    return SubTask(agent=agent, key=key, title=title, instruction=instruction, **kw)


# ------------------------------------------------------------------------------------------ rule plans
def rule_plan(intent: str | None, slots: dict[str, Any], goal: str, lang: str, *, ai: bool = False) -> TaskPlan | None:
    if not intent:
        return None
    builder = _RULES.get(intent)
    if builder is None:
        return None
    plan = builder(dict(slots or {}), goal, lang, ai)
    if plan is not None:
        plan.validate_dag()
    return plan


def _build_app(slots: dict[str, Any], goal: str, lang: str, ai: bool) -> TaskPlan:
    template = TEMPLATE_FOR_KIND.get(str(slots.get("kind") or "app"), "webapp")
    name = project_name(goal, template, lang)
    kind_he, kind_en = KIND_NAMES.get(template, ("פרויקט", "project"))
    plan = _sub(A.PLANNER, "plan", t(lang, "תכנון: מטרות, עמודים ושלבים", "Plan: goals, pages and stages"),
                t(lang, f"תכנן את הפרויקט '{name}' ({kind_he}) לפי הבקשה: {goal}. הגדר קהל יעד, תכונות, עמודים ושלבי עבודה.",
                  f"Plan the project '{name}' ({kind_en}) for this request: {goal}. Define the audience, features, "
                  "pages and work stages."),
                skill="project_brief", params={"goal": goal, "kind": template, "name": name}, priority=Priority.HIGH,
                acceptance_criteria=[t(lang, "מסמך תכנון עם תכונות ושלבים", "A brief with features and stages")])
    research = _sub(A.RESEARCH, "research", t(lang, "מחקר: דוגמאות ושיטות עבודה מומלצות", "Research: examples and best practices"),
                    research_query(goal, template, lang), skill="research",
                    params={"query": research_query(goal, template, lang), "save": True}, optional=True)
    design = _sub(A.CREATIVE, "design", t(lang, "עיצוב: פלטת צבעים, לוגו ואייקונים", "Design: palette, logo and icons"),
                  t(lang, f"צור ערכת מיתוג ל'{name}': פלטת צבעים, לוגו ואייקונים שמתאימים ל: {goal}",
                    f"Create a brand kit for '{name}': color palette, logo and icons that fit: {goal}"),
                  skill="brand_kit", params={"brief": goal, "name": name}, depends_on=[plan.id])
    build_deps = [plan.id, design.id] + ([research.id] if ai else [])
    complete_he = "מלאה ועובדת" if template in KIND_FEMININE_HE else "מלא ועובד"
    build = _sub(A.DEVELOPMENT, "build",
                 t(lang, f"פיתוח: בניית {KIND_DEFINITE_HE.get(template, 'הפרויקט')}", f"Build the {kind_en.lower()}"),
                 t(lang, f"בנה {kind_he} {complete_he} בשם '{name}' לפי הבקשה: {goal}. השתמש בתכנון ובעיצוב שהתקבלו.",
                   f"Build a complete, working {kind_en.lower()} called '{name}' for: {goal}. Use the plan and the "
                   "design from the other agents."),
                 skill="create_project", params={"kind": template, "name": name, "description": goal, "language": lang},
                 depends_on=build_deps, priority=Priority.HIGH,
                 acceptance_criteria=[t(lang, "כל הקבצים נוצרו והפרויקט נפתח בלי שגיאות",
                                        "All files exist and the project opens without errors")],
                 verify=[{"kind": "files_exist"}])
    test = _sub(A.DEVELOPMENT, "test", t(lang, "בדיקות ואימות", "Test and verify"),
                t(lang, f"הרץ את הבדיקות של '{name}', בדוק שכל הקישורים והקבצים תקינים ותקן תקלות.",
                  f"Run the tests of '{name}', check that every link and file resolves, and fix problems."),
                skill="verify_project", params={"name": name}, depends_on=[build.id])
    launch = _sub(A.SYSTEM, "launch", t(lang, "הפעלה ופתיחה לצפייה", "Run it and open it"),
                  t(lang, f"פתח את '{name}' כדי שהמשתמש יראה את התוצאה.", f"Open '{name}' so the user can see it."),
                  skill="open_project", params={"name": name}, depends_on=[test.id], optional=True,
                  priority=Priority.LOW)
    return TaskPlan(
        title=t(lang, f"בניית {name}", f"Build {name}"),
        strategy=t(lang, "התכנון והמחקר רצים במקביל; העיצוב נבנה על התכנון; הפיתוח משלב את הכול; ואז בדיקות והפעלה.",
                   "Planning and research run in parallel; design builds on the plan; development combines "
                   "everything; then tests and launch."),
        subtasks=[plan, research, design, build, test, launch], source="rules",
    )


def _single(agent: AgentId, skill_name: str | None, title_he: str, title_en: str,
            param: str | None = None) -> Any:
    def builder(slots: dict[str, Any], goal: str, lang: str, ai: bool) -> TaskPlan:
        params = dict(slots)
        if param and not params.get(param):
            params[param] = goal
        kind = t(lang, title_he, title_en)
        topic = request_topic(goal)[:70] or kind
        # "תוכנית לימודים לשבוע" already says what it is; "לוגו לבית קפה" becomes "עיצוב: לוגו לבית קפה".
        title = topic if normalize(topic).startswith(normalize(kind)) else f"{kind}: {topic}"
        title = title[:1].upper() + title[1:]
        sub = _sub(agent, skill_name or "main", title, goal, skill=skill_name, params=params, priority=Priority.HIGH)
        return TaskPlan(title=title, strategy=t(lang, f"משימה ל{AGENT_INFO[agent]['name_he']}.",
                                                                  f"One step for the {AGENT_INFO[agent]['name']}."),
                        subtasks=[sub], source="rules")

    return builder


def _organize(slots: dict[str, Any], goal: str, lang: str, ai: bool) -> TaskPlan:
    target = slots.get("project") or slots.get("location") or slots.get("path") or "workspace"
    review = _sub(A.PLANNER, "review", t(lang, "ניתוח התיקייה והצעת מבנה", "Analyze the folder and propose a structure"),
                  t(lang, f"נתח את {target} והצע מבנה מסודר.", f"Analyze {target} and propose a tidy structure."),
                  skill="folder_review", params={"path": target})
    move = _sub(A.SYSTEM, "organize", t(lang, "סידור הקבצים", "Organize the files"),
                t(lang, f"סדר את הקבצים ב-{target} לפי סוג (בלי לשבור פרויקטים).",
                  f"Sort the files in {target} by type (never rearranging code projects)."),
                skill="organize_folder", params={"path": target}, depends_on=[review.id])
    return TaskPlan(title=t(lang, f"ארגון {target}", f"Organize {target}"),
                    strategy=t(lang, "סוכן התכנון מנתח, סוכן המערכת מבצע (עם אישור כשמדובר בהרבה קבצים).",
                               "The Planner analyzes; the System Agent moves files (asking first when many move)."),
                    subtasks=[review, move], source="rules")


def _assign(slots: dict[str, Any], goal: str, lang: str, ai: bool) -> TaskPlan | None:
    try:
        agent = AgentId.parse(slots.get("agent", ""))
    except (ValueError, KeyError):
        return None
    instruction = slots.get("instruction") or goal
    sub = _sub(agent, "assigned", instruction[:80], instruction, priority=Priority.HIGH)
    return TaskPlan(title=instruction[:80], strategy=t(lang, f"הוקצה ישירות ל{AGENT_INFO[agent]['name_he']}.",
                                                       f"Assigned directly to the {AGENT_INFO[agent]['name']}."),
                    subtasks=[sub], source="direct")


def _research(slots: dict[str, Any], goal: str, lang: str, ai: bool) -> TaskPlan:
    query = slots.get("query") or goal
    sub = _sub(A.RESEARCH, "research", t(lang, f"מחקר: {query[:60]}", f"Research: {query[:60]}"), goal,
               skill="research", params={"query": query}, priority=Priority.HIGH)
    return TaskPlan(title=t(lang, f"מחקר: {query[:60]}", f"Research: {query[:60]}"),
                    strategy=t(lang, "סוכן המחקר מחפש, קורא מקורות ומסכם.", "The Research Agent searches, reads and summarizes."),
                    subtasks=[sub], source="rules")


_RULES: dict[str, Any] = {
    I.BUILD_APP: _build_app,
    I.RESEARCH: _research,
    I.DESIGN: _single(A.CREATIVE, "design", "עיצוב", "Design", "brief"),
    I.WRITE_TEXT: _single(A.CREATIVE, "write_text", "כתיבה", "Writing", "brief"),
    I.WRITE_CODE: _single(A.DEVELOPMENT, "write_code", "קוד", "Code", "request"),
    I.PLAN: _single(A.PLANNER, "make_plan", "תוכנית", "Plan", "goal"),
    I.ORGANIZE_PROJECT: _organize,
    I.AGENT_ASSIGN: _assign,
}


# ------------------------------------------------------------------------------------------ fallback
_AGENT_KEYWORDS: list[tuple[AgentId, tuple[str, ...]]] = [
    (A.DEVELOPMENT, ("code", "bug", "python", "javascript", "api", "deploy", "git", "קוד", "באג", "תכנות", "פונקציה",
                     "סקריפט", "שגיאה", "error")),
    (A.CREATIVE, ("design", "logo", "icon", "image", "presentation", "slogan", "text", "post", "עיצוב", "לוגו", "אייקון",
                  "תמונה", "מצגת", "סלוגן", "טקסט", "פוסט", "רעיון", "רעיונות")),
    (A.SYSTEM, ("file", "folder", "process", "install", "windows", "settings", "disk", "קובץ", "תיקייה", "תהליך",
                "התקנה", "הגדרות", "דיסק", "מחשב")),
    (A.PLANNER, ("plan", "roadmap", "schedule", "priorit", "steps", "תוכנית", "תכנון", "לוח זמנים", "שלבים", "עדיפו")),
]


def guess_agent(goal: str) -> AgentId:
    text = normalize(goal)
    best, score = A.RESEARCH, 0
    for agent, words in _AGENT_KEYWORDS:
        hits = sum(1 for w in words if w in text)
        if hits > score:
            best, score = agent, hits
    return best


def fallback_plan(goal: str, lang: str) -> TaskPlan:
    agent = guess_agent(goal)
    sub = _sub(agent, "main", goal[:80], goal, priority=Priority.HIGH)
    return TaskPlan(title=goal[:80], strategy=t(lang, f"משימה אחת ל{AGENT_INFO[agent]['name_he']}.",
                                                f"One step for the {AGENT_INFO[agent]['name']}."),
                    subtasks=[sub], source="fallback")


# ------------------------------------------------------------------------------------------ LLM plan
PLANNER_SYSTEM = """You are the Planner of JARVIS, a personal AI assistant on the user's Windows computer.
Split the user's goal into steps for exactly these five agents (no others exist):
- research: web search, reading and comparing sources, summaries, data analysis, documentation lookup.
- system: files and folders, opening/closing programs, processes, PowerShell, Windows settings, system health;
  it can also operate an app on screen with the mouse and keyboard when nothing else fits (the user approves it).
- development: building apps and websites from templates or from scratch, writing and fixing code, tests, Git, APIs.
- creative: ideas, UI/UX, palettes, logos, icons, images, animations, marketing texts, presentations.
- planner: plans, roadmaps, stages, priorities, progress reviews.
Rules:
- Use as few steps as the goal needs (1 to 8). One agent per step. Steps that do not depend on each other run in
  parallel, so only add a dependency when a step really needs another step's result.
- Each instruction must be complete on its own: the agent sees only its instruction plus the results of the steps it
  depends on. Name concrete outputs (files under the workspace or projects folder) and how to check them.
- Mark steps that are nice-to-have as optional.
- Do not add a final "report to the user" step: JARVIS writes the final answer itself.
- Never plan anything that deletes data broadly, changes security settings, installs software or sends private data
  unless the user explicitly asked; if they did, keep it in its own step so it can be approved.
- Write titles and instructions in the user's language."""


async def llm_plan(router: Any, goal: str, lang: str, *, context: str = "", max_steps: int = 8) -> TaskPlan:
    prompt = f"The user's goal:\n{goal}"
    if context:
        prompt += f"\n\nUseful context:\n{context[:4000]}"
    prompt += f"\n\nUser language: {'Hebrew' if lang == 'he' else 'English'}."
    draft = await router.structured("planner", prompt, PlanDraft, system=PLANNER_SYSTEM)
    plan = plan_from_draft(draft, max_steps=max_steps)
    return plan
