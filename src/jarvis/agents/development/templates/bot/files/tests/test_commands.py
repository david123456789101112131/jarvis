from __future__ import annotations

from datetime import datetime

import pytest

from bot.commands import BotContext, handle, parse
from bot.storage import TodoStore


@pytest.fixture
def ctx(tmp_path):
    return BotContext(store=TodoStore(tmp_path / "todos.json"), lang="en", bot_name="Helper",
                      description="I keep your tasks.", now=lambda: datetime(2026, 1, 1, 9, 30))


def test_parse():
    assert parse("/todo buy milk") == ("todo", "buy milk")
    assert parse("/START@MyBot") == ("start", "")
    assert parse("hello") == (None, "hello")


def test_start_help_echo_time(ctx):
    assert "Helper" in handle("/start", 1, ctx, "Dana") and "Dana" in handle("/start", 1, ctx, "Dana")
    assert "/todo" in handle("/help", 1, ctx)
    assert handle("/echo hi there", 1, ctx) == "hi there"
    assert "Echo what" in handle("/echo", 1, ctx)
    assert handle("/time", 1, ctx) == "It's 09:30"
    assert "don't know" in handle("/nope", 1, ctx)


def test_todo_flow_per_user(ctx):
    assert "1 open" in handle("/todo buy milk", 1, ctx)
    assert "2 open" in handle("/todo call mom", 1, ctx)
    handle("/todo other user's task", 2, ctx)
    listing = handle("/list", 1, ctx)
    assert "1. ⬜ buy milk" in listing and "other user" not in listing
    assert "buy milk" in handle("/done 1", 1, ctx)
    assert "no task number 9" in handle("/done 9", 1, ctx)
    assert "Which task" in handle("/done x", 1, ctx)
    assert "Removed 1" in handle("/clear", 1, ctx)
    assert "1. ⬜ call mom" in handle("/list", 1, ctx)


def test_tasks_persist(tmp_path):
    store = TodoStore(tmp_path / "todos.json")
    store.add(5, "persist me")
    assert TodoStore(tmp_path / "todos.json").tasks(5)[0]["title"] == "persist me"


def test_hebrew(tmp_path):
    he = BotContext(store=TodoStore(None), lang="he", bot_name="עוזר")
    assert "עוזר" in handle("/start", 1, he)
    assert "נוסף" in handle("/todo לקנות חלב", 1, he)
