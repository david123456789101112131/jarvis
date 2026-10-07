"""Offline extractive summarization (Hebrew and English): no model needed.

Sentences are scored by how many query terms and frequent content words they contain, with a
small bonus for early sentences; the best ones are returned in their original order.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

_SENT_SPLIT = re.compile(r"(?<=[.!?。])\s+|\n{2,}|(?<=[.!?])(?=[\"'”’])\s*")
_WORD = re.compile(r"[\w֐-׿']+", re.UNICODE)
_HE_PREFIXES = ("וה", "שה", "וב", "וכ", "ול", "ומ", "ה", "ו", "ב", "ל", "מ", "ש", "כ")

STOPWORDS = {
    # English
    "the", "a", "an", "and", "or", "but", "if", "of", "to", "in", "on", "for", "with", "at", "by", "from", "as", "is",
    "are", "was", "were", "be", "been", "being", "it", "its", "this", "that", "these", "those", "you", "your", "we",
    "our", "they", "their", "he", "she", "his", "her", "i", "me", "my", "not", "no", "can", "will", "would", "should",
    "could", "has", "have", "had", "do", "does", "did", "so", "than", "then", "there", "here", "what", "which", "who",
    "when", "where", "how", "why", "all", "any", "more", "most", "other", "some", "such", "only", "also", "into",
    "about", "over", "after", "before", "up", "out", "just", "very", "one", "two", "may", "new", "use", "used",
    # Hebrew
    "של", "את", "על", "עם", "זה", "זו", "זאת", "הוא", "היא", "הם", "הן", "אני", "אתה", "אנחנו", "אתם", "לא",
    "כן", "גם", "או", "אם", "כי", "אבל", "רק", "כל", "יש", "אין", "היה", "הייתה", "היו", "להיות", "יותר", "פחות",
    "מאוד", "כמו", "אחר", "אחרי", "לפני", "בין", "תוך", "כדי", "עד", "אשר", "שלא", "מה", "מי", "איך", "למה", "איפה",
    "אותו", "אותה", "אותם", "שלו", "שלה", "שלהם", "שלי", "שלנו", "כך", "כמה", "עוד", "ידי", "לכן", "אך", "בו", "בה",
}


@dataclass
class Sentence:
    text: str
    index: int
    score: float = 0.0
    source: int = 0


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"[ \t]+", " ", text or "").strip()
    parts = []
    for block in re.split(r"\n+", text):
        block = block.strip(" -•*#\t")
        if not block:
            continue
        parts.extend(s.strip() for s in _SENT_SPLIT.split(block) if s and s.strip())
    return [p for p in parts if 25 <= len(p) <= 600]


def stem(word: str) -> str:
    w = word.lower().strip("'")
    if re.match(r"^[֐-׿]", w):
        for prefix in _HE_PREFIXES:
            if w.startswith(prefix) and len(w) - len(prefix) >= 3:
                return w[len(prefix):]
        return w
    for suffix in ("ing", "edly", "ed", "es", "s", "ly"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            return w[: -len(suffix)]
    return w


def terms(text: str) -> list[str]:
    return [stem(w) for w in _WORD.findall(text or "") if w.lower() not in STOPWORDS and len(w) > 1]


def summarize(texts: list[str] | str, query: str = "", *, max_sentences: int = 6, max_chars: int = 1400) -> list[Sentence]:
    """Pick the most informative sentences across one or more documents."""
    docs = [texts] if isinstance(texts, str) else list(texts)
    sentences: list[Sentence] = []
    for d, doc in enumerate(docs):
        for i, s in enumerate(split_sentences(doc)):
            sentences.append(Sentence(text=s, index=i, source=d))
    if not sentences:
        return []
    freq = Counter(term for s in sentences for term in set(terms(s.text)))
    query_terms = set(terms(query))
    n_docs = len(sentences)
    seen: set[str] = set()
    for s in sentences:
        words = terms(s.text)
        if not words:
            continue
        unique = set(words)
        content = sum(math.log(1 + freq[w]) for w in unique) / (len(unique) ** 0.5)
        hits = len(unique & query_terms)
        position = 1.0 / (1 + s.index * 0.15)
        s.score = content + 2.5 * hits + 0.8 * position
        if query_terms and hits == 0:
            s.score *= 0.55
        if n_docs > 1 and len(words) < 5:
            s.score *= 0.6
    picked: list[Sentence] = []
    total = 0
    for s in sorted(sentences, key=lambda x: x.score, reverse=True):
        key = " ".join(sorted(set(terms(s.text))))[:120]
        if key in seen:
            continue
        if total + len(s.text) > max_chars and picked:
            continue
        seen.add(key)
        picked.append(s)
        total += len(s.text)
        if len(picked) >= max_sentences:
            break
    return sorted(picked, key=lambda x: (x.source, x.index))


def summary_text(texts: list[str] | str, query: str = "", **kw: int) -> str:
    return " ".join(s.text for s in summarize(texts, query, **kw))
