"""What one computer-control action may do, judged before it runs (and checked again right before it happens).

The vision model only proposes "click at (x, y)" or "type this". JARVIS looks for itself at what the action
would reach: the window under the point or the one with the keyboard focus and, through UI Automation, the
button or field there. A page on the screen can make a model misread a button; it cannot change what
Windows reports, so the model's own description only ever adds caution.

* refused: JARVIS's own windows (its approvals and kill switch are for the user only), password fields,
  sign-in and security prompts, password managers, the Registry Editor, disk and firewall tools, typing a
  secret, Ctrl+Alt+Del;
* asked first: buttons that delete, send, publish, share, upload, reset or allow; Enter in a chat (it sends)
  or in a terminal (it runs the command); pasting into a terminal; personal data typed; Win+R;
* cannot be undone (asked every time, approved only by a click or by typing, never remembered): paying,
  buying, installing, permanent deletion, formatting, running a program file, discarding unsaved work, and
  the commands the shell rules mark so (`jarvis.permissions.risk.analyze_command`).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from jarvis.common.enums import RiskLevel
from jarvis.common.i18n import t
from jarvis.computer.backend import Target, combo_text
from jarvis.permissions.risk import _PII_HE, analyze_command, find_sensitive_data
from jarvis.security.redaction import default_redactor

R = RiskLevel
POINTER_ACTIONS = frozenset({"click", "double_click", "right_click", "drag"})
BASE_RISK = {"move": R.LOW, "scroll": R.LOW, "click": R.LOW, "double_click": R.LOW, "right_click": R.LOW,
             "drag": R.MEDIUM, "type": R.LOW, "press_keys": R.LOW}


@dataclass
class Verdict:
    risk: RiskLevel = RiskLevel.LOW
    reasons: list[str] = field(default_factory=list)
    refused: str | None = None        # why it may not happen at all
    irreversible: bool = False
    rule: str = ""                    # the rule that raised it (part of what a remembered approval covers)

    def raise_to(self, level: RiskLevel, reason: str = "", *, irreversible: bool = False, rule: str = "") -> None:
        if level > self.risk:
            self.risk = level
            if rule:
                self.rule = rule
        if reason and reason not in self.reasons:
            self.reasons.append(reason)
        if irreversible:
            self.irreversible = True

    def refuse(self, reason: str) -> None:
        if self.refused is None:
            self.refused = reason


# ----------------------------------------------------------------------------------------- windows
def _names(*names: str) -> frozenset[str]:
    return frozenset(n.lower() for n in names)


# Never touched: the user's own approvals, sign-in, security, passwords and the tools that can wreck a disk.
SENSITIVE_PROCESSES: dict[str, tuple[str, str]] = {
    **dict.fromkeys(_names("consent.exe"), ("the Windows administrator prompt", "חלון הרשאות המנהל של Windows")),
    **dict.fromkeys(_names("credentialuibroker.exe", "logonui.exe", "lockapp.exe"),
                    ("a Windows sign-in screen", "מסך כניסה של Windows")),
    **dict.fromkeys(_names("sechealthui.exe", "securityhealthsystray.exe", "securityhealthhost.exe"),
                    ("Windows Security", "אבטחת Windows")),
    **dict.fromkeys(_names("regedit.exe", "regedt32.exe"), ("the Registry Editor", "עורך הרישום")),
    **dict.fromkeys(_names("diskpart.exe", "bitlockerwizard.exe", "bitlockerwizardelev.exe"),
                    ("a disk or encryption tool", "כלי לניהול דיסקים או הצפנה")),
    **dict.fromkeys(_names("1password.exe", "bitwarden.exe", "keepass.exe", "keepassxc.exe", "dashlane.exe",
                           "lastpass.exe", "nordpass.exe", "keeperpasswordmanager.exe", "keeper password manager.exe",
                           "roboform.exe", "enpass.exe", "protonpass.exe", "proton pass.exe"),
                    ("a password manager", "מנהל סיסמאות")),
}
_SENSITIVE_TITLE = re.compile(
    r"password manager|credential manager|group policy|local security policy|windows security|bitlocker|firewall|"
    r"disk management|registry editor|user account control|מנהל\s+(?:ה)?(?:סיסמאות|אישורים)|מדיניות\s+קבוצתית|"
    r"מדיניות\s+אבטחה|אבטחת\s+windows|חומת\s+(?:ה)?אש|ניהול\s+(?:ה)?דיסקים|עורך\s+(?:ה)?רישום|בקרת\s+חשבונות",
    re.I)
_BROWSER_SENSITIVE_TITLE = re.compile(r"password manager|^\s*passwords?\b|מנהל\s+(?:ה)?סיסמאות", re.I)
BROWSERS = _names("chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "opera_gx.exe", "vivaldi.exe",
                  "iexplore.exe", "arc.exe", "browser.exe", "zen.exe", "waterfox.exe", "librewolf.exe")
TERMINALS = _names("cmd.exe", "powershell.exe", "pwsh.exe", "powershell_ise.exe", "windowsterminal.exe", "wt.exe",
                   "openconsole.exe", "conhost.exe", "bash.exe", "wsl.exe", "wslhost.exe", "mintty.exe", "git-bash.exe",
                   "alacritty.exe", "wezterm-gui.exe", "hyper.exe", "tabby.exe", "putty.exe", "kitty.exe",
                   "mobaxterm.exe", "termius.exe", "ubuntu.exe")
TERMINAL_CLASSES = frozenset({"consolewindowclass", "cascadia_hosting_window_class", "pseudoconsolewindow"})
CHATS = _names("whatsapp.exe", "whatsapp.root.exe", "telegram.exe", "ms-teams.exe", "teams.exe", "slack.exe",
               "discord.exe", "outlook.exe", "olk.exe", "hxoutlook.exe", "signal.exe", "skype.exe", "thunderbird.exe",
               "zoom.exe", "viber.exe", "messenger.exe", "element.exe", "wechat.exe", "weixin.exe", "line.exe",
               "mailspring.exe")
_CHAT_TITLE = re.compile(r"whatsapp|telegram|messenger|gmail|outlook|slack|discord|\bteams\b|signal|instagram|"
                         r"facebook|linkedin|\bmail\b|\bchat\b|דואר|הודעות|צ'?אט", re.I)
SETTINGS = _names("systemsettings.exe", "control.exe", "mmc.exe", "taskmgr.exe", "msconfig.exe", "rundll32.exe",
                  "systempropertiesadvanced.exe", "systempropertiesprotection.exe", "systempropertiescomputername.exe",
                  "systempropertiesremote.exe", "systempropertiesperformance.exe", "optionalfeatures.exe")
_RUN_TITLES = frozenset({"run", "הפעלה"})
_JARVIS_TITLE = re.compile(r"^\s*JARVIS(?:\s+Orb)?\s*$", re.I)


def is_jarvis_title(title: str) -> bool:
    return bool(_JARVIS_TITLE.match(title or ""))


def sensitive(target: Target) -> tuple[str, str] | None:
    """(English, Hebrew) name of a window JARVIS never touches, or None."""
    found = SENSITIVE_PROCESSES.get(target.process)
    if found is not None:
        return found
    title = target.title or ""
    if target.process in BROWSERS:
        return ("a password manager", "מנהל סיסמאות") if _BROWSER_SENSITIVE_TITLE.search(title) else None
    if _SENSITIVE_TITLE.search(title):
        return (f"“{title.strip()}”", f"“{title.strip()}”")
    return None


def is_terminal(target: Target) -> bool:
    return target.process in TERMINALS or target.class_name.lower() in TERMINAL_CLASSES


def is_run_box(target: Target) -> bool:
    """The Run dialog (Win+R): whatever is typed there runs as a command."""
    return target.class_name == "#32770" and target.process == "explorer.exe" and \
        target.title.strip().lower() in _RUN_TITLES


def runs_commands(target: Target) -> bool:
    """Enter here runs what was typed: terminals, the Run box, File Explorer's address bar."""
    explorer = target.process == "explorer.exe" and target.class_name.lower() == "cabinetwclass"
    return is_terminal(target) or is_run_box(target) or explorer


