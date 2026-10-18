"""Stage 2: intent detection for the spec's voice/text commands, in Hebrew and English."""

from __future__ import annotations

import time

import pytest

from jarvis.common.enums import AgentId
from jarvis.core.ai_words import named_ai
from jarvis.core.intents import I, detect_intent

CASES: list[tuple[str, str, dict]] = [
    # System commands from the spec
    ("JARVIS, open Chrome", I.OPEN_APP, {"app": "chrome"}),
    ("ג'ארוויס, פתח לי את כרום", I.OPEN_APP, {"app": "chrome"}),
    ("תוכל לפתוח את כרום בבקשה", I.OPEN_APP, {"app": "chrome"}),
    ("פתח את גוגל כרום", I.OPEN_APP, {"app": "chrome"}),
    ("open spotify app", I.OPEN_APP, {"app": "spotify"}),
    ("פתח את האפליקציה של ספוטיפיי", I.OPEN_APP, {"app": "spotify"}),
    ("Open Visual Studio Code", I.OPEN_APP, {"app": "vscode"}),
    ("תפתח את ההגדרות", I.OPEN_APP, {"app": "settings"}),
    ("check my system", I.SYSTEM_STATUS, {}),
    ("בדוק את המחשב", I.SYSTEM_STATUS, {}),
    ("show system status", I.SYSTEM_STATUS, {}),
    ("check the cpu usage", I.SYSTEM_STATUS, {"focus": "cpu"}),
    ("כמה זיכרון פנוי", I.SYSTEM_STATUS, {"focus": "memory"}),
    ("run this project", I.RUN_PROJECT, {"project": None}),
    ("תריץ את הפרויקט", I.RUN_PROJECT, {"project": None}),
    ("תריץ את הפרויקט האחרון", I.RUN_PROJECT, {"project": None}),
    ("open the project", I.RUN_PROJECT, {"project": None}),
    # Testing a project: the Development Agent runs its checks; it does not open an app called "tests"
    ("run the tests", I.RUN_TESTS, {"project": None}),
    ("הרץ בדיקות", I.RUN_TESTS, {"project": None}),
    ("תריץ את הבדיקות של הפרויקט אתר לחנות פרחים", I.RUN_TESTS, {"project": "אתר לחנות פרחים"}),
    ("run the tests of my flower shop project", I.RUN_TESTS, {"project": "flower shop"}),
    ("תבדוק את הפרויקט אתר לחנות פרחים", I.TEST_PROJECT, {"project": "אתר לחנות פרחים", "fix": False}),
    ("verify my project", I.TEST_PROJECT, {"project": None, "fix": False}),
    ("check if the calculator project works", I.TEST_PROJECT, {"project": "calculator"}),
    ("תבדוק שהפרויקט עובד", I.TEST_PROJECT, {"project": None}),
    ("בדוק את הקוד של הפרויקט", I.TEST_PROJECT, {"project": None}),
    ("write tests for the calculator", I.WRITE_CODE, {"request": "write tests for the calculator"}),
    ("תכתוב בדיקות למחשבון", I.WRITE_CODE, {}),
    ("create a folder named Reports on the desktop", I.CREATE_FOLDER, {"name": "Reports", "location": "desktop"}),
    ("צור תיקייה בשם דוחות על שולחן העבודה", I.CREATE_FOLDER, {"name": "דוחות", "location": "desktop"}),
    ("צור תיקייה בשם 'פרויקט חדש' במסמכים", I.CREATE_FOLDER, {"name": "פרויקט חדש", "location": "documents"}),
    ("צור תיקייה בשם דוחות בדיקה", I.CREATE_FOLDER, {"name": "דוחות בדיקה", "location": "workspace"}),
    ("make a folder", I.CREATE_FOLDER, {"name": None, "location": "workspace"}),
    # Deleting: the Recycle Bin unless the user says "permanently"; the target must be named as a file/folder or a path
    ("תמחק לצמיתות את התיקייה פרויקט-דמו", I.DELETE_PATH,
     {"name": "פרויקט-דמו", "location": None, "kind": "folder", "permanent": True}),
    ("מחק את הקובץ דוח.txt משולחן העבודה", I.DELETE_PATH,
     {"name": "דוח.txt", "location": "desktop", "kind": "file", "permanent": False}),
    ("תמחק את הקובץ דוח ממרץ", I.DELETE_PATH, {"name": "דוח ממרץ", "location": None}),
    ("תוכל למחוק את report.pdf מההורדות בבקשה", I.DELETE_PATH, {"name": "report.pdf", "location": "downloads"}),
    ('תמחקי את התיקייה בשם "דוחות ישנים" מהמסמכים בלי סל מחזור', I.DELETE_PATH,
     {"name": "דוחות ישנים", "location": "documents", "permanent": True}),
    ("permanently delete the file notes.txt from the desktop", I.DELETE_PATH,
     {"name": "notes.txt", "location": "desktop", "kind": "file", "permanent": True}),
    ("delete C:\\temp\\old logs", I.DELETE_PATH, {"name": "C:\\temp\\old logs", "kind": None}),
    ("remove the directory build", I.DELETE_PATH, {"name": "build", "kind": "folder", "permanent": False}),
    ("delete everything", I.CHAT, {}),
    ("תמחק את ההיסטוריה", I.CHAT, {}),
    ("תסיר את זום", I.CHAT, {}),
    ("research the best laptops for programming", I.RESEARCH, {"query": "the best laptops for programming"}),
    ("תחקור את השוק של רכבים חשמליים", I.RESEARCH, {"query": "השוק של רכבים חשמליים"}),
    ("search for cheap flights to london", I.RESEARCH, {"query": "cheap flights to london"}),
    ("חפש לי מסעדות בחיפה", I.RESEARCH, {"query": "מסעדות בחיפה"}),
    ("how much is a tesla model 3", I.RESEARCH, {"query": "how much is a tesla model 3"}),
    ("build this application", I.BUILD_APP, {"kind": "app"}),
    ("stop task", I.STOP_TASK, {}),
    ("עצור את המשימה", I.STOP_TASK, {}),
    ("show active agents", I.SHOW_AGENTS, {}),
    ("show all agents", I.SHOW_AGENTS, {}),
    ("תראה לי את הסוכנים", I.SHOW_AGENTS, {}),
    ("הראה סוכנים פעילים", I.SHOW_AGENTS, {}),
    ("תראי את כל הסוכנים", I.SHOW_AGENTS, {}),
    ("הראה את המשימות הפעילות", I.SHOW_TASKS, {}),
    # Agent management
    ("activate Research Agent", I.AGENT_ACTIVATE, {"agent": "research"}),
    ("הפעל את סוכן המחקר", I.AGENT_ACTIVATE, {"agent": "research"}),
    ("הפעל את הסוכן היצירתי", I.AGENT_ACTIVATE, {"agent": "creative"}),
    ("stop Development Agent", I.AGENT_STOP, {"agent": "development"}),
    ("עצור את סוכן הפיתוח", I.AGENT_STOP, {"agent": "development"}),
    ("stop the designer agent", I.AGENT_STOP, {"agent": "creative"}),
    ("give Development Agent this task: write a hello world script", I.AGENT_ASSIGN,
     {"agent": "development", "instruction": "write a hello world script"}),
    ("תן לסוכן הפיתוח את המשימה: לכתוב סקריפט", I.AGENT_ASSIGN, {"agent": "development", "instruction": "לכתוב סקריפט"}),
    ("ask the research agent to find the best GPUs", I.AGENT_ASSIGN, {"agent": "research", "instruction": "find the best GPUs"}),
    ("תבקש מסוכן המחקר לחפש מחירי מחשבים", I.AGENT_ASSIGN, {"agent": "research", "instruction": "לחפש מחירי מחשבים"}),
    ("stop all agents", I.STOP_ALL_AGENTS, {}),
    ("עצור את כל הסוכנים", I.STOP_ALL_AGENTS, {}),
    ("restart Agent 3", I.AGENT_RESTART, {"agent": "development"}),
    ("restart agent number 2", I.AGENT_RESTART, {"agent": "system"}),
    ("הפעל מחדש את סוכן 3", I.AGENT_RESTART, {"agent": "development"}),
    # Kill switch / task control
    ("emergency stop", I.KILL_SWITCH, {}),
    ("עצור הכל", I.KILL_SWITCH, {}),
    ("stop", I.STOP_TASK, {}),
    ("resume the task", I.RESUME_TASK, {}),
    ("תמשיך מאיפה שעצרת", I.RESUME_TASK, {}),
    ("תמשיך", I.RESUME_TASK, {}),
    ("continue", I.RESUME_TASK, {}),
    ("stop all tasks", I.KILL_SWITCH, {}),
    ("cancel all the tasks", I.STOP_TASK, {"all": True}),
    ("בטל את כל המשימות", I.STOP_TASK, {"all": True}),
    ("release the kill switch", I.KILL_RELEASE, {}),
    ("שחרר את מתג החירום", I.KILL_RELEASE, {}),
    ("מותר לחזור לעבוד", I.KILL_RELEASE, {}),
    ("אפשר להמשיך לעבוד", I.RESUME_TASK, {}),       # ambiguous: resuming never releases the kill switch
    ("forget that I like coffee", I.FORGET, {"query": "I like coffee"}),
    ("תשכח שאני אוהב קפה", I.FORGET, {"query": "אני אוהב קפה"}),
    ("close it", I.CLOSE_APP, {"app": "it"}),
    ("תסגור את זה", I.CLOSE_APP, {"app": "זה"}),
    ("תפתח את זה", I.OPEN_APP, {"app": "זה"}),
    ("תבנה את זה", I.BUILD_APP, {}),
    ("show tasks", I.SHOW_TASKS, {}),
    ("על מה אתה עובד", I.SHOW_TASKS, {}),
    ("show history", I.SHOW_HISTORY, {"last": False}),
    ("תציג את המשימה האחרונה", I.SHOW_HISTORY, {"last": True}),
    # Modes
    ("switch to debug mode", I.SET_MODE, {"mode": "debug"}),
    ("עבור למצב שיחה", I.SET_MODE, {"mode": "chat"}),
    ("מצב אוטונומי", I.SET_MODE, {"mode": "autonomous"}),
    ("task mode", I.SET_MODE, {"mode": "task"}),
    # Instant answers
    ("what time is it", I.TIME, {}),
    ("Hey Jarvis, what time is it", I.TIME, {}),
    ("מה השעה", I.TIME, {}),
    ("מה התאריך היום", I.DATE, {}),
    ("help", I.HELP, {}),
    ("מה אתה יכול לעשות", I.HELP, {}),
    ("hello", I.GREETING, {}),
    ("JARVIS", I.GREETING, {}),
    ("תודה רבה", I.THANKS, {}),
    # Windows controls
    ("volume up", I.VOLUME, {"action": "up"}),
    ("תגביר את הווליום", I.VOLUME, {"action": "up"}),
    ("set volume to 40", I.VOLUME, {"action": "set", "level": 40}),
    ("תוריד את הווליום ל-20", I.VOLUME, {"action": "set", "level": 20}),
    ("השתק", I.VOLUME, {"action": "mute"}),
    ("תבטל השתקה", I.VOLUME, {"action": "unmute"}),
    ("set brightness to 70", I.BRIGHTNESS, {"action": "set", "level": 70}),
    ("תגביר את הבהירות", I.BRIGHTNESS, {"action": "up"}),
    ("next song", I.MEDIA, {"action": "next"}),
    ("עצור את המוזיקה", I.MEDIA, {"action": "play_pause"}),
    ("lock the computer", I.LOCK_SCREEN, {}),
    ("נעל את המחשב", I.LOCK_SCREEN, {}),
    ("take a screenshot", I.SCREENSHOT, {}),
    ("צלם את המסך", I.SCREENSHOT, {}),
    ("shut down the computer", I.POWER, {"action": "shutdown"}),
    ("הפעל מחדש את המחשב", I.POWER, {"action": "restart"}),
    ("close chrome", I.CLOSE_APP, {"app": "chrome"}),
    ("סגור את כרום", I.CLOSE_APP, {"app": "chrome"}),
    ("פתח את תיקיית ההורדות", I.OPEN_FOLDER, {"location": "downloads"}),
    ("open the downloads folder", I.OPEN_FOLDER, {"location": "downloads"}),
    ("find the file report.docx", I.FIND_FILES, {"query": "report.docx", "location": None}),
    ("תמצא את הקובץ חשבונית במסמכים", I.FIND_FILES, {"query": "חשבונית", "location": "documents"}),
    # Websites & search
    ("open youtube", I.OPEN_WEBSITE, {"url": "https://www.youtube.com"}),
    ("I want you open YouTube.", I.OPEN_WEBSITE, {"url": "https://www.youtube.com"}),   # as speech came out
    ("תפתח יוטיוב", I.OPEN_WEBSITE, {"url": "https://www.youtube.com"}),
    ("פתח את האתר של ynet", I.OPEN_WEBSITE, {"url": "https://www.ynet.co.il"}),
    ("open github.com/anthropics", I.OPEN_WEBSITE, {"url": "https://github.com/anthropics"}),
    ("google best pizza in tel aviv", I.WEB_SEARCH, {"engine": "google", "query": "best pizza in tel aviv"}),
    ("חפש ביוטיוב שירים של עומר אדם", I.WEB_SEARCH, {"engine": "youtube", "query": "שירים של עומר אדם"}),
    ("play despacito on youtube", I.WEB_SEARCH, {"engine": "youtube", "query": "despacito"}),
    ("what's the weather in Tel Aviv tomorrow", I.WEATHER, {"location": "Tel Aviv", "when": "tomorrow"}),
    ("מה מזג האוויר בחיפה מחר", I.WEATHER, {"location": "חיפה", "when": "tomorrow"}),
    ("מה מזג האוויר", I.WEATHER, {"location": None, "when": "today"}),
    ("תראה לי את מזג האוויר", I.WEATHER, {"location": None, "when": "today"}),
    ("תבדוק את מזג האוויר בחיפה", I.WEATHER, {"location": "חיפה", "when": "today"}),
    ("מה התחזית למחר בחיפה", I.WEATHER, {"location": "חיפה", "when": "tomorrow"}),
    ("show me the weather in London", I.WEATHER, {"location": "London", "when": "today"}),
    # Complex work
    ("תבנה לי אתר לחנות", I.BUILD_APP, {"kind": "store"}),
    ("JARVIS, תבנה לי אתר לחנות", I.BUILD_APP, {"kind": "store"}),
    ("build me a website for a bakery", I.BUILD_APP, {"kind": "website"}),
    ("create a todo app", I.BUILD_APP, {"kind": "app"}),
    ("make me a snake game", I.BUILD_APP, {"kind": "game"}),
    ("build a REST API for todos", I.BUILD_APP, {"kind": "api"}),
    ("צור לי אפליקציה לניהול משימות", I.BUILD_APP, {"kind": "app"}),
    ("פתח אפליקציה לניהול משימות", I.BUILD_APP, {"kind": "app"}),
    ("write a python script that renames files", I.WRITE_CODE, {"language": "python"}),
    ("תכתוב לי סקריפט פייתון שממיין קבצים", I.WRITE_CODE, {"language": "python"}),
    ("fix the bug in my code", I.WRITE_CODE, {}),
    ("design a logo for my coffee shop", I.DESIGN, {}),
    ("תעצב לי לוגו לבית קפה", I.DESIGN, {}),
    ("תכין לי מצגת על בינה מלאכותית", I.DESIGN, {}),
    ("תן לי רעיונות לשם לעסק", I.DESIGN, {}),
    ("write a blog post about AI", I.WRITE_TEXT, {}),
    ("תכתוב לי מייל לבוס שאני חולה", I.WRITE_TEXT, {}),
    ("plan a trip to Japan", I.PLAN, {"goal": "a trip to Japan"}),
    ("תכין לי תוכנית עבודה לשבוע", I.PLAN, {}),
    ("תכין לי תוכנית לחתונה", I.PLAN, {}),
    ("make me a plan for my wedding", I.PLAN, {}),
    ("תכתוב לי תוכנית לחישוב ריבית", I.BUILD_APP, {"kind": "app"}),
    ("organize my project", I.ORGANIZE_PROJECT, {}),
    ("תארגן את הפרויקט", I.ORGANIZE_PROJECT, {}),
    ("תסדר את תיקיית ההורדות", I.ORGANIZE_PROJECT, {"location": "downloads"}),
    ("תארגן את שולחן העבודה", I.ORGANIZE_PROJECT, {"location": "desktop"}),
    ("clean up my downloads folder", I.ORGANIZE_PROJECT, {"location": "downloads"}),
    ("תסדר את הפרויקט חנות קפה", I.ORGANIZE_PROJECT, {"project": "חנות קפה"}),
    # Memory
    ("remember that my favorite color is blue", I.REMEMBER, {"fact": "my favorite color is blue"}),
    ("תזכור שאני אוהב קפה", I.REMEMBER, {"fact": "אני אוהב קפה"}),
    ("what do you remember about coffee", I.RECALL, {"query": "coffee"}),
    # Conversation
    ("ספר לי בדיחה", I.CHAT, {}),
]


