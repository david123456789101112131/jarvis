"""Every message the bot sends, in Hebrew and English."""

from __future__ import annotations

TEXTS: dict[str, dict[str, str]] = {
    "he": {
        "start": "שלום {name}! אני {bot}. {description}\nכתבו /help כדי לראות מה אני יודע לעשות.",
        "help": (
            "הפקודות שלי:\n"
            "/help — העזרה הזו\n"
            "/echo <טקסט> — אחזור על מה שכתבתם\n"
            "/todo <משימה> — הוספת משימה\n"
            "/list — המשימות שלכם\n"
            "/done <מספר> — סימון משימה כבוצעה\n"
            "/clear — מחיקת המשימות שבוצעו\n"
            "/time — השעה עכשיו"
        ),
        "echo_empty": "מה לחזור? למשל: /echo שלום",
        "todo_empty": "מה להוסיף? למשל: /todo לקנות חלב",
        "todo_added": "נוסף ✔️ ({count} משימות פתוחות)",
        "list_empty": "אין משימות. הוסיפו עם /todo",
        "list_title": "המשימות שלכם:",
        "done_usage": "איזו משימה? למשל: /done 1",
        "done_missing": "אין משימה מספר {n}",
        "done_ok": "כל הכבוד! ✅ {title}",
        "cleared": "נמחקו {n} משימות שבוצעו",
        "time": "השעה עכשיו {time}",
        "unknown": "לא הכרתי את הפקודה. כתבו /help",
        "text": "קיבלתי: {text}\nאפשר לכתוב /help כדי לראות פקודות.",
    },
    "en": {
        "start": "Hi {name}! I'm {bot}. {description}\nSend /help to see what I can do.",
        "help": (
            "My commands:\n"
            "/help — this help\n"
            "/echo <text> — repeat your text\n"
            "/todo <task> — add a task\n"
            "/list — your tasks\n"
            "/done <number> — mark a task as done\n"
            "/clear — remove finished tasks\n"
            "/time — the current time"
        ),
        "echo_empty": "Echo what? For example: /echo hello",
        "todo_empty": "Add what? For example: /todo buy milk",
        "todo_added": "Added ✔️ ({count} open tasks)",
        "list_empty": "No tasks yet. Add one with /todo",
        "list_title": "Your tasks:",
        "done_usage": "Which task? For example: /done 1",
        "done_missing": "There is no task number {n}",
        "done_ok": "Well done! ✅ {title}",
        "cleared": "Removed {n} finished tasks",
        "time": "It's {time}",
        "unknown": "I don't know that command. Send /help",
        "text": "Got it: {text}\nSend /help to see my commands.",
    },
}