def is_chat(target: Target) -> bool:
    return target.process in CHATS or (target.process in BROWSERS and bool(_CHAT_TITLE.search(target.title or "")))


# ----------------------------------------------------------------------------------------- buttons
_HE_LETTER = "֐-׿"


def _en(*phrases: str) -> str:
    return r"\b(?:" + "|".join(phrases) + r")\b"


def _he(*words: str) -> str:
    # Hebrew glues "and/the/to/in/from/that" to the word: ומחק, למחיקה, שלח.
    return rf"(?<![{_HE_LETTER}])(?:[והבלמש]|וה|של)?(?:" + "|".join(words) + rf")(?![{_HE_LETTER}])"


# (rule, pattern, risk, English reason, Hebrew reason, irreversible) — matched against the name Windows reports for
# the element and the model's own description of it; the first matching rule in each group counts.
_BUTTON_RULES: list[tuple[str, re.Pattern[str], RiskLevel, str, str, bool]] = [
    (key, re.compile(p, re.I), level, en, he, irreversible) for key, p, level, en, he, irreversible in [
        ("pay", _en(r"pay(?:\s+now)?", r"buy(?:\s+now)?", "purchase", r"check\s*out", r"place\s+(?:your\s+)?order",
                    r"(?:confirm|complete|submit)\s+(?:your\s+)?(?:order|purchase|payment)",
                    r"transfer\s+(?:money|funds)", r"send\s+money", "donate", "subscribe")
         + "|" + _he("שלם", "תשלום", "קנה", "קנייה", "קניה", "רכישה", "רכוש", "לקופה", r"הזמן\s+עכשיו", r"בצע\s+הזמנה",
                     r"אשר\s+הזמנה", r"העבר\s+כסף", r"העברת\s+כסף", "תרום", "תרומה", r"הירשם\s+למנוי"),
         R.HIGH, "pays, buys or orders something", "משלם, קונה או מזמין משהו", True),
        ("install", _en("install", "uninstall", "reinstall", r"install\s+now", r"update\s+and\s+restart",
                        r"run\s+anyway", r"keep\s+anyway")
         + "|" + _he("התקן", "התקנה", r"הסר\s+התקנה", r"הסרת\s+התקנה", r"הפעל\s+בכל\s+זאת", r"שמור\s+בכל\s+זאת"),
         R.HIGH, "installs or removes software", "מתקין או מסיר תוכנה", True),
        ("wipe", _en(r"permanently\s+delete", r"delete\s+(?:permanently|forever)", "erase", "wipe",
                     r"empty\s+(?:the\s+)?(?:recycle\s+bin|trash|bin)", r"format\s+(?:the\s+)?(?:drive|disk|partition|"
                     r"volume|usb|device)", r"quick\s+format", r"factory\s+reset", r"reset\s+this\s+pc")
         + "|" + _he(r"מחק\s+לצמיתות", r"מחיקה\s+לצמיתות", r"מחק\s+סופית", r"רוקן\s+(?:את\s+)?סל\s+(?:ה)?מחזור",
                     "פרמט", "פרמוט", r"איפוס\s+להגדרות\s+(?:ה)?יצרן", r"אפס\s+את\s+(?:ה)?מחשב"),
         R.HIGH, "deletes for good", "מוחק לצמיתות", True),
        ("discard", _en(r"don'?t\s+save", r"do\s+not\s+save", r"discard(?:\s+changes)?")
         + "|" + _he(r"אל\s+תשמור", r"אל\s+תשמרי", r"בטל\s+(?:את\s+)?(?:ה)?שינויים", r"זרוק\s+(?:את\s+)?(?:ה)?שינויים"),
         R.HIGH, "throws away unsaved work", "זורק עבודה שלא נשמרה", True),
        ("delete", _en("delete", "remove", r"move\s+to\s+(?:the\s+)?(?:trash|bin|recycle\s+bin)")
         + "|" + _he("מחק", "מחיקה", "הסר", "הסרה", r"העבר\s+לסל"),
         R.HIGH, "deletes or removes something", "מוחק או מסיר משהו", False),
        ("send", _en("send", "submit", "post", "publish", "share", "upload", "reply", "tweet", "comment")
         + "|" + _he("שלח", "שליחה", "פרסם", "פרסום", "שתף", "שיתוף", "העלאה", r"העלה\s+קובץ", r"העלאת\s+קובץ",
                     "הגב"),
         R.HIGH, "sends, publishes or shares something", "שולח, מפרסם או משתף משהו", False),
        ("account", _en(r"sign\s*out", r"log\s*out", r"log\s*off", r"reset(?:\s+password)?", "restore", "disable",
                        r"turn\s+off", "allow", "grant", "approve", "confirm", "accept", r"(?:i\s+)?agree",
                        "unsubscribe")
         + "|" + _he("התנתק", "התנתקות", r"צא\s+מ(?:ה)?חשבון", "אפס", "איפוס", "שחזר", "שחזור", "השבת", "השבתה",
                     "כבה", "אפשר", "הרשה", r"אפשר\s+גישה", "אשר", "קבל", "מסכים", r"אני\s+מסכים", r"אני\s+מסכימה",
                     r"בטל\s+מנוי"),
         R.HIGH, "changes an account, a permission or a setting", "משנה חשבון, הרשאה או הגדרה", False),
    ]
]
_RUNNABLE = re.compile(r"\.(?:exe|msi|msix|msixbundle|appx|appxbundle|bat|cmd|ps1|vbs|vbe|js|jse|wsf|wsh|scr|com|"
                       r"hta|jar|reg|lnk|cpl|msc)\b", re.I)


