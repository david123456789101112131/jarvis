"""Stage 12, end to end in a browser: the real interface (the built page the JARVIS window loads) against
a real JARVIS process, clicked and typed into the way the user does.

The browser is the Chromium that Playwright installed (`python -m playwright install chromium`), the
Chromium-based browser JARVIS_TEST_BROWSER points at, or else Microsoft Edge or Google Chrome when
installed (Edge is the engine of the JARVIS window on Windows). The tests are skipped when none starts.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
from e2e_support import AGENTS, Jarvis, exists, until

playwright_api = pytest.importorskip("playwright.async_api")
async_playwright, expect = playwright_api.async_playwright, playwright_api.expect

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(300)]

GL_FLAGS = ["--use-gl=swiftshader", "--enable-unsafe-swiftshader"]     # the 3D core without a GPU


async def _start_browser(p: Any) -> tuple[Any, list[str]]:
    tries: list[dict[str, Any]] = []
    if os.environ.get("JARVIS_TEST_BROWSER"):
        tries.append({"executable_path": os.environ["JARVIS_TEST_BROWSER"]})
    tries += [{}, {"channel": "msedge"}, {"channel": "chrome"}]
    errors: list[str] = []
    for kw in tries:
        try:
            return await p.chromium.launch(args=GL_FLAGS, **kw), errors
        except Exception as exc:            # Playwright's Error: that browser is not installed here
            errors.append(f"{kw or 'playwright chromium'}: {str(exc).splitlines()[0]}")
    return None, errors


@pytest.fixture
async def window(launch: Callable[[], Awaitable[Jarvis]]) -> AsyncIterator[Callable[[], Awaitable[tuple[Jarvis, Any]]]]:
    """Start JARVIS and open its interface in a browser page; console errors fail the test."""
    expect.set_options(timeout=30_000)
    async with async_playwright() as p:
        browser, errors = await _start_browser(p)
        if browser is None:
            pytest.skip("no browser for the UI tests: " + "; ".join(errors))
        problems: list[str] = []

        async def open_window() -> tuple[Jarvis, Any]:
            j = await launch()
            context = await browser.new_context(viewport={"width": 1464, "height": 881}, locale="he-IL")
            page = await context.new_page()
            page.on("pageerror", lambda e: problems.append(f"page error: {e}"))
            page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
            await page.goto(j.launch_url)
            await expect(page.get_by_test_id("app-shell")).to_be_visible()
            return j, page

        try:
            yield open_window
        finally:
            with contextlib.suppress(Exception):
                await browser.close()
        assert not problems, problems


async def type_command(page: Any, text: str) -> None:
    await page.get_by_test_id("chat-input").fill(text)
    await page.keyboard.press("Enter")


async def test_the_window_shows_live_state_and_runs_commands(window) -> None:
    j, page = await window()
    # The launch link was swapped for a session cookie: neither the address bar nor the page holds the token.
    assert "launch=" not in page.url
    assert j.info["token"] not in await page.content()
    for agent in AGENTS:
        await expect(page.get_by_test_id(f"agent-card-{agent}")).to_be_visible()
    await expect(page.get_by_test_id("sys-cpu")).to_contain_text("%")              # live numbers from this PC
    await expect(page.get_by_test_id("connection-indicator")).to_have_attribute("data-status", "open")

    await type_command(page, "צור תיקייה בשם מהחלון")
    await expect(page.locator('[data-testid="action-card"][data-status="done"]')).to_contain_text("מהחלון")
    await expect(page.locator('[data-testid="chat-message"][data-role="jarvis"]').last).to_contain_text("מהחלון")
    assert await asyncio.to_thread(exists, j.workspace / "מהחלון")


async def test_a_task_runs_in_the_window_from_plan_to_result(window) -> None:
    j, page = await window()
    # The agent's own button on its card stops it (its optional "open the site" step is then skipped).
    await page.get_by_test_id("agent-stop-system").click()
    await expect(page.get_by_test_id("agent-card-system")).to_have_attribute("data-status", "disabled")

    await type_command(page, "תבנה לי אתר לחנות פרחים")
    card = page.get_by_test_id("task-item").first
    await expect(card).to_be_visible()
    await expect(card.locator("li.subtask")).to_have_count(6)                    # the plan, step by step
    done = page.locator('[data-testid="recent-task"][data-status="completed"]')
    await expect(done.first).to_contain_text("פרחים", timeout=240_000)

    await page.get_by_test_id("agent-start-system").click()
    await expect(page.get_by_test_id("agent-card-system")).not_to_have_attribute("data-status", "disabled")
    tasks = await j.get("/api/tasks")
    assert tasks[0]["status"] == "completed" and "פרחים" in tasks[0]["title"]


async def test_approvals_and_the_kill_switch_in_the_window(window) -> None:
    j, page = await window()
    old = j.workspace / "ישן"
    await asyncio.to_thread(old.mkdir, parents=True)

    await type_command(page, "תמחק לצמיתות את התיקייה ישן")
    dialog = page.get_by_test_id("confirm-modal")
    await expect(dialog).to_be_visible()
    await expect(dialog.get_by_test_id("confirm-irreversible")).to_be_visible()
    await expect(dialog.get_by_test_id("confirm-remember")).to_have_count(0)     # never "don't ask again" here
    await dialog.get_by_test_id("confirm-deny").click()
    await expect(dialog).to_be_hidden()
    assert await asyncio.to_thread(exists, old)

    await type_command(page, "תמחק לצמיתות את התיקייה ישן")
    await page.get_by_test_id("confirm-modal").get_by_test_id("confirm-approve").click()
    await until(lambda: not old.exists())

    # The kill switch asks once more, then everything stops until it is released.
    await page.get_by_test_id("kill-switch").first.click()
    await page.get_by_test_id("kill-confirm-yes").click()
    await expect(page.get_by_test_id("kill-banner")).to_be_visible()
    assert (await j.get("/api/state"))["kill_switch"]["global"]
    await type_command(page, "צור תיקייה בשם אחרי")
    await expect(page.locator('[data-testid="chat-message"][data-role="jarvis"]').last).to_contain_text("מתג החירום")
    assert not await asyncio.to_thread(exists, j.workspace / "אחרי")
    await page.get_by_test_id("kill-release-top").click()
    await expect(page.get_by_test_id("kill-banner")).to_be_hidden()
    assert not (await j.get("/api/state"))["kill_switch"]["global"]
