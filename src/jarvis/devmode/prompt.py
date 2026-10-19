"""What the development mode sends to the AI, and what that costs.

One request per click: the user's request (and the error it is about, when there is one), the project
facts from the inspection, the file list, and the full text of the files the user left ticked, with
secret-looking values hidden first. The reply is a `Proposal` (JSON): understanding, plan, assumptions,
questions, search/replace edits, tests advice and a summary.

Fast or strong: a small, clear request on a file or two goes with quick thinking (lower effort: faster
and cheaper); a bigger or vaguer one with deeper thinking. The user can pick either; "auto" decides by
the size of what is sent and the words of the request. Both use the same model.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from jarvis.common.i18n import t

EngineMode = Literal["auto", "fast", "strong"]

MAX_FILE_CHARS = 60_000         # one file's text sent at most (longer files are cut, and the AI is told)
MAX_SEND_CHARS = 160_000        # all files together
MAX_LIST = 400                  # paths in the file list
MAX_ERROR_CHARS = 8_000
CHARS_PER_TOKEN = 3.2           # code and Hebrew are denser than English prose

# Grok list prices in USD per million tokens, read 2026-10-19: morphllm.com/grok-api-pricing (updated
# 2026-08-21, from docs.x.ai) and anotherwrapper.com (grok-4.7 vs grok-4.3). Reasoning tokens bill as output.
PRICES_CHECKED = "2026-10-19"
PRICES: dict[str, tuple[float, float]] = {
    "grok-4.7": (2.00, 6.00),
    "grok-4.6": (2.00, 6.00),
    "grok-4.5": (2.00, 6.00),
    "grok-4.3": (1.25, 2.50),
}
LOCAL = ("local",)

_COMPLEX = re.compile(r"refactor|architecture|redesign|rewrite|migrate|all files|whole project|every|ארכיטקטורה|"
                      r"לשכתב|שכתוב|כל הקבצים|כל הפרויקט|מבנה|להעביר את|מערכת שלמה|מאפס", re.I)


@dataclass(frozen=True)
class Engine:
    provider: str            # "xai", "anthropic", "openai", "local"
    name: str                # "Grok"
    model: str
    effort: str              # "low" (quick thinking) or "high" (deep thinking)
    max_tokens: int
    local: bool

    def label(self, lang: str) -> str:
        how = t(lang, "חשיבה מהירה", "quick thinking") if self.effort == "low" else t(lang, "חשיבה מעמיקה", "deep thinking")
        return f"{self.name} ({self.model}), {how}"


def choose_effort(mode: EngineMode, request: str, files: int, chars: int, error: bool) -> tuple[str, str]:
    """("low" | "high", why) for the request: the user's pick, or auto by size and wording."""
    if mode == "fast":
        return "low", "fast"
    if mode == "strong":
        return "high", "strong"
    if files <= 2 and chars <= 24_000 and len(request) <= 400 and not _COMPLEX.search(request):
        return "low", "small"
    return "high", "large" if files > 2 or chars > 24_000 else "complex"


def effort_reason(why: str, lang: str) -> str:
    return {
        "fast": t(lang, "בחרת מהיר", "you chose fast"),
        "strong": t(lang, "בחרת חזק", "you chose strong"),
        "small": t(lang, "בקשה קטנה על מעט קבצים: חשיבה מהירה מספיקה", "a small request on few files: quick thinking is enough"),
        "large": t(lang, "הרבה קוד או כמה קבצים: חשיבה מעמיקה", "a lot of code or several files: deep thinking"),
        "complex": t(lang, "בקשה רחבה: חשיבה מעמיקה", "a broad request: deep thinking"),
    }[why]


def estimate_tokens(chars: int) -> int:
    return int(chars / CHARS_PER_TOKEN) + 1


def estimate_cost(engine: Engine, prompt_chars: int) -> tuple[float, float] | None:
    """(low, high) USD for one request; None for a local model (free) or a model without a known price."""
    if engine.local:
        return None
    price = PRICES.get(engine.model)
    if price is None:
        return None
    tokens_in = estimate_tokens(prompt_chars) + 1_200
    out_low = 1_500 if engine.effort == "low" else 4_000
    out_high = min(engine.max_tokens, 6_000 if engine.effort == "low" else 16_000)
    low = (tokens_in * price[0] + out_low * price[1]) / 1e6
    high = (tokens_in * price[0] + out_high * price[1]) / 1e6
    return low, high


def actual_cost(engine: Engine, usage: dict[str, int]) -> float | None:
    """What one request cost by the list price (xAI's console shows the real bill). output_tokens includes the
    thinking."""
    price = PRICES.get(engine.model)
    if engine.local or price is None or not usage:
        return None
    return (usage.get("input_tokens", 0) * price[0] + usage.get("output_tokens", 0) * price[1]) / 1e6


def money(value: float) -> str:
    return f"${value:.3f}" if value < 0.1 else f"${value:.2f}"


def cost_text(estimate: tuple[float, float] | None, lang: str, *, local: bool) -> str:
    if local:
        return t(lang, "חינם (מודל מקומי)", "free (local model)")
    if estimate is None:
        return t(lang, "מחיר לא ידוע", "price unknown")
    low, high = estimate
    return t(lang, f"≈ {money(low)}–{money(high)} (הערכה)", f"≈ {money(low)}–{money(high)} (estimate)")