@pytest.mark.parametrize(("text", "name", "slots"), CASES, ids=[c[0] for c in CASES])
def test_intent_table(text: str, name: str, slots: dict) -> None:
    intent = detect_intent(text)
    assert intent.name == name, intent.to_dict()
    for key, value in slots.items():
        assert intent.slots.get(key) == value, (key, intent.slots)


def test_agent_hints_follow_spec_examples() -> None:
    # "תבנה לי אתר לחנות" -> Planner, Research, Creative, Development and System.
    store = detect_intent("JARVIS, תבנה לי אתר לחנות")
    assert set(store.agents) == set(AgentId) and store.complexity == "complex"
    # "write an app" -> Development + Planner
    app = detect_intent("create a todo app")
    assert set(app.agents) == {AgentId.PLANNER, AgentId.DEVELOPMENT}
    assert detect_intent("פתח את כרום").agents == [AgentId.SYSTEM]
    assert detect_intent("research quantum computing").agents == [AgentId.RESEARCH]
    assert detect_intent("design a logo").agents == [AgentId.CREATIVE]
    assert set(detect_intent("organize my project").agents) == {AgentId.PLANNER, AgentId.SYSTEM}


def test_wake_word_and_language() -> None:
    i = detect_intent("ג׳ארוויס, מה השעה?")
    assert i.had_wake_word and i.language == "he" and i.name == I.TIME
    e = detect_intent("JARVIS, open notepad")
    assert e.had_wake_word and e.language == "en" and e.text == "open notepad"


