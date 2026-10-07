"""Risk assessment for every tool call.

The executor never trusts the calling agent's own judgement: each call is re-assessed from
what it will actually do (command text, paths, data leaving the machine). The result decides
whether the action runs silently, needs the user's confirmation, or is blocked outright.

Two ideas shape the shell rules:
* A command JARVIS cannot recognise as read-only is treated as one that changes things, so it
  needs approval (pattern lists of "bad" commands can always be dodged with an alias or some
  quoting; a short list of known read-only forms cannot).
* Some actions cannot be undone (deleting outside the Recycle Bin, installing software,
  changing system settings, sending data out, hiding what runs). They are flagged
  *irreversible*: they always ask, in every mode, and an approval is never remembered for them.
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
    irreversible: bool = False      # cannot be undone: always asks, approvals are never remembered for it
    read_only: bool = False         # a shell command recognised as only showing information

    def raise_to(self, level: RiskLevel, reason: str, *, irreversible: bool = False) -> None:
        if level > self.risk:
            self.risk = level
        if reason and reason not in self.reasons:
            self.reasons.append(reason)
        if irreversible:
            self.irreversible = True


# (pattern, risk, reason in English, reason in Hebrew, irreversible) — evaluated against the full
# command line, case-insensitive.
_COMMAND_RULES: list[tuple[re.Pattern[str], RiskLevel, str, str, bool]] = [
    (re.compile(p, re.I), r, en, he, irreversible)
    for p, r, en, he, irreversible in [
        # destructive / system-breaking
        (r"\bformat(?:\.com)?\s+[a-z]:", R.CRITICAL, "formats a drive", "מפרמט כונן", True),
        (r"\bdiskpart\b", R.CRITICAL, "disk partitioning", "משנה את חלוקת המחיצות בדיסק", True),
        (r"\b(?:format-volume|clear-disk|initialize-disk|(?:new|remove|resize|set)-partition)\b", R.CRITICAL,
         "formats or repartitions a drive", "מפרמט כונן או משנה את המחיצות בו", True),
        (r"\bbcdedit\b", R.CRITICAL, "changes the boot configuration", "משנה את הגדרות האתחול", True),
        (r"\b(?:vssadmin|wbadmin)\s+delete\b", R.CRITICAL, "deletes backups / shadow copies", "מוחק גיבויים ועותקי צל", True),
        (r"\bcipher\s+/w\b", R.CRITICAL, "wipes free disk space", "מוחק את השטח הפנוי בדיסק", True),
        (r"\bmkfs(?:\.\w+)?\b|\bdd\s+if=.*\bof=/dev/", R.CRITICAL, "overwrites a disk", "דורס דיסק שלם", True),
        (r":\(\)\s*\{\s*:\|:&\s*\};:", R.CRITICAL, "fork bomb", "פצצת תהליכים (fork bomb)", True),
        (r"\brm\s+-[a-z]*r[a-z]*\s+(?:-[a-z]+\s+)*(?:/|~|/home|/users|\$home|\*)(?:\s|$)", R.CRITICAL,
         "recursive delete of a root or home folder", "מחיקה רקורסיבית של תיקיית שורש או של תיקיית הבית", True),
        (r"\b(?:rd|rmdir|del|erase)\b[^\n]*\s/s\b[^\n]*\s[a-z]:\\?(?:\*|\s|$)", R.CRITICAL, "recursive delete of a drive",
         "מחיקה רקורסיבית של כונן", True),
        (r"\b(?:remove-item|ri)\b[^\n]*-r[a-z]*\b[^\n]*(?:\s[a-z]:\\?(?:\s|$|\*)|\$env:(?:userprofile|systemroot|windir)|\s~(?:\s|$|\\))",
         R.CRITICAL, "recursive delete of a drive or user profile", "מחיקה רקורסיבית של כונן או של פרופיל המשתמש", True),
        (r"\b(?:set|add)-mppreference\b[^\n]*-(?:disable|exclusion)|\bdisablerealtimemonitoring\b", R.CRITICAL,
         "weakens antivirus protection", "מחליש את הגנת האנטי-וירוס", True),
        (r"\bmanage-bde\b(?![^\n]*\s-status\b)|\b(?:disable|suspend)-bitlocker\b", R.CRITICAL,
         "turns off or changes drive encryption (BitLocker)", "מכבה או משנה את הצפנת הכונן (BitLocker)", True),
        (r"\bnetsh\s+advfirewall\s+set\b[^\n]*\bstate\s+off\b|\bset-netfirewallprofile\b[^\n]*-enabled\s+(?:false|0)",
         R.CRITICAL, "turns off the firewall", "מכבה את חומת האש", True),
        (r"\breg(?:\.exe)?\s+delete\s+hklm\b|\bremove-item\b[^\n]*hklm:", R.CRITICAL, "deletes machine registry keys",
         "מוחק מפתחות רישום של המחשב", True),
        (r"\b(?:iwr|invoke-webrequest|irm|invoke-restmethod|curl|wget)\b[^\n|]*\|\s*(?:iex|invoke-expression|sh|bash|python|pwsh|powershell)\b",
         R.CRITICAL, "downloads and executes code", "מוריד קוד ומריץ אותו", True),
        (r"\b(?:iwr|invoke-webrequest|irm|invoke-restmethod|curl|wget|start-bitstransfer)\b[^\n]*(?:;|&&|\|\|)[^\n]*"
         r"\.(?:exe|msi|ps1|bat|cmd|vbs|js|scr|hta)\b",
         R.HIGH, "downloads a program and runs it", "מוריד תוכנה ומריץ אותה", True),
        (r"\$env:\w*(?:api_?key|token|secret|password)\w*|%\w*(?:api_?key|token|secret|password)\w*%", R.CRITICAL,
         "reads secret environment variables", "קורא משתני סביבה סודיים", True),
        (r"\btaskkill\b[^\n]*/im\s+\"?(?:explorer|winlogon|csrss|lsass|svchost|wininit|services|smss|dwm)(?:\.exe)?",
         R.CRITICAL, "kills a critical Windows process", "סוגר תהליך חיוני של Windows", True),
        # stored passwords, keys and JARVIS's own control files
        (r"\bnetsh\s+wlan\s+(?:show|export)\s+profile[^\n]*\bkey\s*=\s*clear\b", R.CRITICAL,
         "reveals saved Wi-Fi passwords", "חושף סיסמאות Wi-Fi שמורות", True),
        (r"\bmimikatz\b|\bprocdump\b[^\n]*\blsass\b|\bcomsvcs(?:\.dll)?\b[^\n]*\bminidump\b|"
         r"\breg(?:\.exe)?\s+save\s+hklm\\(?:sam|system|security)\b|\bvaultcmd\b",
         R.CRITICAL, "reads the passwords Windows keeps", "קורא את הסיסמאות ש-Windows שומר", True),
        (r"login data|\bkey[34]\.db\b|\blogins\.json\b|\\cookies\b|\blocal state\b", R.CRITICAL,
         "reads saved browser passwords or cookies", "קורא סיסמאות או עוגיות שמורות של הדפדפן", True),
        (r"[\\/]\.ssh[\\/]|\.aws[\\/]credentials|\.git-credentials\b|[\\/]\.netrc\b|[\\/]\.npmrc\b|[\\/]\.pypirc\b",
         R.CRITICAL, "reads stored keys or passwords", "קורא מפתחות או סיסמאות שמורים", True),
        (r"\b(?:runtime|permissions)\.json\b|\bsecrets\.env\b", R.CRITICAL,
         "touches JARVIS's own control files", "נוגע בקבצי הבקרה של JARVIS עצמו", True),
        # hiding what runs: always asked, never covered by a remembered approval
        (r"\s-e(?:c|n[a-z]*)?\s+[a-z0-9+/=]{16,}", R.HIGH, "obfuscated (encoded) PowerShell",
         "פקודת PowerShell מוסתרת (מקודדת)", True),
        (r"\b(?:iex|invoke-expression)\b", R.HIGH, "evaluates dynamic code", "מריץ קוד דינמי", True),
        (r"\[char\]\s*\d|frombase64string|\[convert\]::|-bxor\b|\[scriptblock\]::|\binvoke-command\b|"
         r"\[(?:system\.)?reflection\.assembly\]::load|"
         r"(?:^|[\s;(&|])(?:&|\.)\s*\(|['\"]\s*\+\s*['\"]",
         R.HIGH, "hides what it runs", "מסתיר מה הוא מריץ", True),
        (r"\b(?:mshta|rundll32|regsvr32|wscript|cscript|installutil|msbuild|cmstp|regasm|regsvcs|msxsl|wmic)(?:\.exe)?\b|"
         r"\bcertutil\b[^\n]*-(?:urlcache|decode|encode)|\bbitsadmin\b|\bdownload(?:string|file)\b|\bstart-bitstransfer\b|"
         r"\bnew-object\b[^\n]*net\.webclient",
         R.HIGH, "uses a Windows program that malware often abuses", "משתמש בתוכנת Windows שתוכנות זדוניות מנצלות לעתים קרובות", True),
        (r"\bstart-process\b[^\n]*-verb\s+['\"]?runas\b|\brunas(?:\.exe)?\s", R.HIGH, "asks for administrator rights",
         "מבקש הרשאות מנהל", True),
        # system changes that cannot simply be undone
        (r"\b(?:shutdown|stop-computer|restart-computer|reboot|poweroff|halt|logoff)\b", R.HIGH, "power / session action",
         "כיבוי, הפעלה מחדש או התנתקות", True),
        (r"\bnet\s+(?:user|localgroup)\b|\b(?:new|set|remove)-localuser\b|\badd-localgroupmember\b", R.HIGH,
         "changes user accounts", "משנה חשבונות משתמש", True),
        (r"\breg(?:\.exe)?\s+(?:add|delete|import|load|restore|copy)\b|\b(?:set|new|remove)-itemproperty\b[^\n]*hk(?:lm|cu|cr|u)|"
         r"\bregedit(?:\.exe)?\s+/s\b",
         R.HIGH, "modifies the registry", "משנה את הרישום (Registry)", True),
        (r"\bschtasks\s+/(?:create|change|delete)\b|\bregister-scheduledtask\b|\bnew-service\b|\bset-service\b|"
         r"\bsc(?:\.exe)?\s+(?:create|delete|config)\b",
         R.HIGH, "creates or changes scheduled tasks or services", "יוצר או משנה משימה מתוזמנת או שירות", True),
        (r"\bnetsh\b(?![^\n]*\b(?:show|dump)\b)|\b(?:set|new|remove)-net\w+\b|\broute\s+(?:add|delete|change)\b|"
         r"\b(?:disable|enable|rename|restart)-netadapter\w*\b",
         R.HIGH, "changes network settings", "משנה הגדרות רשת", True),
        (r"\b(?:winget|choco|scoop)\s+(?:install|uninstall|upgrade|remove)\b|\bmsiexec\b|\b(?:install|uninstall)-(?:package|module)\b|"
         r"\b(?:add|remove)-appxpackage\b|\b(?:apt|apt-get|dnf|yum|brew|snap)\s+(?:install|remove|purge)\b|"
         r"\bdism(?:\.exe)?\b[^\n]*/(?:online|image)\b|\b(?:enable|disable)-windowsoptionalfeature\b|"
         r"\b(?:install|uninstall)-windowsfeature\b|\bwsl(?:\.exe)?\b[^\n]*--install\b",
         R.HIGH, "installs or removes software", "מתקין או מסיר תוכנה", True),
        (r"\bnpm\s+(?:install|i|add)\b[^\n]*\s(?:-g|--global)\b|\bpip3?\s+install\b[^\n]*--user\b", R.HIGH,
         "installs packages globally", "מתקין חבילות לכל המערכת", True),
        (r"\bset-executionpolicy\b", R.HIGH, "changes the script execution policy", "משנה את מדיניות הרצת הסקריפטים", True),
        (r"\b(?:takeown|icacls|cacls)\b|\bset-acl\b", R.HIGH, "changes file ownership or permissions",
         "משנה בעלות או הרשאות של קבצים", True),
        (r"\bclear-recyclebin\b|\$recycle\.bin", R.HIGH, "permanently empties the recycle bin", "מרוקן את סל המחזור לצמיתות", True),
        (r"\b(?:clear-eventlog|wevtutil\s+cl)\b", R.HIGH, "clears system logs", "מוחק את יומני המערכת", True),
        # data leaving the computer
        (r"\bgit\s+push\b", R.HIGH, "pushes code to a remote server", "דוחף קוד לשרת מרוחק", True),
        (r"\b(?:npm|pnpm|yarn)\s+publish\b|\b(?:twine\s+upload|poetry\s+publish|cargo\s+publish|gem\s+push)\b|"
         r"\bdotnet\s+nuget\s+push\b|\b(?:docker|podman)\s+push\b|"
         r"\bgh\s+(?:release\s+create|repo\s+create|gist\s+create|pr\s+create|issue\s+create)\b",
         R.HIGH, "publishes to the internet", "מפרסם לאינטרנט", True),
        (r"\bgit\s+(?:reset\s+--hard|clean\s+-[a-z]*f|checkout\s+(?:-f\b|--\s|\.(?:\s|$))|restore\b(?![^\n]*--staged)|"
         r"restore\b[^\n]*--worktree|stash\s+(?:drop|clear)|branch\s+-[a-z]*D)",
         R.HIGH, "discards git changes", "מוחק שינויים ב-Git", True),
        (r"\b(?:ssh|scp|sftp|ftp|tftp|rsync)\b\s|\b(?:rclone|azcopy)\b|\baws\s+s3\s+(?:cp|sync|mv)\b|"
         r"\bgsutil\s+(?:-m\s+)?(?:cp|rsync|mv)\b|\b(?:nc|ncat|netcat|socat)\s", R.HIGH,
         "connects to / copies data to another machine",
         "מתחבר למחשב אחר או מעתיק אליו מידע", True),
        (r"\b(?:curl|wget|iwr|invoke-webrequest|irm|invoke-restmethod)\b[^\n]*(?:\s-x\s*(?:post|put|patch)|\s--data|\s-d\s|\s-f\s|--form|--upload-file|\s-t\s|-body\b|-method\s+(?:post|put|patch)|-infile\b)",
         R.HIGH, "sends data to the internet", "שולח מידע לאינטרנט", True),
        (r"\bsend-mailmessage\b|\bblat\b|-comobject\s+['\"]?outlook\.application", R.HIGH, "sends email",
         "שולח דואר אלקטרוני", True),
        # deleting: a shell command skips the Recycle Bin, so nothing can be restored
        (r"\bwsl(?:\.exe)?\b[^\n]*--unregister\b", R.HIGH, "deletes a Linux (WSL) system with all its files",
         "מוחק מערכת לינוקס (WSL) עם כל הקבצים שבה", True),
        (r"\b(?:docker|podman)\b[^\n]*\bprune\b", R.HIGH, "deletes Docker containers, images or data",
         "מוחק קונטיינרים, תמונות או נתונים של Docker", True),
        (r"\b(?:rm|del|erase|rd|rmdir|remove-item|ri)\b[^\n]*(?:\s-[a-z]*r[a-z]*\b|\s/s\b)", R.HIGH, "recursive delete",
         "מחיקה רקורסיבית", True),
        (r"\b(?:rm|del|erase|rd|rmdir|remove-item|ri|unlink|shred|rmtree)\b|\bclear-content\b|(?:\.|::)delete\(|"
         r"\bos\.remove\b|\.unlink\(", R.HIGH,
         "deletes files without the Recycle Bin", "מוחק קבצים בלי סל המחזור", True),
        # medium
        (r"\b(?:taskkill|stop-process|kill|pkill|killall)\b", R.MEDIUM, "terminates processes", "סוגר תהליכים", False),
        (r"\b(?:pip3?|uv\s+pip|npm|pnpm|yarn|poetry|cargo|dotnet|gem|go)\s+(?:install|add|i)\b", R.MEDIUM,
         "installs project dependencies", "מתקין תלויות של פרויקט", False),
        (r"\bmklink\b|\bnew-item\b[^\n]*-itemtype\s+(?:symboliclink|junction)", R.MEDIUM, "creates links",
         "יוצר קישורים במערכת הקבצים", False),
        (r"\battrib\b[^\n]*[+-][hsr]\b", R.MEDIUM, "changes file attributes", "משנה מאפיינים של קבצים", False),
        (r"\b(?:move|mv|move-item|ren|rename|rename-item|robocopy|xcopy)\b", R.MEDIUM, "moves or renames files",
         "מעביר קבצים או משנה את שמם", False),
        (r"\b(?:curl|wget|iwr|invoke-webrequest|irm|invoke-restmethod|bitsadmin|certutil\s+-urlcache)\b", R.MEDIUM,
         "downloads from the internet", "מוריד מהאינטרנט", False),
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
UNKNOWN_COMMAND_REASON = ("a command JARVIS cannot confirm is read-only", "פקודה ש-JARVIS לא יכול לוודא שהיא רק קוראת מידע")


# ----------------------------------------------------------------------------------------- read-only commands
# A command counts as read-only only in one of these exact shapes. Anything else needs approval,
# so the list can stay short and strict: a missing entry costs one click, a wrong one could cost data.
_INFO_PROGRAMS = frozenset({
    "dir", "ls", "gci", "get-childitem", "tree", "get-location", "gl", "pwd", "where", "where.exe", "which",
    "get-command", "gcm", "whoami", "hostname", "ver", "systeminfo", "get-computerinfo", "get-process", "gps", "ps",
    "tasklist", "get-service", "gsv", "get-date", "get-volume", "get-psdrive", "gdr", "get-disk", "get-netipaddress",
    "get-netadapter", "get-netipconfiguration", "ping", "test-connection", "nslookup", "resolve-dnsname", "tracert",
    "traceroute", "pathping", "netstat", "get-nettcpconnection", "getmac", "driverquery", "get-hotfix", "uname", "df",
    "free", "uptime", "nvidia-smi", "echo", "write-output", "write-host", "test-path", "get-item", "gi", "get-uptime",
    "get-timezone", "get-culture", "get-host", "get-printer", "get-pnpdevice", "get-physicaldisk", "lsblk", "lscpu",
})
_VERSIONED = frozenset({
    "python", "python3", "py", "pip", "pip3", "node", "npm", "npx", "pnpm", "yarn", "git", "dotnet", "java", "javac",
    "code", "go", "cargo", "rustc", "gcc", "g++", "clang", "cmake", "docker", "winget", "choco", "ruby", "perl", "php",
    "deno", "bun", "uv", "poetry", "conda", "gh", "az", "aws", "gcloud", "kubectl", "terraform", "ffmpeg", "tsc", "vite",
})
_VERSION_FLAGS = frozenset({"--version", "-v", "-version", "version", "-V"})
_FILTERS = frozenset({
    "select-object", "select", "sort-object", "format-table", "ft", "format-list", "fl", "format-wide", "fw",
    "measure-object", "measure", "out-string", "convertto-json", "convertto-csv", "select-string", "sls", "findstr",
    "more", "head", "tail", "sort", "wc",
})
_SPECIAL_UNQUOTED = set(";&><{}()[]@^!")
_ANYWHERE = set("$`%\n\r\x00")
_FILTER_ARG = re.compile(r"^(?:-(?:[a-z][\w-]*|\d+)|/[a-z]{1,3}|\d+|[\w*][\w,*-]*)$", re.I)


def _split_command(command: str) -> list[list[str]] | None:
    """Pipeline segments → tokens, honouring quotes. None when the line uses shell syntax beyond words and pipes."""
    if any(ch in _ANYWHERE for ch in command):
        return None
    segments: list[list[str]] = [[]]
    token: list[str] = []
    quote: str | None = None
    started = False
    for ch in command:
        if quote:
            if ch == quote:
                quote = None
            else:
                token.append(ch)
            continue
        if ch in ("'", '"'):
            quote, started = ch, True
        elif ch in _SPECIAL_UNQUOTED:
            return None
        elif ch == "|":
            if started or token:
                segments[-1].append("".join(token))
            token, started = [], False
            segments.append([])
        elif ch.isspace():
            if started or token:
                segments[-1].append("".join(token))
            token, started = [], False
        else:
            token.append(ch)
            started = True
    if quote:
        return None
    if started or token:
        segments[-1].append("".join(token))
    return segments if all(segments) else None


def _program(token: str) -> str | None:
    """A bare program name ("ipconfig", "Get-Process"); None for a path, so a planted `.\\dir.exe` never passes."""
    if any(sep in token for sep in ("\\", "/", ":")):
        return None
    name = token.lower()
    return name[:-4] if name.endswith(".exe") else name


def _read_only_first(prog: str, args: list[str]) -> bool:
    lower = [a.lower() for a in args]
    if prog == "hostname":
        return not args                      # with an argument it would rename the computer
    if prog in _INFO_PROGRAMS:
        return True
    if prog == "ipconfig":
        return all(a in ("/all", "-all", "/displaydns") for a in lower)
    if prog == "arp":
        return lower in ([], ["-a"], ["/a"])
    if prog == "route":
        return lower[:1] == ["print"]
    if prog in _VERSIONED and len(args) == 1 and args[0] in _VERSION_FLAGS:
        return True
    pip_args = lower
    if prog in ("python", "python3", "py") and lower[:2] == ["-m", "pip"]:
        prog, pip_args = "pip", lower[2:]
    if prog in ("pip", "pip3"):
        return pip_args[:1] in (["list"], ["freeze"], ["show"]) and all(not a.startswith("--") or a in (
            "--outdated", "--format=json", "--format=columns", "--files", "--verbose") for a in pip_args[1:])
    if prog == "npm":
        return lower[:1] in (["ls"], ["list"], ["outdated"], ["view"])
    if prog in ("winget", "choco"):
        return lower[:1] in (["list"], ["search"], ["show"], ["info"])
    return False


def is_read_only_command(command: str) -> bool:
    """True only for a command in one of the known shapes that just show information."""
    segments = _split_command(command.strip())
    if not segments:
        return False
    first = _program(segments[0][0])
    if first is None or not _read_only_first(first, segments[0][1:]):
        return False
    for seg in segments[1:]:
        prog = _program(seg[0])
        if prog not in _FILTERS:
            return False
        if any(not _FILTER_ARG.match(a) or a.lower() in ("-path", "-literalpath") for a in seg[1:]):
            return False
    return True


def command_scope(command: str) -> str:
    """What "remember for this task" covers for a command: the programs it starts ("npm", "npm+python").

    A line that does more than start programs is remembered as exactly that line."""
    programs: set[str] = set()
    for part in re.split(r"\|\||&&|[;|&\n]", command):
        tokens = part.strip().split()
        if not tokens:
            continue
        name = re.split(r"[\\/]", tokens[0].strip("'\""))[-1].lower()
        name = re.sub(r"\.(?:exe|cmd|bat|com|ps1)$", "", name)
        if not re.fullmatch(r"[\w.+-]+", name):
            return command.strip()
        programs.add(name)
    return "+".join(sorted(programs)) or command.strip()


def analyze_command(command: str, lang: str = "en") -> Assessment:
    """Risk of a shell / PowerShell command line (reasons in `lang`)."""
    a = Assessment(RiskLevel.MEDIUM, [t(lang, SHELL_COMMAND_REASON[1], SHELL_COMMAND_REASON[0])])
    for pattern, level, en, he, irreversible in _COMMAND_RULES:
        if pattern.search(command):
            a.raise_to(level, t(lang, he, en), irreversible=irreversible)
    if command.count("&&") + command.count(";") + command.count("|") >= 6:
        a.raise_to(RiskLevel.HIGH, t(lang, "שרשרת פקודות ארוכה", "long chained command"))
    a.read_only = a.risk <= RiskLevel.MEDIUM and not a.irreversible and is_read_only_command(command)
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
        if req.irreversible:
            a.raise_to(RiskLevel.HIGH, "", irreversible=True)

        command = req.params.get("command") if isinstance(req.params.get("command"), str) else None
        if command and Capability.SHELL_EXECUTE in req.capabilities:
            sub = analyze_command(command, lang)
            for r in sub.reasons:
                a.raise_to(sub.risk, r, irreversible=sub.irreversible)
            if not sub.read_only or req.params.get("shell") == "cmd":   # cmd runs a same-named file from the folder first
                a.raise_to(RiskLevel.HIGH, t(lang, UNKNOWN_COMMAND_REASON[1], UNKNOWN_COMMAND_REASON[0]))
            low = command.lower()
            for root in self.guard.protected_roots:
                if str(root).lower() in low:
                    a.raise_to(RiskLevel.CRITICAL, t(lang, "נוגע בקבצים של JARVIS עצמו (הגדרות, מפתחות, יומנים)",
                                                     "touches JARVIS's own files (settings, keys, logs)"), irreversible=True)
                    break

        deleting = False
        for use in req.paths:
            zone = self.guard.zone(use.path)
            if use.access == "delete":
                deleting = True
                if self.guard.is_root(use.path):
                    a.raise_to(RiskLevel.CRITICAL, t(lang, f"מוחק תיקיית שורש שלמה ({use.path})",
                                                     f"deletes a whole root folder ({use.path})"), irreversible=True)
                elif zone in ("user", "external"):
                    a.raise_to(RiskLevel.HIGH, t(lang, "מוחק קבצים מחוץ לתיקיית העבודה של JARVIS",
                                                 "deletes files outside the JARVIS workspace"))
            elif use.access == "write" and zone == "external":
                a.raise_to(RiskLevel.HIGH, t(lang, "כותב מחוץ לתיקיות המותרות", "writes outside the allowed folders"))
            elif use.access == "write" and zone == "user" and req.params.get("overwrite"):
                a.raise_to(RiskLevel.MEDIUM, t(lang, "דורס קובץ בתיקיות שלך", "overwrites a file in your folders"))

        count = req.params.get("_affected_count")
        if isinstance(count, int) and count >= self.broad_delete_threshold:
            if deleting:
                a.raise_to(RiskLevel.HIGH, t(lang, f"מחיקה רחבה ({count} פריטים)", f"broad deletion ({count} items)"),
                           irreversible=True)
            elif Capability.FS_DELETE in req.capabilities or Capability.FS_WRITE in req.capabilities:
                a.raise_to(RiskLevel.HIGH, t(lang, f"פעולה על הרבה פריטים ({count})", f"affects many items ({count})"))

        if req.outbound:
            secrets = self.redactor.find_secrets(req.outbound)
            if secrets:
                a.raise_to(RiskLevel.CRITICAL, t(lang, "היה שולח סודות או מפתחות API אל מחוץ למחשב",
                                                 "would send secrets/API keys out of the machine"), irreversible=True)
                a.blocked = True
            sensitive = sorted(set(find_sensitive_data(req.outbound)))
            if sensitive:
                a.raise_to(RiskLevel.HIGH, t(lang, "שולח מידע אישי: " + ", ".join(_PII_HE.get(x, x) for x in sensitive),
                                             "sends personal data: " + ", ".join(sensitive)), irreversible=True)
        return a
