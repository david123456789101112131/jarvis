"""User-facing text in the two languages JARVIS speaks (Hebrew and English).

Every sentence a person may read (task results, activity log, approval requests, audit entries)
is produced in the language of the request that caused it, chosen with `t(lang, he, en)`.
"""

from __future__ import annotations

# agent id -> (English name, Hebrew name)
AGENT_NAMES: dict[str, tuple[str, str]] = {
    "research": ("Research Agent", "סוכן מחקר"),
    "system": ("System Agent", "סוכן מערכת"),
    "development": ("Development Agent", "סוכן פיתוח"),
    "creative": ("Creative Agent", "סוכן יצירתי"),
    "planner": ("Planner Agent", "סוכן תכנון"),
    "jarvis": ("JARVIS", "JARVIS"),
}


def t(lang: str | None, he: str, en: str) -> str:
    """Pick the Hebrew or English variant of a user-facing sentence."""
    return he if lang == "he" else en


def agent_name(agent_id: str, lang: str | None = "en") -> str:
    names = AGENT_NAMES.get(agent_id)
    if names is None:
        return agent_id
    return t(lang, names[1], names[0])


def plural(lang: str | None, n: int, he_one: str, he_many: str, en_one: str, en_many: str) -> str:
    """A count with the right noun form. `he_one` is the whole phrase for one, because Hebrew puts
    the number after the noun and it agrees in gender: plural(lang, n, "קובץ אחד", "קבצים", "file", "files")."""
    if lang == "he":
        return he_one if n == 1 else f"{n} {he_many}"
    return f"1 {en_one}" if n == 1 else f"{n} {en_many}"


def steps_done(lang: str | None, done: int, total: int) -> str:
    """"2 of 5 steps were completed" as a clause, with the forms Hebrew needs for none and one."""
    if lang == "he":
        if total == 1:
            return "השלב הושלם" if done else "השלב לא הושלם"
        if done == 0:
            return f"אף אחד מ-{total} השלבים לא הושלם"
        if done == 1:
            return f"הושלם שלב אחד מתוך {total}"
        return f"הושלמו {done} מתוך {total} שלבים"
    if total == 1:
        return "the step was completed" if done else "the step was not completed"
    return f"{done} of {total} steps were completed"


def steps_already_done(lang: str | None, done: int, total: int) -> str:
    """": 2 of 5 steps are already done." for a resumed task ("." when nothing was done yet)."""
    if not done or not total:
        return "."
    if lang == "he":
        return f": שלב אחד מתוך {total} כבר הושלם." if done == 1 else f": {done} מתוך {total} שלבים כבר הושלמו."
    return f": 1 of {total} steps is already done." if done == 1 else f": {done} of {total} steps are already done."


def in_seconds(lang: str | None, seconds: float) -> str:
    """"בעוד 5 שניות" / "in 5s"."""
    n = round(seconds)
    if lang == "he":
        return "מיד" if n < 1 else "בעוד שנייה" if n == 1 else f"בעוד {n} שניות"
    return "now" if n < 1 else f"in {n}s"


__all__ = ["AGENT_NAMES", "agent_name", "in_seconds", "plural", "steps_already_done", "steps_done", "t"]