def test_unknown_long_open_target_is_not_an_app() -> None:
    i = detect_intent("open the thing I was talking about yesterday with my boss")
    assert i.name != I.OPEN_APP


def test_detection_is_fast() -> None:
    texts = [c[0] for c in CASES]
    start = time.perf_counter()
    for t in texts * 5:
        detect_intent(t)
    per_call_ms = (time.perf_counter() - start) * 1000 / (len(texts) * 5)
    assert per_call_ms < 5, per_call_ms


AI_WORDS: list[tuple[str, str | None, bool, str]] = [
    # A request that names a cloud AI: (command, provider, negated, the rest of the command)
    ("תשתמש בגרוק ותכתוב לי שיר", "xai", False, "תכתוב לי שיר"),
    ("תשאל את גרוק מה הבירה של צרפת", "xai", False, "מה הבירה של צרפת"),
    ("בגרוק: מה הבירה של צרפת", "xai", False, "מה הבירה של צרפת"),
    ("גרוק, תכתוב לי בדיחה", "xai", False, "תכתוב לי בדיחה"),
    ("תכתוב לי שיר עם גרוק", "xai", False, "תכתוב לי שיר"),
    ("תכתוב לי שיר ותשתמש בגרוק", "xai", False, "תכתוב לי שיר"),
    ("תענה בגרוק מה השעה בטוקיו", "xai", False, "תענה מה השעה בטוקיו"),
    ("תבקש מגרוק לסכם את זה", "xai", False, "לסכם את זה"),
    ("שישיתמש בגרוק", "xai", False, ""),                    # the user's own spelling
    ("מעכשיו תשתמש ב-Grok", "xai", False, ""),
    ("use grok to write a poem", "xai", False, "write a poem"),
    ("Use Grok and tell me a joke", "xai", False, "tell me a joke"),
    ("ask Claude what time it is in Tokyo", "anthropic", False, "what time it is in Tokyo"),
    ("תשתמש בקלאוד ותסכם את המייל", "anthropic", False, "תסכם את המייל"),
    ("תשתמש בצ'אט ג'יפיטי ותתרגם", "openai", False, "תתרגם"),
    # ...or tells JARVIS not to use it
    ("אל תשתמש בגרוק", "xai", True, ""),
    ("תפסיק להשתמש בגרוק", "xai", True, ""),
    ("בלי גרוק תפתח את כרום", "xai", True, "תפתח את כרום"),
    # A mention is not a request; "קלאוד" alone is "cloud"
    ("מה זה גרוק?", None, False, "מה זה גרוק?"),
    ("תפתח את גרוק", None, False, "תפתח את גרוק"),
    ("תשמור את זה בקלאוד", None, False, "תשמור את זה בקלאוד"),
    ("תסנכרן עם קלאוד", None, False, "תסנכרן עם קלאוד"),
    ("תפתח את וואטסאפ", None, False, "תפתח את וואטסאפ"),
]


@pytest.mark.parametrize(("text", "provider", "negated", "rest"), AI_WORDS, ids=[c[0] for c in AI_WORDS])
def test_a_named_cloud_ai(text: str, provider: str | None, negated: bool, rest: str) -> None:
    word = named_ai(text)
    assert (word.provider, word.negated, word.rest) == (provider, negated, rest)


def test_the_rest_is_understood_on_its_own() -> None:
    word = named_ai("ג'ארוויס, תשתמש בגרוק ותפתח לי את כרום")
    assert word.provider == "xai" and detect_intent(word.rest).name == I.OPEN_APP
