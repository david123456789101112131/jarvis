"""A reply spoken while the model is still writing it: the "reply" field read out of JSON that is not finished yet
(`jarvis.core.early`), and the sentences handed to the voice pipeline as they complete (`SpeechStream`)."""

from __future__ import annotations

import asyncio
import itertools
import json

import pytest

from jarvis.core.early import EarlyReply, partial_json_string
from jarvis.voice.base import SpeechStream


class TestPartialJson:
    @pytest.mark.parametrize(("raw", "value", "closed"), [
        ('abc"', "abc", True),
        ("abc", "abc", False),
        ('a\\"b" , "x": 1', 'a"b', True),
        ("line\\nnext\\t\\\\", "line\nnext\t\\", False),
        ("cut \\", "cut ", False),                         # the escape is not complete yet
        ("\\u05e9\\u05dc", "של", False),
        ("\\u05e", "", False),
        ("\\ud83d\\ude00!", "😀!", False),                   # a surrogate pair
        ("\\ud83d", "", False),                             # its second half has not arrived
        ("\\uZZZZ", "", True),                              # not JSON: stop
    ])
    def test_a_string_as_far_as_it_is_written(self, raw: str, value: str, closed: bool) -> None:
        assert partial_json_string(raw, 0) == (value, closed)

    def test_matches_json_for_a_whole_string(self) -> None:
        text = 'שלום, "דוד"!\nמה\tשלומך? \\ 😀 /'
        encoded = json.dumps(text)
        assert partial_json_string(encoded, 1) == (text, True)
        assert partial_json_string(json.dumps(text, ensure_ascii=False), 1) == (text, True)


class TestEarlyReply:
    def test_the_reply_field_is_passed_on_as_it_grows(self) -> None:
        said: list[tuple[str, bool]] = []
        early = EarlyReply(lambda text, done: said.append((text, done)))
        answer = json.dumps({"run_task": False, "reply": "היי. מה נשמע?", "task": ""}, ensure_ascii=False)
        for end in range(1, len(answer) + 1):
            early(answer[:end])
        assert said[0] == ("ה", False) and said[-1] == ("היי. מה נשמע?", True)
        texts = [t for t, _ in said]
        assert all(b.startswith(a) for a, b in itertools.pairwise(texts))                # it only grows
        assert len(said) == len(set(said))                                               # each state once
        assert sum(1 for _, done in said if done) == 1

    def test_plain_text_is_the_reply(self) -> None:
        said: list[tuple[str, bool]] = []
        early = EarlyReply(lambda text, done: said.append((text, done)), structured=False)
        early("Hello")
        early("Hello there.")
        assert said == [("Hello", False), ("Hello there.", False)]

    def test_a_failing_listener_does_not_stop_the_answer(self) -> None:
        def broken(text: str, done: bool) -> None:
            raise RuntimeError("the speakers are gone")

        EarlyReply(broken)('{"reply": "x')          # no exception


class TestSpeechStream:
    async def test_each_sentence_once_it_is_complete(self) -> None:
        stream = SpeechStream(min_chars=12)
        stream.update("Hi. This is the first")
        waiting = asyncio.ensure_future(stream.next())
        await asyncio.sleep(0.01)
        assert not waiting.done()                       # "Hi." alone is too short; the next one is not finished
        stream.update("Hi. This is the first sentence. And")
        assert await waiting == "Hi. This is the first sentence."
        stream.update("Hi. This is the first sentence. And the last one", True)
        assert (await stream.next()).strip() == "And the last one"
        assert await stream.next() is None and stream.said.endswith("the last one")

    async def test_closing_ends_it_and_the_full_reply_decides_the_rest(self) -> None:
        stream = SpeechStream()
        stream.update("The answer is forty two. Then")
        assert await stream.next() == "The answer is forty two."
        stream.close()
        assert await stream.next() is None
        assert stream.rest_of("The answer is forty two. Then more.") == "Then more."
        assert stream.rest_of("The answer is forty") == ""                    # cut shorter than what was said
        assert stream.rest_of("The answer is forty…") == ""
        assert stream.rest_of("Something else entirely.") == "Something else entirely."
        assert SpeechStream().rest_of("  All of it. ") == "All of it."

    async def test_a_rewritten_reply_stops_it(self) -> None:
        stream = SpeechStream()
        stream.update("First attempt goes here. More")
        assert await stream.next() == "First attempt goes here."
        stream.update("Another attempt. ")
        assert stream.closed and await stream.next() is None

    async def test_a_long_reply_stops_at_the_limit(self) -> None:
        stream = SpeechStream(min_chars=5, max_chars=30)
        stream.update("One sentence here. Two sentence here. Three sentence here. Four", True)
        pieces = []
        while (piece := await stream.next()) is not None:
            pieces.append(piece.strip())
        assert pieces == ["One sentence here.", "Two sentence here."]