# ============================================================================ the request
SYSTEM = """You are the Development agent of JARVIS, a desktop assistant on the user's Windows computer, working
on the user's own existing project. JARVIS gives you the user's request, facts it gathered about the project
(languages, frameworks, dependencies, commands, coding style), the project's file list, and the full text of
the files chosen as relevant. Plan a focused change and return it as JSON that matches the schema.

Rules:
- Make the smallest change that does what was asked. Do not rewrite, reformat, rename or reorder code the
  request does not need; a focused fix beats a rewrite of working code.
- Keep the project's style: indentation, quotes, semicolons, naming, comment density, and the language of
  comments and user-facing strings.
- Use only the standard library and the libraries listed under the project's dependencies. If the change
  truly needs a new library, say so in `assumptions` and `risks`; never pretend something is installed.
- Do not invent files, functions, APIs, commands or test results. Refer only to files in the file list (you may
  create new files). Files you were not shown must not be modified.
- Edits to existing files are search/replace pairs: `find` is copied exactly from the file as shown (whole
  lines, with their indentation) and long enough to occur exactly once; `replace` is the new text. Prefer
  several small pairs to one big one. A new file goes in `content` with action "create".
- Text shown as [REDACTED...] is a secret JARVIS hid: never put it in `find` or `replace`.
- Where the request is unclear, take the most reasonable reading, state it in `assumptions`, do the parts that
  do not depend on it, and ask in `questions` what you need.
- When the change can be tested and the project has tests (or a test framework among its dependencies), add or
  update tests in `edits` and say in `tests` how the project runs them. Never claim tests pass: JARVIS runs them
  after the user applies the change.
- Write understood, plan, assumptions, questions, summary, risks and every `why` in {language}."""


def build_messages(request: str, facts: dict[str, Any], file_list: list[str], files: list[tuple[str, str, bool]],
                   *, error_text: str = "", previous: dict[str, str] | None = None,
                   lang: str = "he") -> tuple[str, str]:
    """(system, user message). `files` holds (path, text, cut) for each file sent in full; `previous` is the
    change this request follows ({kind: "applied" | "proposed", diff, request})."""
    language = "Hebrew" if lang == "he" else "English"
    system = SYSTEM.replace("{language}", language)
    parts = [f"REQUEST:\n{request.strip()}"]
    if error_text.strip():
        parts.append("ERROR OR TEST OUTPUT (what the user wants fixed):\n" + error_text.strip()[-MAX_ERROR_CHARS:])
    if previous and previous.get("diff", "").strip():
        diff = previous["diff"].strip()[:20_000]
        if previous.get("kind") == "proposed":
            parts.append("YOUR PREVIOUS PROPOSAL, NOT APPLIED: the REQUEST above says how to revise it. Its original "
                         f"request was:\n{previous.get('request', '').strip()[:4000]}\nIts diff (the files are still "
                         f"as shown below, unchanged):\n{diff}")
        else:
            parts.append("THE CHANGE ALREADY APPLIED BEFORE THIS ONE (its diff; the files below include it):\n" + diff)
    parts.append("PROJECT FACTS (found by JARVIS without running anything):\n" + json.dumps(_brief(facts), ensure_ascii=False, indent=1))
    listed = file_list[:MAX_LIST]
    more = len(file_list) - len(listed)
    parts.append(f"FILE LIST ({len(file_list)} files" + (f", first {len(listed)} shown" if more > 0 else "") + "):\n"
                 + "\n".join(listed))
    blocks = []
    for path, text, cut in files:
        lines = text.count("\n") + 1
        note = " — CUT: only the start of the file is shown; do not edit past it" if cut else ""
        blocks.append(f"=== {path} ({lines} lines){note} ===\n{text}")
    parts.append("FILES (full text):\n" + "\n\n".join(blocks) if blocks else "FILES: none were chosen.")
    return system, "\n\n".join(parts)


def _brief(facts: dict[str, Any]) -> dict[str, Any]:
    """The inspection report without what the AI does not need (paths on this computer, timings)."""
    py = facts.get("python") or {}
    node = facts.get("node") or {}
    return {
        "name": facts.get("name"),
        "languages": [x["name"] for x in facts.get("languages", [])],
        "frameworks": facts.get("frameworks", []),
        "python": {k: py.get(k) for k in ("dependencies", "dev_dependencies", "line_length") if py.get(k)} if py.get("present") else None,
        "node": {k: node.get(k) for k in ("dependencies", "dev_dependencies", "scripts", "module") if node.get(k)} if node.get("present") else None,
        "commands": facts.get("commands", {}),
        "style": facts.get("style", {}),
        "tests": {k: v for k, v in (facts.get("tests") or {}).items() if k != "examples"},
    }


__all__ = [
    "MAX_FILE_CHARS",
    "MAX_SEND_CHARS",
    "PRICES",
    "PRICES_CHECKED",
    "Engine",
    "EngineMode",
    "actual_cost",
    "build_messages",
    "choose_effort",
    "cost_text",
    "effort_reason",
    "estimate_cost",
    "estimate_tokens",
    "money",
]
