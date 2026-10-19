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
from pathlib import Path
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


def _console_error(message: Any, problems: list[str]) -> None:
    if message.type != "error":
        return
    # With no JARVIS window there is no file dialog to open: the server says so (409) and the screen asks for a
    # typed path instead, which is the designed way; the browser still logs the 409 as a failed request.
    if "/api/dialog" in ((message.location or {}).get("url") or "") and "409" in message.text:
        return
    problems.append(f"console.{message.type}: {message.text}")


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
            page.on("console", lambda m: _console_error(m, problems))
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


def _shop(root: Path) -> Path:
    """A small Python project whose own tests are one that passes and one that fails."""
    files = {
        "pyproject.toml": '[project]\nname = "shop"\ndependencies = []\n',
        "shop/__init__.py": "",
        "shop/cart.py": "def total(items):\n    return sum(items)\n",
        "tests/test_cart.py": "from shop.cart import total\n\n\ndef test_empty():\n    assert total([]) == 0\n\n\n"
                              "def test_two_items():\n    assert total([2, 2]) == 5\n",
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root


async def test_the_work_areas_show_what_is_real_and_ask_first(window, tmp_path: Path) -> None:
    """The work areas from the rail, against a real JARVIS with no AI key and no window: the code screen takes a
    typed project path (no file dialog without a window), shows what it found, and runs the project's own tests
    only after the approval, then says what ran, what passed and what failed; the image and video screens say
    xAI is not connected and offer only a demonstration, marked as one, with nothing sent; the apps screen lists
    its ways of working and why each is or is not available; the work history keeps every job; the privacy
    screen counts what is kept and deletes it only on the second click."""
    j, page = await window()
    shop = await asyncio.to_thread(_shop, tmp_path / "shop")

    await page.get_by_test_id("nav-code").click()
    await expect(page.get_by_test_id("code-screen")).to_be_visible()
    await page.get_by_test_id("code-pick-open").click()
    await page.get_by_test_id("code-pick-typed").fill(str(shop))
    await page.get_by_test_id("code-pick-use").click()
    await expect(page.get_by_test_id("code-test-command")).to_contain_text("pytest")
    await page.get_by_test_id("code-run-tests").click()
    dialog = page.get_by_test_id("confirm-modal")
    await expect(dialog).to_be_visible()
    assert not (await j.get("/api/jobs?area=code&kind=dev.tests"))[0]["result"]     # nothing ran before the approval
    await dialog.get_by_test_id("confirm-approve").click()
    await expect(page.get_by_test_id("code-tests-verdict")).to_be_visible(timeout=120_000)
    await expect(page.get_by_test_id("code-count-passed")).to_contain_text("1")
    await expect(page.get_by_test_id("code-count-failed")).to_contain_text("1")
    await expect(page.get_by_test_id("code-failures")).to_contain_text("test_two_items")
    await expect(page.get_by_test_id("code-tests-command")).to_contain_text("pytest")

    await page.get_by_test_id("nav-images").click()
    await expect(page.get_by_test_id("images-unavailable")).to_be_visible()
    await expect(page.get_by_test_id("images-create")).to_have_count(0)            # no button that pretends
    await page.get_by_test_id("images-description").fill("שועל כתום יושב ביער בשקיעה")
    await page.get_by_test_id("images-demo").click()
    await expect(page.get_by_test_id("media-demo-badge").first).to_be_visible()
    images = await j.get("/api/media/items?kind=image")
    assert images and not any(i["real"] for i in images)
    assert all(not job["real"] and not job["sends"] for job in await j.get("/api/jobs?area=images"))

    await page.get_by_test_id("nav-video").click()
    await expect(page.get_by_test_id("video-unavailable")).to_be_visible()
    await expect(page.get_by_test_id("video-create")).to_have_count(0)
    await expect(page.get_by_test_id("video-demo")).to_be_visible()

    await page.get_by_test_id("nav-apps").click()
    await expect(page.get_by_test_id("apps-ways")).to_be_visible()
    status = await j.get("/api/apps/status?lang=he")
    assert [w["key"] for w in status["ways"]] == ["launch", "windows", "uia", "vision", "connectors"]
    assert all(w["available"] or w["reason"] for w in status["ways"])                 # never unavailable silently

    await page.get_by_test_id("nav-hub").click()
    await page.get_by_test_id("hub-tab-jobs").click()
    await expect(page.get_by_test_id("jobs-list").get_by_test_id("job-card")).to_have_count(3)  # look, tests, demo

    await page.get_by_test_id("hub-tab-privacy").click()
    jobs = page.get_by_test_id("privacy-count-jobs")
    await expect(jobs).to_contain_text("3")
    clear = page.get_by_test_id("privacy-clear-jobs")
    await clear.click()
    await asyncio.sleep(0.5)
    assert len(await j.get("/api/jobs")) == 3                                         # the first click only asks
    await clear.click()
    await expect(jobs).to_have_text("אין כלום שמור")
    assert await j.get("/api/jobs") == []
