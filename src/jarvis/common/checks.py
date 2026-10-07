"""Check-by-check reports for the self-tests (`jarvis check` and `jarvis voice-test`).

Every check ends as "ok", "warn", "fail" or "skip" with a one-line detail; a check that crashes
becomes a failed check, so one problem never hides the others.
"""

from __future__ import annotations

import contextlib
import logging
import platform
import time
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

STATUS_ORDER = {"ok": 0, "skip": 1, "warn": 2, "fail": 3}
MARKS = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]", "skip": "[SKIP]"}
VERDICTS = {"ok": "All good.", "warn": "Usable, with warnings.", "fail": "Problems found.", "skip": "Nothing checked."}


def short_error(exc: BaseException) -> str:
    text = getattr(exc, "message", None) or str(exc) or type(exc).__name__
    return text.splitlines()[0][:300]


class CheckReport:
    def __init__(self, progress: Callable[[str], None] | None = None) -> None:
        self.checks: list[dict[str, Any]] = []
        self.progress = progress
        self.started = time.monotonic()

    def add(self, name: str, status: str, detail: str, *, seconds: float | None = None, **data: Any) -> dict[str, Any]:
        check: dict[str, Any] = {"name": name, "status": status, "detail": detail,
                                 "seconds": None if seconds is None else round(seconds, 3)}
        if data:
            check["data"] = data
        self.checks.append(check)
        return check

    def note(self, message: str) -> None:
        if self.progress is not None:
            with contextlib.suppress(Exception):
                self.progress(message)

    async def guard(self, name: str, job: Awaitable[Any]) -> Any:
        """Run one check; an unexpected exception becomes a failed check instead of a crash."""
        try:
            return await job
        except Exception as exc:
            log.debug("Self-test check %s crashed", name, exc_info=True)
            self.add(name, "fail", f"unexpected error: {short_error(exc)}")
            return None

    def finish(self) -> dict[str, Any]:
        worst = max((STATUS_ORDER.get(c["status"], 0) for c in self.checks), default=0)
        return {
            "ok": worst < STATUS_ORDER["fail"],
            "status": next(k for k, v in STATUS_ORDER.items() if v == worst),
            "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
            "python": platform.python_version(),
            "seconds": round(time.monotonic() - self.started, 2),
            "checks": self.checks,
        }


def format_checks(title: str, report: dict[str, Any], width: int = 10) -> str:
    width = max([width, *(len(c["name"]) for c in report["checks"])])
    lines = [f"{title} — {report['platform']}, Python {report['python']}", ""]
    for check in report["checks"]:
        took = f" ({check['seconds']:.1f} s)" if check.get("seconds") else ""
        lines.append(f"{MARKS.get(check['status'], check['status'])} {check['name']:<{width}} {check['detail']}{took}")
        for hint in (check.get("data") or {}).get("hints") or []:
            lines.append(f"{'':>{len(MARKS['ok']) + width + 2}}→ {hint}")
    lines += ["", f"{VERDICTS.get(report['status'], report['status'])} ({report['seconds']:.1f} s)"]
    return "\n".join(lines)
