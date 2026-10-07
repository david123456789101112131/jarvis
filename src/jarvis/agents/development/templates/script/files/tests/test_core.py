from __future__ import annotations

import json

import core
import main
import pytest


@pytest.fixture
def folder(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.txt").write_text("hello world", encoding="utf-8")
    (tmp_path / "docs" / "b.txt").write_text("hello world", encoding="utf-8")
    (tmp_path / "big.bin").write_bytes(b"x" * 5000)
    (tmp_path / ".hidden").write_text("secret", encoding="utf-8")
    return tmp_path


def test_scan_skips_hidden(folder):
    names = sorted(e.path.name for e in core.scan(folder))
    assert names == ["a.txt", "b.txt", "big.bin"]
    assert len(core.scan(folder, include_hidden=True)) == 4


def test_summarize(folder):
    report = core.summarize(core.scan(folder), top=2)
    assert report["files"] == 3
    assert report["total_size"] == 5022
    assert report["by_extension"][0] == {"extension": ".txt", "files": 2, "size": 22}
    assert report["largest"][0]["path"].endswith("big.bin")


def test_duplicates(folder):
    groups = core.find_duplicates(core.scan(folder))
    assert len(groups) == 1
    assert sorted(p.name for p in groups[0]) == ["a.txt", "b.txt"]


def test_top_words_hebrew_and_english():
    text = "שלום שלום עולם. Hello hello world, the world!"
    words = dict(core.top_words(text, 5))
    assert words["שלום"] == 2 and words["hello"] == 2 and words["world"] == 2
    assert "the" not in words


def test_human_size():
    assert core.human_size(500) == "500 B"
    assert core.human_size(2048) == "2.0 KB"
    assert core.human_size(5 * 1024**3) == "5.0 GB"


def test_cli(folder, capsys):
    assert main.main(["stats", str(folder), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["files"] == 3
    assert main.main(["dupes", str(folder)]) == 0
    assert "a.txt" in capsys.readouterr().out
    assert main.main(["stats", str(folder / "missing")]) == 1
