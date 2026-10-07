"""Command logic: plain functions from (text, user) to a reply, independent of Telegram."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from bot.storage import TodoStore
from bot.texts import TEXTS


@dataclass
class BotContext:
    store: TodoStore
    lang: str = "en"
    bot_name: str = "Bot"
    description: str = ""
    now: Callable[[], datetime] = field(default=datetime.now)

    def t(self, key: str, **values: object) -> str:
        texts = TEXTS.get(self.lang, TEXTS["en"])
        return texts[key].format(**values)


def parse(text: str) -> tuple[str | None, str]:
    """'/todo buy milk' -> ('todo', 'buy milk'); '/start@MyBot' -> ('start', ''); plain text -> (None, text)."""
    text = (text or "").strip()
    if not text.startswith("/"):
        return None, text
    head, _, rest = text.partition(" ")
    command = head[1:].split("@", 1)[0].lower()
    return command, rest.strip()


def handle(text: str, user_id: int, ctx: BotContext, user_name: str = "") -> str:
    command, arg = parse(text)
    if command is None:
        return ctx.t("text", text=arg[:500])
    if command == "start":
        return ctx.t("start", name=user_name or "", bot=ctx.bot_name, description=ctx.description).replace("  ", " ")
    if command == "help":
        return ctx.t("help")
    if command == "echo":
        return arg[:4000] if arg else ctx.t("echo_empty")
    if command == "todo":
        if not arg:
            return ctx.t("todo_empty")
        return ctx.t("todo_added", count=ctx.store.add(user_id, arg))
    if command == "list":
        tasks = ctx.store.tasks(user_id)
        if not tasks:
            return ctx.t("list_empty")
        lines = [f"{i}. {'✅' if t['done'] else '⬜'} {t['title']}" for i, t in enumerate(tasks, 1)]
        return "\n".join([ctx.t("list_title"), *lines])
    if command == "done":
        if not arg.isdigit():
            return ctx.t("done_usage")
        title = ctx.store.mark_done(user_id, int(arg))
        return ctx.t("done_ok", title=title) if title else ctx.t("done_missing", n=arg)
    if command == "clear":
        return ctx.t("cleared", n=ctx.store.clear_done(user_id))
    if command == "time":
        return ctx.t("time", time=ctx.now().strftime("%H:%M"))
    return ctx.t("unknown")
