"""Risk assessment for every tool call.

The executor never trusts the calling agent's own judgement: each call is re-assessed from
what it will actually do (command text, paths, data leaving the machine). The result decides
whether the action runs silently, needs the user's confirmation, or is blocked outright.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jarvis.common.enums import Capability, RiskLevel
from jarvis.common.i18n import t
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


# (pattern, risk, reason in English, reason in Hebrew) — evaluated against the full command line,
# case-insensitive.
_COMMAND_RULES: list[tuple[re.Pattern[str], RiskLevel, str, str]] = [
    (re.compile(p, re.I), r, en, he)
    for p, r, en, he in [
        # destructive / system-breaking
        (r"\bformat(?:\.com)?\s+[a-z]:", R.CRITICAL, "formats a drive", "מפרמט כונן"),
        (r"\bdiskpart\b", R.CRITICAL, "disk partitioning", "משנה את חלוקת המחיצות בדיסק"),
        (r"\bbcdedit\b", R.CRITICAL, "changes the boot configuration", "משנה את הגדרות האתחול"),
        (r"\b(?:vssadmin|wbadmin)\s+delete\b", R.CRITICAL, "deletes backups / shadow copies", "מוחק גיבויים ועותקי צל"),
        (r"\bcipher\s+/w\b", R.CRITICAL, "wipes free disk space", "מוחק את השטח הפנוי בדיסק"),
        (r"\bmkfs(?:\.\w+)?\b|\bdd\s+if=.*\bof=/dev/", R.CRITICAL, "overwrites a disk", "דורס דיסק שלם"),
        (r":\(\)\s*\{\s*:\|:&\s*\};:", R.CRITICAL, "fork bomb", "פצצת תהליכים (fork bomb)"),
        (r"\brm\s+-[a-z]*r[a-z]*\s+(?:-[a-z]+\s+)*(?:/|~|/home|/users|\$home|\*)(?:\s|$)", R.CRITICAL,
         "recursive delete of a root or home folder", "מחיקה רקורסיבית של תיקיית שורש או של תיקיית הבית"),
        (r"\b(?:rd|rmdir|del|erase)\b[^\n]*\s/s\b[^\n]*\s[a-z]:\\?(?:\*|\s|$)", R.CRITICAL, "recursive delete of a drive", "מחיקה רקורסיבית של כונן"),
        (r"\bremove-item\b[^\n]*-recurse[^\n]*(?:\s[a-z]:\\?(?:\s|$|\*)|\$env:(?:userprofile|systemroot|windir)|\s~(?:\s|$|\\))",
         R.CRITICAL, "recursive delete of a drive or user profile", "מחיקה רקורסיבית של כונן או של פרופיל המשתמש"),
        (r"\bset-mppreference\b[^\n]*-disable|\bdisablerealtimemonitoring\b", R.CRITICAL, "disables antivirus protection", "משבית את הגנת האנטי-וירוס"),
        (r"\bnetsh\s+advfirewall\s+set\b[^\n]*\bstate\s+off\b|\bset-netfirewallprofile\b[^\n]*-enabled\s+(?:false|0)",
         R.CRITICAL, "turns off the firewall", "מכבה את חומת האש"),
        (r"\breg(?:\.exe)?\s+delete\s+hklm\b|\bremove-item\b[^\n]*hklm:", R.CRITICAL, "deletes machine registry keys", "מוחק מפתחות רישום של המחשב"),
        (r"\b(?:iwr|invoke-webrequest|irm|invoke-restmethod|curl|wget)\b[^\n|]*\|\s*(?:iex|invoke-expression|sh|bash|python|pwsh|powershell)\b",
         R.CRITICAL, "downloads and executes code", "מוריד קוד ומריץ אותו"),
        (r"\$env:\w*(?:api_?key|token|secret|password)\w*|%\w*(?:api_?key|token|secret|password)\w*%", R.CRITICAL,
         "reads secret environment variables", "קורא משתני סביבה סודיים"),
        # high: needs confirmation in normal modes
        (r"\b(?:shutdown|stop-computer|restart-computer|reboot|poweroff|halt|logoff)\b", R.HIGH, "power / session action", "כיבוי, הפעלה מחדש או התנתקות"),
        (r"\bnet\s+(?:user|localgroup)\b|\b(?:new|set|remove)-localuser\b|\badd-localgroupmember\b", R.HIGH,
         "changes user accounts", "משנה חשבונות משתמש"),
        (r"\breg(?:\.exe)?\s+(?:add|delete|import|load|restore)\b|\b(?:set|new|remove)-itemproperty\b[^\n]*hk(?:lm|cu|cr|u)",
         R.HIGH, "modifies the registry", "משנה את הרישום (Registry)"),
        (r"\s-(?:e|en|enc|encodedcommand)\s+[a-z0-9+/=]{16,}", R.HIGH, "obfuscated (encoded) PowerShell", "פקודת PowerShell מוסתרת (מקודדת)"),
        (r"\b(?:iex|invoke-expression)\b", R.HIGH, "evaluates dynamic code", "מריץ קוד דינמי"),
        (r"\bschtasks\s+/create\b|\bregister-scheduledtask\b|\bnew-service\b|\bsc(?:\.exe)?\s+(?:create|delete|config)\b",
         R.HIGH, "creates persistent tasks or services", "יוצר משימה מתוזמנת או שירות קבוע"),
        (r"\b(?:winget|choco|scoop)\s+(?:install|uninstall|upgrade)\b|\bmsiexec\b|\b(?:apt|apt-get|dnf|yum|brew|snap)\s+(?:install|remove|purge)\b",
         R.HIGH, "installs or removes software", "מתקין או מסיר תוכנה"),
        (r"\bnpm\s+(?:install|i|add)\b[^\n]*\s(?:-g|--global)\b|\bpip3?\s+install\b[^\n]*--user\b", R.HIGH,
         "installs packages globally", "מתקין חבילות לכל המערכת"),
        (r"\bset-executionpolicy\b", R.HIGH, "changes the script execution policy", "משנה את מדיניות הרצת הסקריפטים"),
        (r"\b(?:takeown|icacls|cacls)\b", R.HIGH, "changes file ownership or permissions", "משנה בעלות או הרשאות של קבצים"),
        (r"\bclear-recyclebin\b|\$recycle\.bin", R.HIGH, "permanently empties the recycle bin", "מרוקן את סל המחזור לצמיתות"),
        (r"\b(?:clear-eventlog|wevtutil\s+cl)\b", R.HIGH, "clears system logs", "מוחק את יומני המערכת"),
        (r"\bgit\s+push\b", R.HIGH, "pushes code to a remote server", "דוחף קוד לשרת מרוחק"),
        (r"\bgit\s+(?:reset\s+--hard|clean\s+-[a-z]*f|checkout\s+--\s+\.|push\s+[^\n]*--force)", R.HIGH,
         "discards git changes", "מוחק שינויים ב-Git"),
        (r"\b(?:ssh|scp|sftp|ftp|rsync)\b\s", R.HIGH, "connects to / copies data to another machine", "מתחבר למחשב אחר או מעתיק אליו מידע"),
        (r"\b(?:curl|wget|iwr|invoke-webrequest|irm|invoke-restmethod)\b[^\n]*(?:\s-x\s*(?:post|put|patch)|\s--data|\s-d\s|\s-f\s|--form|--upload-file|\s-t\s|-body\b|-method\s+(?:post|put|patch)|-infile\b)",
         R.HIGH, "sends data to the internet", "שולח מידע לאינטרנט"),
        (r"\b(?:rm|del|erase|rd|rmdir|remove-item)\b[^\n]*(?:\s-[a-z]*r[a-z]*\b|\s/s\b|-recurse\b)", R.HIGH, "recursive delete", "מחיקה רקורסיבית"),
        (r"\btaskkill\b[^\n]*/im\s+\"?(?:explorer|winlogon|csrss|lsass|svchost|wininit|services|smss|dwm)(?:\.exe)?",
         R.CRITICAL, "kills a critical Windows process", "סוגר תהליך חיוני של Windows"),
        # medium
        (r"\b(?:rm|del|erase|rd|rmdir|remove-item|unlink)\b", R.MEDIUM, "deletes files", "מוחק קבצים"),
        (r"\b(?:taskkill|stop-process|kill|pkill|killall)\b", R.MEDIUM, "terminates processes", "סוגר תהליכים"),
        (r"\b(?:pip3?|uv\s+pip|npm|pnpm|yarn|poetry|cargo|dotnet|gem|go)\s+(?:install|add|i)\b", R.MEDIUM,
         "installs project dependencies", "מתקין תלויות של פרויקט"),
        (r"\bmklink\b|\bnew-item\b[^\n]*-itemtype\s+(?:symboliclink|junction)", R.MEDIUM, "creates links", "יוצר קישורים במערכת הקבצים"),
        (r"\battrib\b[^\n]*[+-][hsr]\b", R.MEDIUM, "changes file attributes", "משנה מאפיינים של קבצים"),
        (r"\b(?:move|mv|move-item|ren|rename|rename-item|robocopy|xcopy)\b", R.MEDIUM, "moves or renames files", "מעביר קבצים או משנה את שמם"),
        (r"\b(?:curl|wget|iwr|invoke-webrequest|irm|invoke-restmethod|bitsadmin|certutil\s+-urlcache)\b", R.MEDIUM,
         "downloads from the internet", "מוריד מהאינטרנט"),
    ]
]

_PII_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "card-like number"),
    (re.compile(r"\b\d{9}\b"), "ID-like number"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "email address"),
    (re.compile(r"(?:\+972|0)(?:[23489]|5\d|7\d)[- ]?\d{3}[- ]?\d{4}\b"), "phone number"),
    (re.compile(r"\b(?:password|passwd|סיסמה|סיסמא)\b", re.I), "password text"),
]

_PII_HE = {
    "card-like number": "מספר שנראה כמו כרטיס אשראי",
    "ID-like number": "מספר שנראה כמו תעודת זהות",
    "email address": "כתובת אימייל",
    "phone number": "מספר טלפון",
    "password text": "סיסמה",
}


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


SHELL_COMMAND_REASON = ("runs a shell command", "מריץ פקודת מערכת")


def analyze_command(command: str, lang: str = "en") -> Assessment:
    """Risk of a shell / PowerShell command line (reasons in `lang`)."""
    a = Assessment(RiskLevel.MEDIUM, [t(lang, SHELL_COMMAND_REASON[1], SHELL_COMMAND_REASON[0])])
    for pattern, level, en, he in _COMMAND_RULES:
        if pattern.search(command):
            a.raise_to(level, t(lang, he, en))
    if command.count("&&") + command.count(";") + command.count("|") >= 6:
        a.raise_to(RiskLevel.HIGH, t(lang, "שרשרת פקודות ארוכה", "long chained command"))
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
        lang = req.lang
        a = Assessment(req.risk, list(req.risk_reasons))
        for cap in req.capabilities:
            a.raise_to(CAPABILITY_RISK.get(cap, RiskLevel.MEDIUM), "")
        if req.always_confirm:
            a.raise_to(RiskLevel.HIGH, t(lang, "פעולה שתמיד דורשת אישור", "always requires confirmation"))

        command = req.params.get("command") if isinstance(req.params.get("command"), str) else None
        if command and Capability.SHELL_EXECUTE in req.capabilities:
            sub = analyze_command(command, lang)
            for r in sub.reasons:
                a.raise_to(sub.risk, r)

        for use in req.paths:
            zone = self.guard.zone(use.path)
            if use.access == "delete":
                if self.guard.is_root(use.path):
                    a.raise_to(RiskLevel.CRITICAL, t(lang, f"מוחק תיקיית שורש שלמה ({use.path})",
                                                     f"deletes a whole root folder ({use.path})"))
                elif zone in ("user", "external"):
                    a.raise_to(RiskLevel.HIGH, t(lang, "מוחק קבצים מחוץ לתיקיית העבודה של JARVIS",
                                                 "deletes files outside the JARVIS workspace"))
            elif use.access == "write" and zone == "external":
                a.raise_to(RiskLevel.HIGH, t(lang, "כותב מחוץ לתיקיות המותרות", "writes outside the allowed folders"))
            elif use.access == "write" and zone == "user" and req.params.get("overwrite"):
                a.raise_to(RiskLevel.MEDIUM, t(lang, "דורס קובץ בתיקיות שלך", "overwrites a file in your folders"))

        count = req.params.get("_affected_count")
        if isinstance(count, int) and count >= self.broad_delete_threshold and Capability.FS_DELETE in req.capabilities:
            a.raise_to(RiskLevel.HIGH, t(lang, f"מחיקה רחבה ({count} פריטים)", f"broad deletion ({count} items)"))

        if req.outbound:
            secrets = self.redactor.find_secrets(req.outbound)
            if secrets:
                a.raise_to(RiskLevel.CRITICAL, t(lang, "היה שולח סודות או מפתחות API אל מחוץ למחשב",
                                                 "would send secrets/API keys out of the machine"))
                a.blocked = True
            sensitive = sorted(set(find_sensitive_data(req.outbound)))
            if sensitive:
                a.raise_to(RiskLevel.HIGH, t(lang, "שולח מידע אישי: " + ", ".join(_PII_HE.get(x, x) for x in sensitive),
                                             "sends personal data: " + ", ".join(sensitive)))
        return a
