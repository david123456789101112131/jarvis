"""Connects the command logic to Telegram with python-telegram-bot (v21+)."""

from __future__ import annotations

import logging

from bot.commands import BotContext, handle

log = logging.getLogger("bot")


def run(token: str, ctx: BotContext) -> None:
    try:
        from telegram import Update
        from telegram.ext import Application, ContextTypes, MessageHandler, filters
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise SystemExit("python-telegram-bot is not installed. Run: pip install -r requirements.txt") from exc

    async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        user = update.effective_user
        if message is None or user is None or not message.text:
            return
        reply = handle(message.text, user.id, ctx, user.first_name or "")
        await message.reply_text(reply)

    app = Application.builder().token(token).build()
    app.add_handler(MessageHandler(filters.TEXT, on_message))
    log.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
