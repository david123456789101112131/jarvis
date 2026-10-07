"""Start the bot: python main.py (needs TELEGRAM_BOT_TOKEN in the environment or in a .env file)."""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from bot.commands import BotContext
from bot.storage import TodoStore

ROOT = Path(__file__).resolve().parent


def load_dotenv(path: Path) -> None:
    """Read KEY=VALUE lines from .env without overriding variables that are already set."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(ROOT / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("Missing TELEGRAM_BOT_TOKEN. Create a bot with @BotFather, then put the token in a .env file "
              "(see .env.example) or in the environment.", file=sys.stderr)
        return 2
    info = json.loads((ROOT / "project.json").read_text(encoding="utf-8"))
    ctx = BotContext(store=TodoStore(ROOT / "data" / "todos.json"), lang=info.get("lang", "en"),
                     bot_name=info.get("title", "Bot"), description=info.get("description", ""))
    from bot.telegram_adapter import run

    run(token, ctx)
    return 0


if __name__ == "__main__":
    sys.exit(main())
