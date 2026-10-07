"""What JARVIS says: bilingual (Hebrew / English) texts for answers the core gives itself.

Kept apart from the routing logic so wording can change without touching behaviour. Every
function takes the reply language ("he" or "en") first.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from jarvis.agents.base import AGENT_INFO, AGENT_NUMBER, t
from jarvis.common.enums import AgentId, Mode
from jarvis.core.intents import I

_WEEKDAYS_HE = ("שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון")
_MONTHS_HE = ("ינואר", "פברואר", "מרץ", "אפריל", "מאי", "יוני", "יולי", "אוגוסט", "ספטמבר", "אוקטובר", "נובמבר",
              "דצמבר")

MODE_NAMES = {
    Mode.CHAT: ("שיחה", "chat"),
    Mode.NORMAL: ("רגיל", "normal"),
    Mode.TASK: ("משימה", "task"),
    Mode.AUTONOMOUS: ("אוטונומי", "autonomous"),
    Mode.DEBUG: ("דיבאג", "debug"),
}
MODE_EXPLAINED = {
    Mode.CHAT: ("אני רק משוחח ולא מבצע פעולות במחשב.", "I only talk and take no actions on the computer."),
    Mode.NORMAL: ("פקודות פשוטות מתבצעות מיד ומשימות מורכבות מתחלקות בין הסוכנים.",
                  "Simple commands run at once and complex work is split between the agents."),
    Mode.TASK: ("כל בקשה מתוכננת ומוצגת כמשימה עם שלבים.", "Every request is planned and shown as a task with steps."),
    Mode.AUTONOMOUS: ("אני מוביל משימות ארוכות בעצמי, בודק את התוצאה ומוסיף שלבי המשך. פעולות מסוכנות עדיין דורשות "
                      "אישור.", "I drive long tasks myself, check the result and add follow-up steps. Risky actions "
                      "still need your approval."),
    Mode.DEBUG: ("אני מציג את ההחלטות שלי: זיהוי הכוונה, הניתוב, התוכנית והכלים.",
                 "I show my decisions: the intent, the routing, the plan and the tools."),
}
STATUS_NAMES = {
    "offline": ("כבוי", "offline"), "idle": ("פנוי", "idle"), "thinking": ("חושב", "thinking"),
    "working": ("עובד", "working"), "waiting": ("ממתין לאישור", "waiting for approval"),
    "completed": ("סיים", "done"), "error": ("שגיאה", "error"),
}
TASK_STATUS_NAMES = {
    "pending": ("ממתינה", "pending"), "planning": ("בתכנון", "planning"), "queued": ("בתור", "queued"),
    "running": ("רצה", "running"), "waiting": ("ממתינה לאישור", "waiting for approval"),
    "verifying": ("באימות", "verifying"), "completed": ("הושלמה", "completed"), "failed": ("נכשלה", "failed"),
    "cancelled": ("בוטלה", "cancelled"), "interrupted": ("נעצרה באמצע", "paused"), "skipped": ("דולגה", "skipped"),
}


def agent_name(agent: AgentId | str, lang: str) -> str:
    info = AGENT_INFO[AgentId.parse(agent)]
    return info["name_he"] if lang == "he" else info["name"]


def mode_name(mode: Mode, lang: str) -> str:
    he, en = MODE_NAMES[mode]
    return he if lang == "he" else en


# ------------------------------------------------------------------------------------------ instant answers
def time_now(lang: str, now: datetime | None = None) -> str:
    now = now or datetime.now()
    if lang == "he":
        return f"השעה {now:%H:%M}."
    hour = now.hour % 12 or 12
    return f"It's {hour}:{now:%M} {'AM' if now.hour < 12 else 'PM'}."


def date_today(lang: str, now: datetime | None = None) -> str:
    now = now or datetime.now()
    if lang == "he":
        return f"היום יום {_WEEKDAYS_HE[now.weekday()]}, {now.day} ב{_MONTHS_HE[now.month - 1]} {now.year}."
    return f"Today is {now:%A}, {now:%B} {now.day}, {now.year}."


def greeting(lang: str, user_name: str = "", now: datetime | None = None) -> str:
    hour = (now or datetime.now()).hour
    name = f" {user_name}" if user_name else ""
    if lang == "he":
        hello = "בוקר טוב" if 5 <= hour < 12 else "ערב טוב" if 17 <= hour < 23 else "שלום"
        return f"{hello}{name}. במה אפשר לעזור?"
    hello = "Good morning" if 5 <= hour < 12 else "Good evening" if 17 <= hour < 23 else "Hello"
    return f"{hello}{name}. How can I help?"


def thanks(lang: str) -> str:
    return t(lang, "בשמחה.", "Anytime.")


def help_text(lang: str) -> str:
    if lang == "he":
        return ("אני JARVIS, ויש לי חמישה סוכנים. אפשר לבקש ממני:\n"
                "• לפתוח ולסגור תוכנות ואתרים: \"פתח את כרום\", \"תפתח יוטיוב\"\n"
                "• לשלוט במחשב: עוצמת שמע, בהירות, צילום מסך, נעילה, \"בדוק את המחשב\"\n"
                "• קבצים ותיקיות: \"צור תיקייה בשם דוחות על שולחן העבודה\", \"תסדר את תיקיית ההורדות\"\n"
                "• לבנות אתרים, אפליקציות, משחקים ובוטים: \"תבנה לי אתר לחנות קפה\"\n"
                "• מחקר וסיכומים: \"תחקור את השוק של רכבים חשמליים\", \"מה מזג האוויר מחר\"\n"
                "• עיצוב וכתיבה: לוגו, פלטת צבעים, מצגות, פוסטים ומיילים\n"
                "• תוכניות ולוחות זמנים: \"תכין לי תוכנית עבודה לשבוע\"\n"
                "• לזכור דברים: \"תזכור ש...\", \"מה אתה זוכר על...\"\n"
                "ניהול: \"תראה לי את הסוכנים\", \"עצור את המשימה\", \"תמשיך את המשימה\", \"עבור למצב משימה\", "
                "\"עצור הכול\".")
    return ("I'm JARVIS, and I have five agents. You can ask me to:\n"
            "• open and close apps and websites: \"open Chrome\", \"open YouTube\"\n"
            "• control the computer: volume, brightness, screenshots, lock, \"check my system\"\n"
            "• files and folders: \"create a folder named Reports on the desktop\", \"clean up my downloads\"\n"
            "• build websites, apps, games and bots: \"build me a website for a bakery\"\n"
            "• research and summaries: \"research electric car prices\", \"what's the weather tomorrow\"\n"
            "• design and writing: logos, palettes, presentations, posts and emails\n"
            "• plans and schedules: \"make me a study plan\"\n"
            "• remember things: \"remember that...\", \"what do you remember about...\"\n"
            "Control: \"show the agents\", \"stop the task\", \"continue the task\", \"switch to task mode\", "
            "\"emergency stop\".")


def help_spoken(lang: str) -> str:
    return t(lang, "אני יכול לפתוח תוכנות ואתרים, לשלוט במחשב, לבנות אתרים ואפליקציות, לחקור, לעצב ולתכנן. "
                   "הרשימה המלאה מופיעה על המסך.",
             "I can open apps and sites, control the computer, build websites and apps, research, design and plan. "
             "The full list is on the screen.")


def offline_chat(lang: str) -> str:
    return t(lang, "לא בטוח שהבנתי. אפשר לנסח את זה כפקודה, למשל \"פתח את כרום\" או \"תבנה לי אתר לחנות\". "
                   "לשיחה חופשית צריך להגדיר מפתח API של Anthropic או OpenAI בהגדרות.",
             "I'm not sure I understood. Try it as a command, like \"open Chrome\" or \"build me a website\". "
             "For open conversation, add an Anthropic or OpenAI API key in Settings.")


# ------------------------------------------------------------------------------------------ modes
def mode_changed(mode: Mode, lang: str) -> str:
    he, en = MODE_EXPLAINED[mode]
    return t(lang, f"עברתי למצב {mode_name(mode, 'he')}. {he}", f"Switched to {mode_name(mode, 'en')} mode. {en}")


def mode_already(mode: Mode, lang: str) -> str:
    return t(lang, f"אני כבר במצב {mode_name(mode, 'he')}.", f"I'm already in {mode_name(mode, 'en')} mode.")


def chat_mode_refusal(lang: str) -> str:
    return t(lang, "אני במצב שיחה, ולכן לא מבצע פעולות. אפשר להגיד \"עבור למצב רגיל\" ואבצע את זה.",
             "I'm in chat mode, so I don't take actions. Say \"switch to normal mode\" and I'll do it.")


# ------------------------------------------------------------------------------------------ agents and tasks
def agents_overview(views: list[dict[str, Any]], lang: str) -> str:
    lines = []
    for v in sorted(views, key=lambda x: AGENT_NUMBER[AgentId(x["id"])]):
        he, en = STATUS_NAMES.get(v.get("status", ""), (v.get("status", ""), v.get("status", "")))
        status = he if lang == "he" else en
        if v.get("killed"):
            status = t(lang, "מתג חירום פעיל", "kill switch engaged")
        elif not v.get("enabled", True):
            status = t(lang, "כבוי", "stopped")
        line = f"{AGENT_NUMBER[AgentId(v['id'])]}. {v['name_he'] if lang == 'he' else v['name']} — {status}"
        current = v.get("current")
        if current and current.get("title"):
            line += f": {current['title']}"
            if v.get("progress"):
                line += f" ({round(float(v['progress']) * 100)}%)"
        if v.get("queue"):
            line += t(lang, f", {v['queue']} בתור", f", {v['queue']} queued")
        lines.append(line)
    working = sum(1 for v in views if v.get("status") in ("working", "thinking", "waiting"))
    off = sum(1 for v in views if not v.get("enabled", True) or v.get("killed"))
    if off:
        head = t(lang, f"{5 - off} מתוך 5 הסוכנים פעילים.", f"{5 - off} of the 5 agents are active.")
    elif working:
        head = t(lang, f"כל חמשת הסוכנים פעילים, {working} עובדים עכשיו.",
                 f"All five agents are active; {working} working now.")
    else:
        head = t(lang, "כל חמשת הסוכנים פעילים ופנויים.", "All five agents are active and idle.")
    return head + "\n" + "\n".join(lines)


def agents_spoken(views: list[dict[str, Any]], lang: str) -> str:
    return agents_overview(views, lang).split("\n", 1)[0]


def task_line(task: dict[str, Any], lang: str) -> str:
    he, en = TASK_STATUS_NAMES.get(task.get("status", ""), (task.get("status", ""), task.get("status", "")))
    progress = task.get("progress") or 0
    pct = f" {round(float(progress) * 100)}%" if task.get("status") not in ("completed", "pending") and progress else ""
    return f"• {task.get('title') or task.get('goal', '')[:60]} — {he if lang == 'he' else en}{pct}"


def tasks_overview(active: list[dict[str, Any]], paused: list[dict[str, Any]], lang: str) -> str:
    if not active and not paused:
        return t(lang, "אין משימות פעילות כרגע.", "There are no active tasks right now.")
    lines: list[str] = []
    if active:
        lines.append(t(lang, f"משימות פעילות ({len(active)}):", f"Active tasks ({len(active)}):"))
        lines += [task_line(x, lang) for x in active[:8]]
    if paused:
        lines.append(t(lang, "נעצרו באמצע (אפשר להמשיך):", "Paused (can continue):"))
        lines += [task_line(x, lang) for x in paused[:5]]
    return "\n".join(lines)


def task_story(detail: dict[str, Any], lang: str) -> str:
    """A finished (or paused) task told from its saved record: what was asked, the steps, the answer."""
    he, en = TASK_STATUS_NAMES.get(detail.get("status", ""), ("", ""))
    lines = [t(lang, f"המשימה האחרונה: {detail.get('title')} — {he}.", f"The last task: {detail.get('title')} — {en}.")]
    plan = detail.get("plan") or {}
    for s in (plan.get("subtasks") or [])[:10]:
        s_he, s_en = TASK_STATUS_NAMES.get(s.get("status", ""), ("", ""))
        lines.append(f"• {agent_name(s['agent'], lang)} — {s['title']}: {s_he if lang == 'he' else s_en}")
    result = detail.get("result") or {}
    if result.get("summary"):
        lines.append(result["summary"])
    return "\n".join(lines)


def no_task_to_stop(lang: str) -> str:
    return t(lang, "אין משימה פעילה לעצור.", "There's no active task to stop.")


def task_cancelled(title: str, lang: str) -> str:
    return t(lang, f"עצרתי את המשימה '{title}'.", f"I stopped the task '{title}'.")


def tasks_cancelled(n: int, lang: str) -> str:
    return t(lang, f"עצרתי {n} משימות.", f"I stopped {n} tasks.") if n != 1 else t(lang, "עצרתי את המשימה.",
                                                                                   "I stopped the task.")


def nothing_to_resume(lang: str) -> str:
    return t(lang, "אין משימה שנעצרה באמצע.", "There's no paused task to continue.")


def resumed(title: str, done: int, total: int, lang: str) -> str:
    if total and done:
        return t(lang, f"ממשיך את '{title}' מהמקום שבו עצרנו: {done} מתוך {total} שלבים כבר הושלמו.",
                 f"Continuing '{title}' where we stopped: {done} of {total} steps are already done.")
    return t(lang, f"ממשיך את '{title}'.", f"Continuing '{title}'.")


def complex_ack(agents: list[AgentId], lang: str) -> str:
    if len(agents) == 1:
        return t(lang, f"קיבלתי. {agent_name(agents[0], 'he')} מתחיל לעבוד על זה.",
                 f"Got it. The {agent_name(agents[0], 'en')} is on it.")
    return t(lang, "קיבלתי. אני מחלק את המשימה בין הסוכנים.", "Got it. I'm splitting the task between the agents.")


def plan_message(plan: dict[str, Any], lang: str) -> str:
    subs = plan.get("subtasks") or []
    n = len(subs)
    count = t(lang, "שלב אחד" if n == 1 else f"{n} שלבים", "1 step" if n == 1 else f"{n} steps")
    lines = [t(lang, f"התוכנית ({count}): {plan.get('strategy') or ''}".strip(),
               f"The plan ({count}): {plan.get('strategy') or ''}".strip())]
    by_id = {s["id"]: i + 1 for i, s in enumerate(subs)}
    for i, s in enumerate(subs, 1):
        deps = [str(by_id[d]) for d in s.get("depends_on", []) if d in by_id]
        after = t(lang, f" (אחרי {', '.join(deps)})", f" (after {', '.join(deps)})") if deps else ""
        optional = t(lang, " — רשות", " — optional") if s.get("optional") else ""
        lines.append(f"{i}. {agent_name(s['agent'], lang)}: {s['title']}{after}{optional}")
    return "\n".join(lines)


def waiting_for_approval(agent: str, summary: str, lang: str) -> str:
    return t(lang, f"{agent_name(agent, 'he')} מבקש את האישור שלך: {summary}. לאשר?",
             f"The {agent_name(agent, 'en')} needs your approval: {summary}. Approve?")


def approval_answer(approved: bool, summary: str, lang: str) -> str:
    if approved:
        return t(lang, f"אישרתי: {summary}.", f"Approved: {summary}.")
    return t(lang, f"לא אישרתי: {summary}.", f"Not approved: {summary}.")


def kill_engaged(lang: str, stopped: int) -> str:
    extra = t(lang, f" {stopped} משימות נעצרו ואפשר להמשיך אותן אחר כך.", f" {stopped} tasks were paused and can "
              "continue later.") if stopped else ""
    return t(lang, "עצרתי הכול. מתג החירום פעיל: אף סוכן לא מבצע פעולות עד שתגיד \"שחרר את מתג החירום\"." + extra,
             "Everything is stopped. The kill switch is on: no agent acts until you say \"release the kill switch\"."
             + extra)


def kill_released(lang: str, paused: list[dict[str, Any]]) -> str:
    text = t(lang, "מתג החירום שוחרר. הסוכנים זמינים שוב.", "The kill switch is released. The agents are available "
             "again.")
    if paused:
        text += " " + t(lang, f"המשימה '{paused[0]['title']}' נעצרה באמצע; אפשר להגיד \"תמשיך את המשימה\".",
                        f"The task '{paused[0]['title']}' is paused; say \"continue the task\" to resume it.")
    return text


def kill_not_engaged(lang: str) -> str:
    return t(lang, "מתג החירום לא פעיל.", "The kill switch is not engaged.")


def agent_control(action: str, agent: AgentId, lang: str, paused: list[dict[str, Any]] | None = None) -> str:
    he_name, en_name = agent_name(agent, "he"), agent_name(agent, "en")
    text = {
        "stop": t(lang, f"עצרתי את {he_name}. הוא לא יקבל משימות עד שתפעיל אותו שוב.",
                  f"I stopped the {en_name}. It takes no work until you start it again."),
        "start": t(lang, f"{he_name} פעיל.", f"The {en_name} is active."),
        "restart": t(lang, f"הפעלתי מחדש את {he_name}.", f"I restarted the {en_name}."),
    }[action]
    if paused and action in ("start", "restart"):
        text += " " + t(lang, f"המשימה '{paused[0]['title']}' חיכתה לו; אפשר להגיד \"תמשיך את המשימה\".",
                        f"The task '{paused[0]['title']}' was waiting for it; say \"continue the task\".")
    return text


def all_agents_stopped(lang: str) -> str:
    return t(lang, "עצרתי את כל הסוכנים. אפשר להפעיל כל סוכן מחדש מלוח הסוכנים או בפקודה.",
             "I stopped all the agents. Start any of them again from the Agents panel or by command.")


def agent_unavailable(agent: AgentId, lang: str, killed: bool) -> str:
    if killed:
        return t(lang, f"מתג החירום פעיל עבור {agent_name(agent, 'he')}, ולכן הוא לא מבצע פעולות.",
                 f"The kill switch is engaged for the {agent_name(agent, 'en')}, so it can't act.")
    return t(lang, f"{agent_name(agent, 'he')} כבוי. אפשר להגיד \"הפעל את {agent_name(agent, 'he')}\".",
             f"The {agent_name(agent, 'en')} is stopped. Say \"start the {agent_name(agent, 'en')}\".")


# ------------------------------------------------------------------------------------------ memory
def remembered(lang: str) -> str:
    return t(lang, "אזכור את זה.", "I'll remember that.")


def forgot(n: int, lang: str) -> str:
    if n == 0:
        return t(lang, "לא מצאתי משהו כזה בזיכרון.", "I didn't find anything like that in memory.")
    return t(lang, "שכחתי את זה." if n == 1 else f"שכחתי {n} דברים.", "Forgotten." if n == 1 else f"Forgot {n} things.")


def recall_nothing(query: str, lang: str) -> str:
    return t(lang, f"אני לא זוכר שום דבר על {query}.", f"I don't remember anything about {query}.")


def recall_list(query: str, items: list[str], lang: str) -> str:
    return t(lang, f"זה מה שאני זוכר על {query}:", f"Here's what I remember about {query}:") + "\n" + "\n".join(
        f"• {x}" for x in items[:8])


def memory_off(lang: str) -> str:
    return t(lang, "הזיכרון לא זמין כרגע.", "Memory is not available right now.")


# ------------------------------------------------------------------------------------------ fast-path action cards
ACTION_CARDS: dict[str, tuple[str, str, str]] = {
    # intent: (Hebrew title, English title, icon) — {target} is filled from the slots
    I.OPEN_APP: ("פותח את {target}…", "Opening {target}…", "app"),
    I.CLOSE_APP: ("סוגר את {target}…", "Closing {target}…", "close"),
    I.OPEN_WEBSITE: ("פותח את {target}…", "Opening {target}…", "globe"),
    I.WEB_SEARCH: ("מחפש: {target}…", "Searching: {target}…", "search"),
    I.OPEN_FOLDER: ("פותח את התיקייה…", "Opening the folder…", "folder"),
    I.CREATE_FOLDER: ("יוצר תיקייה {target}…", "Creating the folder {target}…", "folder-plus"),
    I.FIND_FILES: ("מחפש קבצים: {target}…", "Finding files: {target}…", "file-search"),
    I.SYSTEM_STATUS: ("בודק את מצב המחשב…", "Checking the computer…", "cpu"),
    I.VOLUME: ("משנה את עוצמת השמע…", "Adjusting the volume…", "volume"),
    I.BRIGHTNESS: ("משנה את הבהירות…", "Adjusting the brightness…", "sun"),
    I.MEDIA: ("שולט בנגינה…", "Controlling playback…", "music"),
    I.LOCK_SCREEN: ("נועל את המחשב…", "Locking the computer…", "lock"),
    I.POWER: ("מכין את פעולת ההפעלה…", "Preparing the power action…", "power"),
    I.SCREENSHOT: ("מצלם את המסך…", "Taking a screenshot…", "camera"),
    I.WEATHER: ("בודק את מזג האוויר…", "Checking the weather…", "cloud"),
    I.RUN_PROJECT: ("מפעיל את הפרויקט…", "Starting the project…", "play"),
    "open_project": ("פותח את {target}…", "Opening {target}…", "play"),
}


def action_title(intent: str, slots: dict[str, Any], lang: str) -> tuple[str, str]:
    he, en, icon = ACTION_CARDS.get(intent, ("מבצע…", "Working…", "bolt"))
    target = (slots.get("app_name") or slots.get("site") or slots.get("query") or slots.get("name")
              or slots.get("location") or slots.get("project") or "")
    title = (he if lang == "he" else en).format(target=target).replace("  ", " ").replace(" …", "…")
    return title, icon


__all__ = [
    "ACTION_CARDS", "MODE_NAMES", "action_title", "agent_control", "agent_name", "agents_overview", "agents_spoken",
    "all_agents_stopped", "approval_answer", "chat_mode_refusal", "complex_ack", "date_today", "forgot", "greeting",
    "help_spoken", "help_text", "kill_engaged", "kill_not_engaged", "kill_released", "memory_off", "mode_already",
    "mode_changed", "mode_name", "no_task_to_stop", "nothing_to_resume", "offline_chat", "plan_message",
    "recall_list", "recall_nothing", "remembered", "resumed", "task_cancelled", "task_line", "task_story",
    "tasks_cancelled", "tasks_overview", "thanks", "time_now", "waiting_for_approval",
]
