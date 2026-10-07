"""Offline plan documents: phases, tasks, priorities, dates, owners and open questions.

Used by the Planner when no AI provider is configured (and as a skeleton the model can refine).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from jarvis.common.text import normalize

PRIORITY_LABEL = {"critical": ("קריטי", "critical"), "high": ("גבוה", "high"), "normal": ("רגיל", "normal"),
                  "low": ("נמוך", "low")}
AGENT_LABEL = {"research": ("סוכן מחקר", "Research Agent"), "system": ("סוכן מערכת", "System Agent"),
               "development": ("סוכן פיתוח", "Development Agent"), "creative": ("סוכן יצירתי", "Creative Agent"),
               "planner": ("סוכן תכנון", "Planner Agent")}


@dataclass
class Step:
    he: str
    en: str
    priority: str = "normal"
    agent: str | None = None


@dataclass
class Phase:
    he: str
    en: str
    days: int
    steps: list[Step] = field(default_factory=list)


@dataclass
class Domain:
    key: str
    keywords: tuple[str, ...]
    phases: list[Phase]


def S(he: str, en: str, priority: str = "normal", agent: str | None = None) -> Step:
    return Step(he, en, priority, agent)


DOMAINS: list[Domain] = [
    Domain("software", ("אתר", "אפליקציה", "מערכת", "תוכנה", "קוד", "פיתוח", "app", "website", "software", "system",
                        "saas", "platform", "פלטפורמה", "חנות אונליין", "online store"), [
        Phase("גילוי ודרישות", "Discovery and requirements", 5, [
            S("להגדיר מטרות ומדדי הצלחה", "Define goals and success metrics", "high"),
            S("לתאר את קהל היעד ואת התרחישים המרכזיים", "Describe the users and the main scenarios", "high"),
            S("לחלק תכונות לחובה ולרצוי", "Split features into must-have and nice-to-have", "high"),
            S("לבדוק מתחרים ודוגמאות מוצלחות", "Study competitors and good examples", "normal", "research"),
        ]),
        Phase("עיצוב", "Design", 5, [
            S("מפת מסכים / עמודים", "Screen and page map", "high", "creative"),
            S("פלטת צבעים, לוגו ואייקונים", "Palette, logo and icons", "normal", "creative"),
            S("שרטוט (wireframe) למסכים המרכזיים", "Wireframes for the key screens", "high", "creative"),
        ]),
        Phase("פיתוח גרסה ראשונה (MVP)", "Build the first version (MVP)", 15, [
            S("הקמת הפרויקט ומאגר Git", "Set up the project and a Git repository", "high", "development"),
            S("פיתוח תכונות הליבה", "Build the core features", "critical", "development"),
            S("שמירת נתונים ו-API", "Data storage and API", "high", "development"),
            S("בדיקות אוטומטיות", "Automated tests", "high", "development"),
        ]),
        Phase("בדיקות וליטוש", "Testing and polish", 5, [
            S("בדיקה מקצה לקצה במכשירים ובדפדפנים", "End-to-end testing on devices and browsers", "high", "development"),
            S("תיקון באגים וביצועים", "Fix bugs and performance", "high", "development"),
            S("נגישות ותמיכה בעברית (RTL)", "Accessibility and right-to-left support", "normal", "creative"),
        ]),
        Phase("השקה", "Launch", 3, [
            S("העלאה לשרת / אחסון", "Deploy and host", "high", "system"),
            S("מדידה וניטור", "Analytics and monitoring", "normal", "system"),
            S("הודעת השקה", "Launch announcement", "normal", "creative"),
        ]),
        Phase("אחרי ההשקה", "After launch", 7, [
            S("איסוף משוב ממשתמשים", "Collect user feedback", "normal"),
            S("תעדוף שיפורים לגרסה הבאה", "Prioritize improvements for the next version", "normal", "planner"),
        ]),
    ]),
    Domain("study", ("ללמוד", "לימוד", "לימודים", "מבחן", "בחינה", "בגרות", "קורס", "study", "learn", "exam", "course",
                     "test prep", "studying"), [
        Phase("מיפוי החומר", "Map the material", 3, [
            S("לרשום את כל הנושאים והסילבוס", "List every topic in the syllabus", "high"),
            S("לבדוק רמה נוכחית בכל נושא", "Check your current level per topic", "high"),
            S("לקבוע לוח זמנים שבועי", "Set a weekly schedule", "high", "planner"),
        ]),
        Phase("יסודות", "Foundations", 10, [
            S("לימוד יומי של מושגי הליבה (1–2 שעות)", "Daily core concepts (1–2 hours)", "critical"),
            S("סיכומים וכרטיסיות", "Summaries and flashcards", "high", "creative"),
        ]),
        Phase("תרגול", "Practice", 10, [
            S("תרגילים לפי נושא", "Exercises by topic", "critical"),
            S("מעקב אחרי טעויות חוזרות", "Track repeated mistakes", "high"),
        ]),
        Phase("חזרה ומבחני דמה", "Review and mock exams", 6, [
            S("מבחני דמה מלאים בזמן אמת", "Full mock exams under time", "critical"),
            S("חזרה ממוקדת על נקודות חלשות", "Targeted review of weak topics", "high"),
        ]),
        Phase("הימים האחרונים", "Final days", 2, [
            S("חזרה קלה, שינה ומנוחה", "Light review, sleep and rest", "normal"),
            S("לוגיסטיקה: מקום, שעה, ציוד", "Logistics: place, time, materials", "normal"),
        ]),
    ]),
    Domain("event", ("אירוע", "חתונה", "מסיבה", "יום הולדת", "כנס", "בר מצווה", "בת מצווה", "event", "wedding", "party",
                     "conference", "birthday", "meetup"), [
        Phase("יסודות", "Basics", 3, [
            S("תאריך, תקציב ומספר אורחים", "Date, budget and guest count", "critical"),
            S("קונספט ואווירה", "Concept and theme", "normal", "creative"),
        ]),
        Phase("מקום וספקים", "Venue and vendors", 10, [
            S("להזמין מקום", "Book the venue", "critical"),
            S("קייטרינג, מוזיקה וצילום", "Catering, music and photography", "high"),
        ]),
        Phase("הזמנות", "Invitations", 7, [
            S("רשימת מוזמנים", "Guest list", "high"),
            S("עיצוב ושליחת הזמנות ומעקב אישורי הגעה", "Design and send invitations, track RSVPs", "high", "creative"),
        ]),
        Phase("לוגיסטיקה", "Logistics", 5, [
            S("לו״ז מפורט ליום האירוע", "Detailed run-of-show", "high", "planner"),
            S("הגעה, חניה ונגישות", "Transport, parking and accessibility", "normal"),
            S("תוכנית גיבוי (מזג אוויר, ביטולים)", "Backup plan (weather, cancellations)", "normal"),
        ]),
        Phase("יום האירוע", "The day", 1, [
            S("רשימת הקמה ואחראי לכל תחום", "Setup checklist and an owner for each area", "high"),
        ]),
        Phase("אחרי", "After", 3, [
            S("תודות, תשלומים ותמונות", "Thank-yous, payments and photos", "normal"),
        ]),
    ]),
    Domain("trip", ("טיול", "חופשה", "נסיעה", "טיסה", "trip", "travel", "vacation", "holiday", "flight", "itinerary"), [
        Phase("יסודות", "Basics", 2, [
            S("תאריכים, תקציב ומשתתפים", "Dates, budget and travelers", "critical"),
            S("מה חשוב לראות ולעשות", "What matters most to see and do", "high", "research"),
        ]),
        Phase("הזמנות", "Bookings", 5, [
            S("טיסות", "Flights", "critical", "research"),
            S("לינה", "Accommodation", "critical", "research"),
            S("ביטוח נסיעות", "Travel insurance", "high"),
            S("דרכון, ויזה ומסמכים", "Passport, visa and documents", "critical"),
        ]),
        Phase("מסלול", "Itinerary", 4, [
            S("תוכנית יום אחר יום", "Day-by-day plan", "high", "planner"),
            S("הזמנות מראש לאטרקציות", "Advance tickets for key attractions", "normal"),
            S("תחבורה מקומית", "Local transport", "normal", "research"),
        ]),
        Phase("הכנות", "Preparation", 3, [
            S("רשימת ציוד", "Packing list", "normal"),
            S("כסף, כרטיסים וסים", "Money, cards and SIM", "normal"),
        ]),
    ]),
    Domain("business", ("עסק", "סטארטאפ", "השקה", "שיווק", "מוצר", "לקוחות", "business", "startup", "launch", "marketing",
                        "product", "customers", "brand"), [
        Phase("מחקר", "Research", 7, [
            S("שוק ומתחרים", "Market and competitors", "high", "research"),
            S("מי הלקוחות ומה הבעיה שלהם", "Who the customers are and their problem", "high", "research"),
        ]),
        Phase("הצעה ומודל", "Offer and model", 5, [
            S("הצעת ערך ברורה", "A clear value proposition", "critical", "planner"),
            S("תמחור", "Pricing", "high", "research"),
            S("עלויות ותקציב", "Costs and budget", "high", "planner"),
        ]),
        Phase("מותג", "Brand", 5, [
            S("שם, לוגו וצבעים", "Name, logo and colors", "normal", "creative"),
            S("מסרים ושפה", "Messaging and tone", "high", "creative"),
        ]),
        Phase("מוצר ראשון", "First product", 14, [
            S("גרסה ראשונה (MVP)", "First version (MVP)", "critical", "development"),
            S("משוב מ-5–10 לקוחות", "Feedback from 5–10 customers", "high"),
        ]),
        Phase("שיווק והשקה", "Marketing and launch", 7, [
            S("ערוצי שיווק", "Marketing channels", "high", "research"),
            S("תוכן להשקה", "Launch content", "high", "creative"),
            S("יום ההשקה", "Launch day", "high", "planner"),
        ]),
        Phase("מדידה", "Measure", 7, [
            S("מדדים עיקריים (KPI)", "Key metrics (KPIs)", "high", "planner"),
            S("שיפור לפי הנתונים", "Iterate on the data", "normal"),
        ]),
    ]),
    Domain("fitness", ("כושר", "אימון", "אימונים", "ריצה", "דיאטה", "משקל", "תזונה", "fitness", "workout", "training",
                       "running", "diet", "weight", "gym", "marathon"), [
        Phase("נקודת פתיחה", "Starting point", 3, [
            S("מדידות ומטרה ברורה", "Measurements and a clear goal", "high"),
            S("לקבוע 3–4 אימונים בשבוע ביומן", "Book 3–4 sessions a week in the calendar", "high", "planner"),
        ]),
        Phase("שבועות 1–4: בסיס", "Weeks 1–4: foundation", 28, [
            S("טכניקה נכונה בעומס מתון", "Correct technique at moderate load", "critical"),
            S("שינה ותזונה בסיסיות", "Baseline sleep and nutrition", "high"),
        ]),
        Phase("שבועות 5–8: התקדמות", "Weeks 5–8: progression", 28, [
            S("להעלות עומס ב-5–10% בשבוע", "Increase load by 5–10% a week", "high"),
            S("מעקב התקדמות", "Track progress", "normal"),
        ]),
        Phase("שבועות 9–12: שיא", "Weeks 9–12: peak", 28, [
            S("אימונים ממוקדי מטרה", "Goal-specific training", "high"),
            S("שבוע הורדת עומס", "A deload week", "normal"),
        ]),
    ]),
]

GENERIC = Domain("generic", (), [
    Phase("הגדרה", "Define", 2, [
        S("מטרה, מדד הצלחה ותאריך יעד", "Goal, success measure and deadline", "critical", "planner"),
    ]),
    Phase("פירוק למשימות", "Break it down", 2, [
        S("רשימת משימות עם הערכת זמן", "Task list with time estimates", "high", "planner"),
        S("סדר לפי תלויות ועדיפות", "Order by dependency and priority", "high", "planner"),
    ]),
    Phase("משאבים", "Resources", 3, [
        S("מה צריך: זמן, כסף, אנשים, כלים", "What is needed: time, money, people, tools", "high"),
        S("מידע שחסר ומי יכול לעזור", "Missing information and who can help", "normal", "research"),
    ]),
    Phase("ביצוע באבני דרך", "Execute in milestones", 14, [
        S("אבן דרך 1: התוצאה הראשונה שאפשר להראות", "Milestone 1: the first result you can show", "critical"),
        S("אבן דרך 2: השלמת עיקר העבודה", "Milestone 2: the bulk of the work", "high"),
        S("אבן דרך 3: גימור ובדיקה", "Milestone 3: finishing and checking", "high"),
    ]),
    Phase("סקירה", "Review", 2, [
        S("בדיקה שבועית של ההתקדמות ותיקון כיוון", "Weekly progress check and course correction", "normal", "planner"),
    ]),
])


def detect_domain(goal: str) -> Domain:
    text = normalize(goal)
    best, best_hits = GENERIC, 0
    for domain in DOMAINS:
        hits = sum(1 for k in domain.keywords if normalize(k) in text)
        if hits > best_hits:
            best, best_hits = domain, hits
    return best


_DATE_HINT = re.compile(r"\d{1,2}[./-]\d{1,2}|\b20\d\d\b|\b(?:by|until|before|deadline|in \d+ (?:days|weeks|months))\b|"
                        r"עד\s|תוך\s|בעוד\s|ינואר|פברואר|מרץ|אפריל|מאי|יוני|יולי|אוגוסט|ספטמבר|אוקטובר|נובמבר|דצמבר|"
                        r"january|february|march|april|may|june|july|august|september|october|november|december", re.I)
_BUDGET_HINT = re.compile(r"₪|\$|€|ש\"ח|שקל|תקציב|budget|dollars|shekels", re.I)


def missing_information(goal: str, domain: Domain, lang: str) -> list[str]:
    out: list[str] = []
    if not _DATE_HINT.search(goal):
        out.append("תאריך יעד" if lang == "he" else "a deadline")
    if domain.key in ("event", "trip", "business", "software") and not _BUDGET_HINT.search(goal):
        out.append("תקציב" if lang == "he" else "a budget")
    if domain.key in ("event", "trip") and not re.search(r"\d+\s*(?:אנשים|אורחים|people|guests|travelers)", goal, re.I):
        out.append("מספר משתתפים" if lang == "he" else "the number of people")
    if domain.key == "study" and not re.search(r"מבחן|בחינה|exam|test", goal, re.I):
        out.append("מתי ומה בודקים (מבחן / פרויקט)" if lang == "he" else "how and when you are assessed")
    return out


def schedule(phases: list[dict[str, Any]], start: date | None = None) -> list[dict[str, Any]]:
    """Give consecutive phases their start and end dates (each phase lasts `days` calendar days)."""
    day = start or date.today()
    out = []
    for phase in phases:
        days = max(1, int(phase.get("days") or 1))
        end = day + timedelta(days=days - 1)
        out.append({**phase, "days": days, "start": day.isoformat(), "end": end.isoformat()})
        day = end + timedelta(days=1)
    return out


_REQUEST_HE = re.compile(r"^(?:ג'ארוויס[,\s]*)?(?:תכין|הכן|תכיני|הכיני|תעשה|עשה|תעשי|תכתוב|כתוב|תכתבי|תבנה|בנה|תיצור|"
                         r"צור|תתכנן|תכנן|תתכנני|תכנני|תארגן|ארגן)\s+(?:לי\s+|לנו\s+)?(?:את\s+)?")
_REQUEST_EN = re.compile(r"^(?:jarvis[,\s]*)?(?:please\s+)?(?:make|create|write|build|prepare|draft|plan|outline|"
                         r"draw up)\s+(?:me\s+|us\s+)?(?:a\s+|an\s+|the\s+)?", re.I)


def plan_title(goal: str, lang: str) -> str:
    """A document title from the request: "תכין לי תוכנית לחתונה" → "תוכנית לחתונה"."""
    text = _REQUEST_EN.sub("", _REQUEST_HE.sub("", (goal or "").strip().rstrip(" .!?"))).strip()
    if not text:
        return "תוכנית" if lang == "he" else "Plan"
    return (text if lang == "he" else text[:1].upper() + text[1:])[:80]


def build_plan(goal: str, lang: str, *, start: date | None = None) -> dict[str, Any]:
    """Structured plan: phases with dates, tasks (priority, owner), first actions and open questions."""
    domain = detect_domain(goal)
    phases = schedule([{
        "title": phase.he if lang == "he" else phase.en, "days": phase.days,
        "tasks": [{"title": s.he if lang == "he" else s.en, "priority": s.priority, "agent": s.agent}
                  for s in phase.steps],
    } for phase in domain.phases], start)
    first = [t["title"] for p in phases for t in p["tasks"] if t["priority"] == "critical"][:3] or \
        [phases[0]["tasks"][0]["title"]]
    return {"goal": goal, "title": plan_title(goal, lang), "domain": domain.key, "phases": phases,
            "first_actions": first, "missing": missing_information(goal, domain, lang),
            "finish": phases[-1]["end"] if phases else None, "source": "template"}


def plan_markdown(plan: dict[str, Any], lang: str) -> str:
    he = lang == "he"
    title = str(plan.get("title") or plan["goal"])
    named = re.match(r"^(?:ה)?(?:תוכנית|תכנית)\b|^(?:the\s+|a\s+)?(?:\w+\s+)?(?:plan|roadmap)\b", title, re.I)
    lines = [f"# {title}" if named else f"# {'תוכנית' if he else 'Plan'}: {title}", ""]
    if plan.get("summary"):
        lines += [str(plan["summary"]), ""]
    k = len(plan["phases"])
    lines.append(("**מבנה:** " if he else "**Shape:** ")
                 + (f"{'שלב אחד' if k == 1 else f'{k} שלבים'}, סיום משוער {plan['finish']}" if he
                    else f"{k} phase{'' if k == 1 else 's'}, estimated finish {plan['finish']}"))
    lines += ["", "## " + ("לוח זמנים" if he else "Timeline"), "",
              "| " + (" | ".join(["שלב", "מתאריך", "עד", "ימים"] if he else ["Phase", "From", "To", "Days"])) + " |",
              "|---|---|---|---|"]
    lines += [f"| {p['title']} | {p['start']} | {p['end']} | {p['days']} |" for p in plan["phases"]]
    for i, p in enumerate(plan["phases"], 1):
        lines += ["", f"## {i}. {p['title']} ({p['start']} → {p['end']})", ""]
        for task in p["tasks"]:
            prio = PRIORITY_LABEL.get(task.get("priority", "normal"), PRIORITY_LABEL["normal"])[0 if he else 1]
            owner = f" — {AGENT_LABEL[task['agent']][0 if he else 1]}" if task.get("agent") in AGENT_LABEL else ""
            lines.append(f"- [ ] {task['title']} ({'עדיפות' if he else 'priority'}: {prio}){owner}")
    lines += ["", "## " + ("להתחיל עכשיו" if he else "Start now"), ""]
    lines += [f"{i}. {a}" for i, a in enumerate(plan["first_actions"], 1)]
    if plan.get("risks"):
        lines += ["", "## " + ("סיכונים וחסמים" if he else "Risks and blockers"), ""]
        lines += [f"- {r}" for r in plan["risks"]]
    if plan.get("missing"):
        lines += ["", "## " + ("מה חסר כדי לדייק את התוכנית" if he else "What is missing to sharpen the plan"), ""]
        lines += [f"- {m}" for m in plan["missing"]]
    return "\n".join(lines) + "\n"
