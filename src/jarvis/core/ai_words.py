"""Which cloud AI the user named in a command: "תשתמש בגרוק ותכתוב לי שיר", "תשאל את גרוק מה...", "בגרוק:",
"use Grok to...", and "בלי גרוק" / "אל תשתמש בגרוק" for the opposite.

Only a clear request counts: a use/ask/with word before the name, "ב" + the name ("תענה בגרוק"), or the name
said first, as an address ("גרוק, ..."). A plain mention ("מה זה גרוק?", "תפתח את גרוק") is not a request.
"קלאוד" is also how Hebrew writes "cloud", so it names Claude only after "use" or "ask".
The phrase is taken out of the command, so the rest is understood on its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from jarvis.common.text import normalize

_NAMES = {
    "xai": r"grok|גרוק",
    "anthropic": r"claude|קלוד|קלואד",
    "openai": r"chat\s?gpt|gpt|openai|צ'?אט\s?ג'?י[-\s]?פי[-\s]?טי|צ'?אט\s?ג'?יפיטי|ג'?יפיטי|ג'?י[-\s]?פי[-\s]?טי",
}
_AFTER_USE = {**_NAMES, "anthropic": _NAMES["anthropic"] + r"|קלאוד"}

_USE = r"(?:\S*שתמש\S*|\S*שיתמש\S*|use|using)"
_SMALL = r"(?:\s+(?:רק|הפעם|עכשיו|מעכשיו|גם|תמיד|only|just|now|the))*"
_B = r"(?:ב[-־]?\s?)"                       # בגרוק, ב-Grok, ב־Grok, ב Grok
_START = r"(?<![\w'])"
_END = r"(?![\w'])"
# After the phrase: punctuation, and a joining word that belongs to it ("ותכתוב" -> "תכתוב", "כדי לכתוב").
_TAIL = r"(?:\s*[,:;.!\-–—])*(?:\s+(?:כדי\s+ש?|and\s+|to\s+|ו(?=[^\sו])))?"

_NEGATIVE = (
    _START + r"(?:אל|לא|תפסיק|תפסיקי|הפסק|תפסיקו|stop|don't|do not|never)\s+(?:{use}|לשאול|ask)\s+"
             r"(?:את\s+|" + _B + r")?(?:{after_use})",
    _START + r"(?:בלי|ללא|without)\s+(?:ה)?(?:{name})",
)
_POSITIVE = (
    _START + _USE + _SMALL + r"\s+" + _B + r"?(?:{after_use})",
    _START + r"(?:תשאל|שאל|לשאול|תשאלי|ask|let)\s+(?:את\s+)?(?:{after_use})",
    _START + r"(?:תבקש|בקש|לבקש|תבקשי)\s+(?:מ[-־]?\s?|from\s+)(?:{after_use})",
    _START + r"(?:עם|דרך|בעזרת|באמצעות|with|via|through|on|in)\s+(?:ה)?(?:{name})",
    _START + r"ו?" + _B + r"(?:{name})",
    r"^(?:{name})\s*[,:]",
)
# Left when the request was only the phrase: "מעכשיו תשתמש בגרוק", "use Grok please".
_FILLER = re.compile(r"^(?:(?:מעכשיו|עכשיו|מהיום|תמיד|בבקשה|הפעם|רק|אז|טוב|יאללה|please|now|from now on|always|"
                     r"just|ok|okay)\s*)*$", re.I)


def _compile(patterns: tuple[str, ...]) -> list[tuple[str, re.Pattern[str]]]:
    out = []
    for provider in _NAMES:
        for pattern in patterns:
            body = pattern.format(use=_USE, name=_NAMES[provider], after_use=_AFTER_USE[provider])
            out.append((provider, re.compile(body + _END + _TAIL, re.I)))
    return out


_NEG = _compile(_NEGATIVE)
_POS = _compile(_POSITIVE)


@dataclass(frozen=True)
class AIWord:
    provider: str | None = None    # the cloud AI the user named ("xai", "anthropic", "openai")
    negated: bool = False          # ...in order to say not to use it
    rest: str = ""                 # the command without the phrase ("" when the phrase was all of it)


def named_ai(text: str) -> AIWord:
    """The cloud AI a command asks for (or refuses), and the command without that phrase."""
    clean = normalize(text, lower=False)
    for found, negated in ((_first(_NEG, clean), True), (_first(_POS, clean), False)):
        if found is None:
            continue
        provider, m = found
        rest = " ".join(f"{clean[:m.start()]} {clean[m.end():]}".split()).strip(" ,.:;!-–—")
        return AIWord(provider, negated, "" if _FILLER.match(rest) else rest)
    return AIWord(rest=clean)


def _first(patterns: list[tuple[str, re.Pattern[str]]], text: str) -> tuple[str, re.Match[str]] | None:
    best: tuple[str, re.Match[str]] | None = None
    for provider, pattern in patterns:
        m = pattern.search(text)
        if m is not None and (best is None or m.start() < best[1].start()):
            best = (provider, m)
    return best


__all__ = ["AIWord", "named_ai"]