def _button_rules(texts: Sequence[str], v: Verdict, lang: str) -> None:
    for key, pattern, level, en, he, irreversible in _BUTTON_RULES:
        if any(text and pattern.search(text) for text in texts):
            v.raise_to(level, t(lang, he, en), irreversible=irreversible, rule=key)
            if irreversible:
                return       # the strongest reason says enough


# ----------------------------------------------------------------------------------------- keys
# key set → (rule, risk, English, Hebrew, irreversible)
_KEY_RULES: dict[frozenset[str], tuple[str, RiskLevel, str, str, bool]] = {
    frozenset({"win", "r"}): ("run_box", R.HIGH, "opens the Run box, which can start any program",
                              "פותח את חלון ההפעלה, שממנו אפשר להריץ כל תוכנה", False),
    frozenset({"shift", "delete"}): ("wipe", R.HIGH, "deletes without the Recycle Bin", "מוחק בלי סל המחזור", True),
    frozenset({"win", "x"}): ("menu", R.MEDIUM, "opens the Windows power-user menu", "פותח את התפריט המתקדם של Windows",
                              False),
    frozenset({"ctrl", "shift", "esc"}): ("taskmgr", R.MEDIUM, "opens Task Manager", "פותח את מנהל המשימות", False),
    frozenset({"alt", "f4"}): ("close", R.MEDIUM, "closes the window (unsaved work may be lost)",
                               "סוגר את החלון (עבודה שלא נשמרה עלולה ללכת לאיבוד)", False),
    frozenset({"win", "l"}): ("lock", R.MEDIUM, "locks the computer", "נועל את המחשב", False),
    frozenset({"delete"}): ("delete", R.MEDIUM, "deletes what is selected", "מוחק את מה שמסומן", False),
    frozenset({"ctrl", "d"}): ("delete", R.MEDIUM, "may delete what is selected", "עלול למחוק את מה שמסומן", False),
}
_ENTER = (frozenset({"enter"}), frozenset({"ctrl", "enter"}), frozenset({"alt", "enter"}))
_PASTE = (frozenset({"ctrl", "v"}), frozenset({"shift", "insert"}), frozenset({"ctrl", "shift", "v"}))


