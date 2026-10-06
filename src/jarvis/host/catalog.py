"""Catalog of well-known applications and websites with Hebrew/English aliases.

Used by intent detection ("פתח לי את כרום" -> chrome) and by the host app launcher, which
combines these hints with the apps actually installed on the machine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jarvis.common.text import normalize


@dataclass(frozen=True)
class AppEntry:
    id: str
    display: str
    aliases: tuple[str, ...]
    windows: tuple[str, ...] = ()   # executables / shell targets to try, in order
    posix: tuple[str, ...] = ()
    process_names: tuple[str, ...] = ()
    uri: str | None = None          # protocol launch (ms-settings:, etc.)
    kind: str = "app"


@dataclass(frozen=True)
class SiteEntry:
    id: str
    display: str
    url: str
    aliases: tuple[str, ...] = field(default_factory=tuple)


APPS: tuple[AppEntry, ...] = (
    AppEntry("chrome", "Google Chrome", ("chrome", "google chrome", "כרום", "גוגל כרום", "קרום"),
             ("chrome.exe",), ("google-chrome", "chromium", "chromium-browser"), ("chrome.exe", "chrome")),
    AppEntry("edge", "Microsoft Edge", ("edge", "microsoft edge", "אדג'", "אדג", "מיקרוסופט אדג'", "אדג׳"),
             ("msedge.exe",), ("microsoft-edge",), ("msedge.exe", "msedge")),
    AppEntry("firefox", "Firefox", ("firefox", "פיירפוקס", "פיר פוקס"), ("firefox.exe",), ("firefox",), ("firefox.exe", "firefox")),
    AppEntry("notepad", "Notepad", ("notepad", "פנקס רשימות", "נוטפד", "פנקס", "כתבן"), ("notepad.exe",), ("gedit", "kate", "mousepad"),
             ("notepad.exe", "gedit")),
    AppEntry("calculator", "Calculator", ("calculator", "calc", "מחשבון"), ("calc.exe",), ("gnome-calculator", "kcalc"),
             ("CalculatorApp.exe", "calc.exe", "gnome-calculator")),
    AppEntry("explorer", "File Explorer", ("explorer", "file explorer", "files", "סייר הקבצים", "סייר קבצים", "סייר", "קבצים",
                                            "תיקיות"), ("explorer.exe",), ("xdg-open",), ("explorer.exe",)),
    AppEntry("settings", "Settings", ("settings", "windows settings", "הגדרות", "הגדרות מערכת", "הגדרות המחשב"),
             (), ("gnome-control-center",), ("SystemSettings.exe",), uri="ms-settings:"),
    AppEntry("word", "Microsoft Word", ("word", "microsoft word", "וורד", "ורד"), ("winword.exe",), ("libreoffice --writer",), ("WINWORD.EXE",)),
    AppEntry("excel", "Microsoft Excel", ("excel", "microsoft excel", "אקסל"), ("excel.exe",), ("libreoffice --calc",), ("EXCEL.EXE",)),
    AppEntry("powerpoint", "PowerPoint", ("powerpoint", "פאוורפוינט", "פאואר פוינט", "מצגות"), ("powerpnt.exe",), ("libreoffice --impress",),
             ("POWERPNT.EXE",)),
    AppEntry("outlook", "Outlook", ("outlook", "אאוטלוק", "אוטלוק"), ("outlook.exe", "olk.exe"), (), ("OUTLOOK.EXE", "olk.exe")),
    AppEntry("teams", "Microsoft Teams", ("teams", "microsoft teams", "טימס"), ("ms-teams.exe", "teams.exe"), ("teams",),
             ("ms-teams.exe", "Teams.exe"), uri="msteams:"),
    AppEntry("spotify", "Spotify", ("spotify", "ספוטיפיי", "ספוטיפי"), ("spotify.exe",), ("spotify",), ("Spotify.exe", "spotify"),
             uri="spotify:"),
    AppEntry("vscode", "Visual Studio Code", ("vscode", "vs code", "visual studio code", "code", "וי אס קוד", "ויזואל סטודיו קוד",
                                               "ויז'ואל סטודיו קוד"), ("code.cmd", "Code.exe"), ("code",), ("Code.exe", "code")),
    AppEntry("cursor", "Cursor", ("cursor", "קרסר", "קורסור"), ("cursor.exe", "Cursor.exe"), ("cursor",), ("Cursor.exe", "cursor")),
    AppEntry("terminal", "Windows Terminal", ("terminal", "windows terminal", "טרמינל", "מסוף"), ("wt.exe", "cmd.exe"),
             ("gnome-terminal", "konsole", "xterm"), ("WindowsTerminal.exe", "wt.exe")),
    AppEntry("cmd", "Command Prompt", ("cmd", "command prompt", "שורת הפקודה", "שורת פקודה"), ("cmd.exe",), ("xterm",), ("cmd.exe",)),
    AppEntry("powershell", "PowerShell", ("powershell", "פאוורשל", "פאוור של"), ("pwsh.exe", "powershell.exe"), ("pwsh",),
             ("powershell.exe", "pwsh.exe")),
    AppEntry("taskmgr", "Task Manager", ("task manager", "מנהל המשימות", "מנהל משימות"), ("taskmgr.exe",), ("gnome-system-monitor",),
             ("Taskmgr.exe",)),
    AppEntry("paint", "Paint", ("paint", "mspaint", "צייר", "פיינט"), ("mspaint.exe",), ("pinta", "kolourpaint"), ("mspaint.exe",)),
    AppEntry("snipping", "Snipping Tool", ("snipping tool", "כלי החיתוך", "כלי חיתוך"), ("snippingtool.exe",), ("gnome-screenshot",),
             ("SnippingTool.exe",), uri="ms-screenclip:"),
    AppEntry("control", "Control Panel", ("control panel", "לוח הבקרה", "לוח בקרה"), ("control.exe",), (), ("control.exe",)),
    AppEntry("discord", "Discord", ("discord", "דיסקורד"), ("Discord.exe", "Update.exe"), ("discord",), ("Discord.exe",), uri="discord:"),
    AppEntry("whatsapp", "WhatsApp", ("whatsapp", "וואטסאפ", "ווטסאפ", "וואצאפ"), ("WhatsApp.exe",), (), ("WhatsApp.exe",),
             uri="whatsapp:"),
    AppEntry("telegram", "Telegram", ("telegram", "טלגרם"), ("Telegram.exe",), ("telegram-desktop",), ("Telegram.exe",), uri="tg:"),
    AppEntry("zoom", "Zoom", ("zoom", "זום"), ("Zoom.exe",), ("zoom",), ("Zoom.exe",), uri="zoommtg:"),
    AppEntry("steam", "Steam", ("steam", "סטים"), ("steam.exe",), ("steam",), ("steam.exe",), uri="steam:"),
    AppEntry("vlc", "VLC", ("vlc", "וי אל סי"), ("vlc.exe",), ("vlc",), ("vlc.exe",)),
    AppEntry("obs", "OBS Studio", ("obs", "obs studio", "או בי אס"), ("obs64.exe",), ("obs",), ("obs64.exe",)),
    AppEntry("photoshop", "Photoshop", ("photoshop", "פוטושופ"), ("Photoshop.exe",), (), ("Photoshop.exe",)),
    AppEntry("camera", "Camera", ("camera", "מצלמה"), (), ("cheese",), ("WindowsCamera.exe",), uri="microsoft.windows.camera:"),
    AppEntry("clock", "Clock", ("clock", "alarms", "שעון", "שעון מעורר", "טיימר"), (), (), ("Time.exe",), uri="ms-clock:"),
    AppEntry("mail", "Mail", ("mail", "דואר", "מייל"), (), ("thunderbird",), ("HxOutlook.exe", "olk.exe"), uri="mailto:"),
    AppEntry("calendar", "Calendar", ("calendar", "לוח שנה", "יומן"), (), (), (), uri="outlookcal:"),
    AppEntry("store", "Microsoft Store", ("microsoft store", "store", "חנות האפליקציות", "חנות מיקרוסופט"), (), (),
             ("WinStore.App.exe",), uri="ms-windows-store:"),
    AppEntry("photos", "Photos", ("photos", "תמונות", "אפליקציית התמונות"), (), ("eog",), ("Photos.exe",), uri="ms-photos:"),
)

SITES: tuple[SiteEntry, ...] = (
    SiteEntry("youtube", "YouTube", "https://www.youtube.com", ("youtube", "יוטיוב", "יו טיוב")),
    SiteEntry("google", "Google", "https://www.google.com", ("google", "גוגל")),
    SiteEntry("gmail", "Gmail", "https://mail.google.com", ("gmail", "ג'ימייל", "גימייל", "ג׳ימייל")),
    SiteEntry("maps", "Google Maps", "https://maps.google.com", ("google maps", "maps", "גוגל מפות", "מפות")),
    SiteEntry("translate", "Google Translate", "https://translate.google.com", ("google translate", "translate", "גוגל תרגום", "מתרגם")),
    SiteEntry("facebook", "Facebook", "https://www.facebook.com", ("facebook", "פייסבוק")),
    SiteEntry("instagram", "Instagram", "https://www.instagram.com", ("instagram", "אינסטגרם")),
    SiteEntry("whatsapp_web", "WhatsApp Web", "https://web.whatsapp.com", ("whatsapp web", "וואטסאפ ווב")),
    SiteEntry("github", "GitHub", "https://github.com", ("github", "גיטהאב", "גיט האב")),
    SiteEntry("netflix", "Netflix", "https://www.netflix.com", ("netflix", "נטפליקס")),
    SiteEntry("twitter", "X", "https://x.com", ("twitter", "x.com", "טוויטר")),
    SiteEntry("linkedin", "LinkedIn", "https://www.linkedin.com", ("linkedin", "לינקדאין", "לינקדין")),
    SiteEntry("wikipedia", "Wikipedia", "https://he.wikipedia.org", ("wikipedia", "ויקיפדיה")),
    SiteEntry("ynet", "ynet", "https://www.ynet.co.il", ("ynet", "ווינט", "וויינט")),
    SiteEntry("chatgpt", "ChatGPT", "https://chatgpt.com", ("chatgpt", "צ'אט ג'יפיטי", "צאט גיפיטי")),
    SiteEntry("claude", "Claude", "https://claude.ai", ("claude", "קלוד")),
)

_URL_RE = re.compile(r"^(?:https?://)?(?:www\.)?[a-z0-9\-]+(?:\.[a-z0-9\-]+)+(?:/\S*)?$", re.I)


def _strip_article(text: str) -> str:
    t = normalize(text).strip(" .,!?\"'")
    for prefix in ("את ה", "את ", "the ", "app ", "האפליקציה ", "התוכנה ", "תוכנת ", "אפליקציית "):
        if t.startswith(prefix):
            t = t[len(prefix):]
    if t.startswith("ה") and len(t) > 3:
        candidate = t[1:]
        if find_app(candidate, _strip=False) or find_site(candidate, _strip=False):
            return candidate
    return t.strip()


def _match(aliases: tuple[str, ...], text: str) -> bool:
    return text in {normalize(a) for a in aliases}


def find_app(name: str, _strip: bool = True) -> AppEntry | None:
    key = _strip_article(name) if _strip else normalize(name)
    for app in APPS:
        if _match(app.aliases, key) or key == app.id:
            return app
    return None


def find_site(name: str, _strip: bool = True) -> SiteEntry | None:
    key = _strip_article(name) if _strip else normalize(name)
    for site in SITES:
        if _match(site.aliases, key) or key == site.id:
            return site
    return None


def looks_like_url(text: str) -> bool:
    return bool(_URL_RE.match(text.strip()))


def to_url(text: str) -> str:
    t = text.strip()
    return t if t.lower().startswith(("http://", "https://")) else f"https://{t}"
