"""Risk assessment for every tool call.

The executor never trusts the calling agent's own judgement: each call is re-assessed from
what it will actually do (command text, paths, data leaving the machine). The result decides
whether the action runs silently, needs the user's confirmation, or is blocked outright.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jarvis.common.enums import Capability, RiskLevel
from jarvis.permissions.models import ActionRequest
from jarvis.permissions.policies import CAPABILITY_RISK
from jarvis.security.path_guard import PathGuard
from jarvis.security.redaction import Redactor, default_redactor

R = RiskLevel


@dataclass
class Assessment:
    risk: RiskLevel = RiskLevel.SAFE
    reasons: list[str] = field(default_factory=list)
    blocked: bool = False

    def raise_to(self, level: RiskLevel, reason: str) -> None:
        if level > self.risk:
            self.risk = level
        if reason and reason not in self.reasons:
            self.reasons.append(reason)


# (pattern, risk, reason) — evaluated against the full command line, case-insensitive.
_COMMAND_RULES: list[tuple[re.Pattern[str], RiskLevel, str]] = [
    (re.compile(p, re.I), r, why)
    for p, r, why in [
        # destructive / system-breaking
        (r"\bformat(?:\.com)?\s+[a-z]:", R.CRITICAL, "formats a drive"),
        (r"\bdiskpart\b", R.CRITICAL, "disk partitioning"),
        (r"\bbcdedit\b", R.CRITICAL, "changes the boot configuration"),
        (r"\b(?:vssadmin|wbadmin)\s+delete\b", R.CRITICAL, "deletes backups / shadow copies"),
        (r"\bcipher\s+/w\b", R.CRITICAL, "wipes free disk space"),
        (r"\bmkfs(?:\.\w+)?\b|\bdd\s+if=.*\bof=/dev/", R.CRITICAL, "overwrites a disk"),
        (r":\(\)\s*\{\s*:\|:&\s*\};:", R.CRITICAL, "fork bomb"),
        (r"\brm\s+-[a-z]*r[a-z]*\s+(?:-[a-z]+\s+)*(?:/|~|/home|/users|\$home|\*)(?:\s|$)", R.CRITICAL,
         "recursive delete of a root or home folder"),
        (r"\b(?:rd|rmdir|del|erase)\b[^\n]*\s/s\b[^\n]*\s[a-z]:\\?(?:\*|\s|$)", R.CRITICAL, "recursive delete of a drive"),
        (r"\bremove-item\b[^\n]*-recurse[^\n]*(?:\s[a-z]:\\?(?:\s|$|\*)|\$env:(?:userprofile|systemroot|windir)|\s~(?:\s|$|\\))",
         R.CRITICAL, "recursive delete of a drive or user profile"),
        (r"\bset-mppreference\b[^\n]*-disable|\bdisablerealtimemonitoring\b", R.CRITICAL, "disables antivirus protection"),
        (r"\bnetsh\s+advfirewall\s+set\b[^\n]*\bstate\s+off\b|\bset-netfirewallprofile\b[^\n]*-enabled\s+(?:false|0)",
         R.CRITICAL, "turns off the firewall"),
        (r"\breg(?:\.exe)?\s+delete\s+hklm\b|\bremove-item\b[^\n]*hklm:", R.CRITICAL, "deletes machine registry keys"),
        (r"\b(?:iwr|invoke-webrequest|irm|invoke-restmethod|curl|wget)\b[^\n|]*\|\s*(?:iex|invoke-expression|sh|bash|python|pwsh|powershell)\b",
         R.CRITICAL, "downloads and executes code"),
        (r"\$env:\w*(?:api_?key|token|secret|password)\w*|%\w*(?:api_?key|token|secret|password)\w*%", R.CRITICAL,
         "reads secret environment variables"),
        # high: needs confirmation in normal modes
        (r"\b(?:shutdown|stop-computer|restart-computer|reboot|poweroff|halt|logoff)\b", R.HIGH, "power / session action"),
        (r"\bnet\s+(?:user|localgroup)\b|\b(?:new|set|remove)-localuser\b|\badd-localgroupmember\b", R.HIGH,
         "changes user accounts"),
        (r"\breg(?:\.exe)?\s+(?:add|delete|import|load|restore)\b|\b(?:set|new|remove)-itemproperty\b[^\n]*hk(?:lm|cu|cr|u)",
         R.HIGH, "modifies the registry"),
        (r"\s-(?:e|en|enc|encodedcommand)\s+[a-z0-9+/=]{16,}", R.HIGH, "obfuscated (encoded) PowerShell"),
        (r"\b(?:iex|invoke-expression)\b", R.HIGH, "evaluates dynamic code"),
        (r"\bschtasks\s+/create\b|\bregister-scheduledtask\b|\bnew-service\b|\bsc(?:\.exe)?\s+(?:create|delete|config)\b",
         R.HIGH, "creates persistent tasks or services"),
        (r"\b(?:winget|choco|scoop)\s+(?:install|uninstall|upgrade)\b|\bmsiexec\b|\b(?:apt|apt-get|dnf|yum|brew|snap)\s+(?:install|remove|purge)\b",
         R.HIGH, "installs or removes software"),
        (r"\bnpm\s+(?:install|i|add)\b[^\n]*\s(?:-g|--global)\b|\bpip3?\s+install\b[^\n]*--user\b", R.HIGH,
         "installs packages globally"),
        (r"\bset-executionpolicy\b", R.HIGH, "changes the script execution policy"),
        (r"\b(?:takeown|icacls|cacls)\b", R.HIGH, "changes file ownership or permissions"),
        (r"\bclear-recyclebin\b|\$recycle\.bin", R.HIGH, "permanently empties the recycle bin"),
        (r"\b(?:clear-eventlog|wevtutil\s+cl)\b", R.HIGH, "clears system logs"),
        (r"\bgit\s+push\b", R.HIGH, "pushes code to a remote server"),
        (r"\bgit\s+(?:reset\s+--hard|clean\s+-[a-z]*f|checkout\s+--\s+\.|push\s+[^\n]*--force)", R.HIGH,
         "discards git changes"),
        (r"\b(?:ssh|scp|sftp|ftp|rsync)\b\s", R.HIGH, "connects to / copies data to another machine"),
        (r"\b(?:curl|wget|iwr|invoke-webrequest|irm|invoke-restmethod)\b[^\n]*(?:\s-x\s*(?:post|put|patch)|\s--data|\s-d\s|\s-f\s|--form|--upload-file|\s-t\s|-body\b|-method\s+(?:post|put|patch)|-infile\b)",
         R.HIGH, "sends data to the internet"),
        (r"\b(?:rm|del|erase|rd|rmdir|remove-item)\b[^\n]*(?:\s-[a-z]*r[a-z]*\b|\s/s\b|-recurse\b)", R.HIGH, "recursive delete"),
        (r"\btaskkill\b[^\n]*/im\s+\"?(?:explorer|winlogon|csrss|lsass|svchost|wininit|services|smss|dwm)(?:\.exe)?",
         R.CRITICAL, "kills a critical Windows process"),
        # medium
        (r"\b(?:rm|del|erase|rd|rmdir|remove-item|unlink)\b", R.MEDIUM, "deletes files"),
        (r"\b(?:taskkill|stop-process|kill|pkill|killall)\b", R.MEDIUM, "terminates processes"),
        (r"\b(?:pip3?|uv\s+pip|npm|pnpm|yarn|poetry|cargo|dotnet|gem|go)\s+(?:install|add|i)\b", R.MEDIUM,
         "installs project dependencies"),
        (r"\bmklink\b|\bnew-item\b[^\n]*-itemtype\s+(?:symboliclink|junction)", R.MEDIUM, "creates links"),
        (r"\battrib\b[^\n]*[+-][hsr]\b", R.MEDIUM, "changes file attributes"),
        (r"\b(?:move|mv|move-item|ren|rename|rename-item|robocopy|xcopy)\b", R.MEDIUM, "moves or renames files"),
        (r"\b(?:curl|wget|iwr|invoke-webrequest|irm|invoke-restmethod|bitsadmin|certutil\s+-urlcache)\b", R.MEDIUM,
         "downloads from the internet"),
    ]
]

_PII_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "card-like number"),
    (re.compile(r"\b\d{9}\b"), "ID-like number"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "email address"),
    (re.compile(r"(?:\+972|0)(?:[23489]|5\d|7\d)[- ]?\d{3}[- ]?\d{4}\b"), "phone number"),
    (re.compile(r"\b(?:password|passwd|סיסמה|סיסמא)\b", re.I), "password text"),
]


def _luhn_ok(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def analyze_command(command: str) -> Assessment:
    """Risk of a shell / PowerShell command line."""
    a = Assessment(RiskLevel.MEDIUM, ["runs a shell command"])
    for pattern, level, why in _COMMAND_RULES:
        if pattern.search(command):
            a.raise_to(level, why)
    if command.count("&&") + command.count(";") + command.count("|") >= 6:
        a.raise_to(RiskLevel.HIGH, "long chained command")
    return a


def find_sensitive_data(text: str) -> list[str]:
    found: list[str] = []
    for pattern, label in _PII_RULES:
        for m in pattern.finditer(text):
            if label == "card-like number" and not _luhn_ok(m.group(0)):
                continue
            found.append(label)
            break
    return found


class RiskAssessor:
    def __init__(self, path_guard: PathGuard, *, redactor: Redactor | None = None, broad_delete_threshold: int = 20) -> None:
        self.guard = path_guard
        self.redactor = redactor or default_redactor()
        self.broad_delete_threshold = broad_delete_threshold

    def assess(self, req: ActionRequest) -> Assessment:
        a = Assessment(req.risk, list(req.risk_reasons))
        for cap in req.capabilities:
            a.raise_to(CAPABILITY_RISK.get(cap, RiskLevel.MEDIUM), "")
        if req.always_confirm:
            a.raise_to(RiskLevel.HIGH, "always requires confirmation")

        command = req.params.get("command") if isinstance(req.params.get("command"), str) else None
        if command and Capability.SHELL_EXECUTE in req.capabilities:
            sub = analyze_command(command)
            for r in sub.reasons:
                a.raise_to(sub.risk, r)

        for use in req.paths:
            zone = self.guard.zone(use.path)
            if use.access == "delete":
                if self.guard.is_root(use.path):
                    a.raise_to(RiskLevel.CRITICAL, f"deletes a whole root folder ({use.path})")
                elif zone in ("user", "external"):
                    a.raise_to(RiskLevel.HIGH, "deletes files outside the JARVIS workspace")
            elif use.access == "write" and zone == "external":
                a.raise_to(RiskLevel.HIGH, "writes outside the allowed folders")
            elif use.access == "write" and zone == "user" and req.params.get("overwrite"):
                a.raise_to(RiskLevel.MEDIUM, "overwrites a file in your folders")

        count = req.params.get("_affected_count")
        if isinstance(count, int) and count >= self.broad_delete_threshold and Capability.FS_DELETE in req.capabilities:
            a.raise_to(RiskLevel.HIGH, f"broad deletion ({count} items)")

        if req.outbound:
            secrets = self.redactor.find_secrets(req.outbound)
            if secrets:
                a.raise_to(RiskLevel.CRITICAL, "would send secrets/API keys out of the machine")
                a.blocked = True
            sensitive = find_sensitive_data(req.outbound)
            if sensitive:
                a.raise_to(RiskLevel.HIGH, "sends personal data: " + ", ".join(sorted(set(sensitive))))
        return a