def _command(v: Verdict, command: str, lang: str) -> None:
    """Enter on a command line: judged like `run_command` judges the same text."""
    command = command.strip()
    if not command:
        v.raise_to(R.HIGH, t(lang, "מריץ את מה שכתוב בשורת הפקודה", "runs whatever is on the command line"),
                   rule="command")
        return
    a = analyze_command(command, lang)
    if a.read_only:
        v.raise_to(R.MEDIUM, t(lang, "מריץ פקודה שרק מציגה מידע", "runs a command that only shows information"),
                   rule="command")
        return
    v.raise_to(R.HIGH, t(lang, f"מריץ פקודה: {command[:120]}", f"runs a command: {command[:120]}"), rule="command")
    for reason in a.reasons:
        v.raise_to(a.risk, reason, irreversible=a.irreversible)


# ----------------------------------------------------------------------------------------- the judgement
def assess(action: str, target: Target, *, lang: str = "en", element: str = "", text: str = "",
           press_enter: bool = False, combos: Sequence[Sequence[str]] = (), pending_command: str = "") -> Verdict:
    """How risky `action` on `target` is. `pending_command`: what was already typed on this command line."""
    v = Verdict(BASE_RISK.get(action, R.MEDIUM))
    if target.own:
        v.refuse(t(lang, "זה חלון של JARVIS עצמו: האישורים ומתג החירום שבו שמורים לך בלבד",
                   "this is JARVIS's own window: its approvals and kill switch are for you only"))
    found = sensitive(target)
    if found is not None:
        v.refuse(t(lang, f"זה {found[1]}, ו-JARVIS לא נוגע בו", f"this is {found[0]}, which JARVIS does not touch"))
    commands = runs_commands(target)

    if action in POINTER_ACTIONS:
        _button_rules([target.element, element], v, lang)
        if action == "double_click" and any(_RUNNABLE.search(s or "") for s in (target.element, element)):
            v.raise_to(R.HIGH, t(lang, "מפעיל קובץ תוכנה (התקנות וסקריפטים יכולים לשנות את המחשב)",
                                 "runs a program file (installers and scripts can change the computer)"),
                       irreversible=True, rule="program")
        if action == "right_click" and is_terminal(target):
            v.raise_to(R.HIGH, t(lang, "לחיצה ימנית בטרמינל מדביקה את הלוח, ושורה מודבקת יכולה לרוץ",
                                 "a right-click in a terminal pastes the clipboard, and a pasted line can run"),
                       rule="paste")
        if action == "drag":
            v.raise_to(R.MEDIUM, t(lang, "גרירה יכולה להעביר קבצים", "dragging can move files"))
        if target.process in SETTINGS and action != "drag":
            v.raise_to(R.MEDIUM, t(lang, "משנה הגדרות של Windows", "changes Windows settings"))

    elif action == "type":
        if target.password:
            v.refuse(t(lang, "זה שדה סיסמה: את הסיסמה צריך להקליד בעצמך", "this is a password field: type the "
                                                                       "password yourself"))
        if default_redactor().find_secrets(text):
            v.refuse(t(lang, "הטקסט כולל סוד (מפתח API, טוקן או סיסמה)",
                       "the text contains a secret (an API key, a token or a password)"))
        lines = text.replace("\r", "").split("\n")
        ran = lines[:-1] + ([lines[-1]] if press_enter else [])     # each line ended by an Enter runs
        if commands:
            for i, line in enumerate(ran):
                _command(v, (pending_command if i == 0 else "") + line, lang)
            if not ran:
                v.raise_to(R.MEDIUM, t(lang, "מקליד בשורת פקודה (היא תרוץ רק ב-Enter)",
                                       "types on a command line (it runs only on Enter)"))
        elif ran:
            if is_chat(target):
                v.raise_to(R.HIGH, t(lang, "שולח הודעה (Enter שולח בצ'אט)", "sends a message (Enter sends in a chat)"),
                           rule="send")
            else:
                v.raise_to(R.MEDIUM, t(lang, "לוחץ Enter (עשוי לשלוח טופס)", "presses Enter (it may submit a form)"))
        personal = sorted(set(find_sensitive_data(text)))
        if personal:
            v.raise_to(R.HIGH, t(lang, "מקליד מידע אישי: " + ", ".join(_PII_HE.get(p, p) for p in personal),
                                 "types personal data: " + ", ".join(personal)), rule="personal")

    elif action == "press_keys":
        for combo in combos:
            keys = frozenset(combo)
            if {"ctrl", "alt", "delete"} <= keys:
                v.refuse(t(lang, "Ctrl+Alt+Del שמור לאדם שליד המחשב", "Ctrl+Alt+Del is only for the person at the "
                                                                     "computer"))
                continue
            rule = _KEY_RULES.get(keys)
            if rule is not None:
                key, level, en, he, irreversible = rule
                v.raise_to(level, t(lang, f"{combo_text(combo)}: {he}", f"{combo_text(combo)}: {en}"),
                           irreversible=irreversible, rule=key)
            if keys in _ENTER:
                if commands:
                    _command(v, pending_command, lang)
                    pending_command = ""
                elif is_chat(target):
                    v.raise_to(R.HIGH, t(lang, "שולח את ההודעה", "sends the message"), rule="send")
            if keys in _PASTE and is_terminal(target):
                v.raise_to(R.HIGH, t(lang, "מדביק לטרמינל, ושורה מודבקת יכולה לרוץ",
                                     "pastes into a terminal, and a pasted line can run"), rule="paste")
    return v


def pending_after(pending: str, text: str, press_enter: bool) -> str:
    """What is left typed on a command line (and not run yet) after typing `text` there."""
    lines = text.replace("\r", "").split("\n")
    if press_enter:
        return ""
    return lines[-1] if len(lines) > 1 else pending + text


def window_label(target: Target, lang: str = "en") -> str:
    """How a person names the window an action reaches."""
    cls = target.class_name
    if cls in ("Progman", "WorkerW"):
        return t(lang, "שולחן העבודה", "the desktop")
    if cls in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
        return t(lang, "שורת המשימות", "the taskbar")
    return (target.title or "").strip()[:80] or target.process or t(lang, "המסך", "the screen")


__all__ = ["BASE_RISK", "POINTER_ACTIONS", "Verdict", "assess", "is_chat", "is_jarvis_title", "is_run_box",
           "is_terminal", "pending_after", "runs_commands", "sensitive", "window_label"]
