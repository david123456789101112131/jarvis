"""The development mode's own logic, without an AI or a running JARVIS: looking at a project, finding the files
a request is about, turning proposed edits into a reviewable change (and its checks), what is sent and what it
costs, hiding secrets in code, and reading test output."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from jarvis.devmode import inspect as dev_inspect
from jarvis.devmode.edits import (
    Change,
    ProposedEdit,
    Replacement,
    apply_replacements,
    build_file,
    check_imports,
    safe_relative,
)
from jarvis.devmode.prompt import (
    Engine,
    actual_cost,
    build_messages,
    choose_effort,
    cost_text,
    estimate_cost,
)
from jarvis.devmode.relevance import rank_files, terms, traced_files
from jarvis.devmode.service import parse_failures
from jarvis.security.redaction import Redactor

SECRET = "xai-" + "Q" * 40


def write(root: Path, rel: str, text: str | bytes) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_bytes(text.encode("utf-8"))
    return path


def python_project(root: Path) -> Path:
    write(root, "pyproject.toml", '[project]\nname = "shop"\ndependencies = ["fastapi>=0.110", "pydantic"]\n'
                                  '[project.optional-dependencies]\ndev = ["pytest"]\n'
                                  "[tool.ruff]\nline-length = 110\n")
    write(root, "main.py", "from shop.cart import Cart\n\n\ndef main():\n    print(Cart().total())\n")
    write(root, "shop/__init__.py", "")
    write(root, "shop/cart.py", "class Cart:\n    def __init__(self):\n        self.items = []\n\n"
                                "    def total(self):\n        if self.items:\n            return sum(self.items)\n"
                                "        return 0\n")
    write(root, "tests/test_cart.py", "from shop.cart import Cart\n\n\ndef test_empty():\n    assert Cart().total() == 0\n")
    write(root, "README.md", "# Shop\nA small shop.\n")
    write(root, ".env", f"XAI_API_KEY={SECRET}\n")
    write(root, "node_modules/left-pad/index.js", "module.exports = 1\n")
    write(root, "secrets.json", json.dumps({"password": "hunter2hunter2"}))
    return root


# ============================================================================ looking at a project
class TestInspect:
    def test_a_python_project(self, tmp_path: Path) -> None:
        root = python_project(tmp_path / "shop")
        report = dev_inspect.inspect_project(root)
        assert report["name"] == "shop" and report["languages"][0]["name"] == "Python"
        assert {"FastAPI", "Pydantic", "pytest"} <= set(report["frameworks"])
        assert report["python"]["dependencies"] == ["fastapi", "pydantic"]
        assert report["python"]["dev_dependencies"] == ["pytest"] and report["python"]["line_length"] == 110
        assert report["commands"]["test"] == "pytest" and "python main.py" in report["commands"]["run"]
        assert report["style"]["indent"] == "4 spaces" and report["style"]["line_endings"] == "LF"
        assert report["tests"] == {"dirs": ["tests"], "files": 1, "examples": ["tests/test_cart.py"]}
        assert set(report["secret_files"]) == {".env", "secrets.json"}
        assert "shop" in report["local_modules"] and "main" in report["local_modules"]
        assert report["readme"]["text"].startswith("# Shop")
        assert {m["key"] for m in report["missing"]} & {"no_repo", "no_git"}
        assert not any(f.startswith("node_modules") for f in (x["name"] for x in report["layout"]))
        dumped = json.dumps(report)
        assert SECRET not in dumped and "hunter2" not in dumped       # secret files are listed, never read

    def test_a_node_project(self, tmp_path: Path) -> None:
        root = tmp_path / "web"
        write(root, "package.json", json.dumps({
            "name": "web", "type": "module",
            "scripts": {"dev": "vite", "build": "vite build", "test": "vitest run", "lint": "eslint ."},
            "dependencies": {"react": "^19.0.0"}, "devDependencies": {"vite": "^7", "vitest": "^3"}}))
        body = "".join(f"  const v{i} = 'value {i}'\n" for i in range(30))
        write(root, "src/App.tsx", "import React from 'react'\n\nexport function App() {\n" + body + "  return null\n}\n")
        report = dev_inspect.inspect_project(root)
        assert {"React", "Vite", "Vitest"} <= set(report["frameworks"])
        assert report["node"]["module"] is True and report["node"]["manager"] == "npm"
        assert report["commands"]["run"] == ["npm run dev"] and report["commands"]["build"] == ["npm run build"]
        assert report["commands"]["check"] == ["npm run lint"] and report["commands"]["test"] == "npm test"
        assert report["style"]["indent"] == "2 spaces" and report["style"]["js_quotes"] == "single"
        assert report["style"]["js_semicolons"] is False
        assert "no_node_modules" in {m["key"] for m in report["missing"]}

    def test_the_scan_is_reused_until_the_project_changes(self, tmp_path: Path) -> None:
        root = python_project(tmp_path / "shop")
        dev_inspect.clear_cache()
        first = dev_inspect.scan_cached(root)
        assert dev_inspect.scan_cached(root) is first
        assert dev_inspect.scan_cached(root, refresh=True) is not first
        again = dev_inspect.scan_cached(root)
        write(root, "new_module.py", "x = 1\n")
        changed = dev_inspect.scan_cached(root)
        assert changed is not again and any(f.path == "new_module.py" for f in changed.files)
        assert dev_inspect.clear_cache() == 1 and dev_inspect.clear_cache() == 0

    def test_secret_file_names(self) -> None:
        for name in (".env", ".env.local", "id_rsa", "server.pem", "credentials.json", "app.db", ".npmrc"):
            assert dev_inspect.is_secret_name(name), name
        for name in (".env.example", "config.py", "keyboard.ts", "README.md", "tokens.css"):
            assert not dev_inspect.is_secret_name(name), name


# ============================================================================ the relevant files
class TestRelevance:
    def test_hebrew_words_find_english_code_names(self) -> None:
        found = terms("תוסיף כפתור התחברות בדף הבית")
        assert {"button", "login", "page", "home"} <= found
        assert "תוסיף" not in found and "add" not in terms("add a LoginButton") - {"login", "button", "loginbutton"}
        assert {"login", "button"} <= terms("fix LoginButton")

    def test_the_request_ranks_the_files_with_reasons(self, tmp_path: Path) -> None:
        root = tmp_path / "web"
        write(root, "src/LoginButton.tsx", "export function LoginButton() { return <button>Sign in</button> }\n")
        write(root, "src/Footer.tsx", "export function Footer() { return <footer/> }\n")
        write(root, "src/api.ts", "export const get = () => fetch('/x')\n")
        files = dev_inspect.scan(root).files
        ranked = rank_files(root, files, "תוסיף אייקון לכפתור ההתחברות", lang="en")
        assert ranked[0].path == "src/LoginButton.tsx" and "the name matches" in ranked[0].reasons[0]
        assert "src/Footer.tsx" not in [r.path for r in ranked]

    def test_a_traceback_points_at_its_files_and_lines(self, tmp_path: Path) -> None:
        root = python_project(tmp_path / "shop")
        error = (f'Traceback (most recent call last):\n  File "{root / "shop" / "cart.py"}", line 7, in total\n'
                 "TypeError: unsupported operand\n")
        files = dev_inspect.scan(root).files
        assert traced_files(error, root, {f.path for f in files}) == {"shop/cart.py": [7]}
        ranked = rank_files(root, files, "", error_text=error, lang="he")
        assert ranked[0].path == "shop/cart.py" and ranked[0].lines == [7] and "מופיע בשגיאה" in ranked[0].reasons
        assert "tests/test_cart.py" in [r.path for r in ranked]        # its tests come along

    def test_nothing_matches_falls_back_to_the_entry_point(self, tmp_path: Path) -> None:
        root = python_project(tmp_path / "shop")
        old = time.time() - 30 * 86400
        for f in root.rglob("*"):
            os.utime(f, (old, old))
        ranked = rank_files(root, dev_inspect.scan(root).files, "zzz qqq", lang="en")
        assert ranked and ranked[0].path == "main.py" and "entry point" in ranked[0].reasons[0]
        assert all(not r.path.startswith((".env", "secrets")) for r in ranked)


# ============================================================================ a proposed change
class TestEdits:
    def test_replacements_apply_exactly_once(self) -> None:
        text = "a = 1\nb = 2\nb = 2\n"
        new, problems = apply_replacements(text, [Replacement(find="a = 1\n", replace="a = 10\n")], "en")
        assert new == "a = 10\nb = 2\nb = 2\n" and problems == []
        _, problems = apply_replacements(text, [Replacement(find="b = 2\n", replace="b = 3\n")], "en")
        assert [p.code for p in problems] == ["ambiguous"]
        _, problems = apply_replacements(text, [Replacement(find="c = 3", replace="c = 4")], "en")
        assert [p.code for p in problems] == ["not_found"]
        _, problems = apply_replacements(text, [Replacement(find='k = "[REDACTED]"', replace="k = 1")], "en")
        assert [p.code for p in problems] == ["masked"]

    def test_trailing_spaces_do_not_spoil_a_match_or_change_other_lines(self) -> None:
        text = "keep = 1   \ndef f():   \n    return 1\nother = 2  \n"
        new, problems = apply_replacements(
            text, [Replacement(find="def f():\n    return 1\n", replace="def f():\n    return 2\n")], "en")
        assert problems == [] and new == "keep = 1   \ndef f():\n    return 2\nother = 2  \n"

    def test_a_crlf_file_with_a_bom_keeps_its_form_and_its_backup(self, tmp_path: Path) -> None:
        raw = b"\xef\xbb\xbfname = 'x'\r\nvalue = 1\r\n"
        write(tmp_path, "app.py", raw)
        edit = ProposedEdit(path="app.py", action="modify", why="w",
                            replacements=[Replacement(find="value = 1\n", replace="value = 2\nmore = 3\n")])
        fc = build_file(tmp_path, edit, "en", {})
        assert fc.problems == [] and (fc.added, fc.removed) == (2, 1)
        assert fc.encoded() == b"\xef\xbb\xbfname = 'x'\r\nvalue = 2\r\nmore = 3\r\n"
        assert fc.original() == raw and "+value = 2" in fc.diff

    def test_problems_that_block_a_file(self, tmp_path: Path) -> None:
        write(tmp_path, "data.json", '{"a": 1}\n')
        write(tmp_path, "mod.py", "x = 1\n")
        bad_json = build_file(tmp_path, ProposedEdit(path="data.json", action="modify", replacements=[
            Replacement(find='{"a": 1}', replace='{"a": 1,}')]), "en", {})
        bad_py = build_file(tmp_path, ProposedEdit(path="mod.py", action="modify", replacements=[
            Replacement(find="x = 1", replace="x = (1")]), "en", {})
        outside = build_file(tmp_path, ProposedEdit(path="../evil.py", action="create", content="x"), "en", {})
        missing = build_file(tmp_path, ProposedEdit(path="nope.py", action="modify", replacements=[
            Replacement(find="a", replace="b")]), "en", {})
        assert [p.code for p in bad_json.problems] == ["syntax"] and bad_json.blocked
        assert [p.code for p in bad_py.problems] == ["syntax"]
        assert [p.code for p in outside.problems] == ["path"] and [p.code for p in missing.problems] == ["missing"]
        exists = build_file(tmp_path, ProposedEdit(path="mod.py", action="create", content="y = 2\n"), "en", {})
        assert exists.action == "modify" and [w.code for w in exists.warnings] == ["exists"] and not exists.blocked

    def test_new_files_follow_the_projects_line_endings(self, tmp_path: Path) -> None:
        fc = build_file(tmp_path, ProposedEdit(path="src/new.py", action="create", content="a = 1\nb = 2\n"),
                        "en", {"style": {"line_endings": "CRLF"}})
        assert fc.old is None and fc.encoded() == b"a = 1\r\nb = 2\r\n" and "/dev/null" in fc.diff

    def test_a_text_the_files_encoding_cannot_hold_is_blocked(self, tmp_path: Path) -> None:
        write(tmp_path, "old.py", "# שלום\nx = 1\n".encode("cp1255"))             # Hebrew ANSI, as old Windows saved it
        fc = build_file(tmp_path, ProposedEdit(path="old.py", action="modify", replacements=[
            Replacement(find="x = 1\n", replace="x = '🙂'\n")]), "en", {})
        assert fc.encoding == "cp1255" and [p.code for p in fc.problems] == ["encoding"]

    def test_imports_the_project_does_not_declare_are_warned_about(self) -> None:
        facts = {"python": {"dependencies": ["pydantic", "pyyaml"], "dev_dependencies": []},
                 "node": {"dependencies": ["react"], "dev_dependencies": ["vitest"]}, "local_modules": ["shop"]}
        new = "import json\nimport yaml\nimport requests\nfrom shop import cart\nfrom pydantic import BaseModel\n"
        warned = check_imports("a.py", "", new, facts, "en")
        assert len(warned) == 1 and "requests" in warned[0].text and "yaml" not in warned[0].text
        js = "import React from 'react'\nimport axios from 'axios'\nimport fs from 'node:fs'\nimport x from './x'\n"
        warned = check_imports("a.tsx", "", js, facts, "en")
        assert len(warned) == 1 and "axios" in warned[0].text
        assert check_imports("a.py", "import requests\n", "import requests\nx = 1\n", facts, "en") == []

    def test_a_broad_rewrite_is_pointed_out(self, tmp_path: Path) -> None:
        write(tmp_path, "big.py", "".join(f"v{i} = {i}\n" for i in range(40)))
        whole = "".join(f"v{i} = {i}\n" for i in range(40))
        fc = build_file(tmp_path, ProposedEdit(path="big.py", action="modify", replacements=[
            Replacement(find=whole, replace="".join(f"w{i} = {i}\n" for i in range(40)))]), "en", {})
        assert [w.code for w in fc.warnings] == ["broad"] and not fc.blocked

    def test_the_digest_binds_an_approval_to_the_exact_text(self, tmp_path: Path) -> None:
        write(tmp_path, "a.py", "x = 1\n")
        fc = build_file(tmp_path, ProposedEdit(path="a.py", action="modify", replacements=[
            Replacement(find="x = 1", replace="x = 2")]), "en", {})
        change = Change(session_id="dev_1", root=str(tmp_path), request="r", files=[fc])
        before = change.digest()
        fc.new = "x = 3\n"
        assert change.digest() != before
        fc.include = False
        assert change.to_apply() == []
        fc.include, fc.status, fc.applied_sha = True, "applied", "abc"
        assert change.to_undo() == [fc] and change.digest(undo=True) != change.digest()

    def test_paths_stay_inside_the_project(self) -> None:
        assert safe_relative("./src\\app.py") == "src/app.py" and safe_relative("a//b") == "a/b"
        for bad in ("../x", "/etc/passwd", "C:/Windows/x", "a/../../b", "a:b", "", "x/.."):
            assert safe_relative(bad) is None, bad


# ============================================================================ what is sent, and its price
GROK = Engine(provider="xai", name="Grok", model="grok-4.7", effort="low", max_tokens=12_000, local=False)


class TestPrompt:
    def test_quick_or_deep_thinking(self) -> None:
        assert choose_effort("auto", "rename the variable", 1, 2_000, False) == ("low", "small")
        assert choose_effort("auto", "rename the variable", 5, 2_000, False) == ("high", "large")
        assert choose_effort("auto", "לשכתב את כל הפרויקט", 1, 2_000, False) == ("high", "complex")
        assert choose_effort("fast", "refactor everything", 9, 99_000, True)[0] == "low"
        assert choose_effort("strong", "x", 1, 10, False)[0] == "high"

    def test_the_price_estimate_and_the_real_cost(self) -> None:
        low, high = estimate_cost(GROK, 32_000) or (0, 0)
        assert 0 < low < high < 0.2
        assert estimate_cost(Engine("local", "Local", "qwen", "low", 4000, True), 32_000) is None
        assert cost_text(None, "en", local=True) == "free (local model)"
        assert actual_cost(GROK, {"input_tokens": 1_000_000, "output_tokens": 100_000}) == 2.6

    def test_the_message_carries_the_request_facts_and_files(self, tmp_path: Path) -> None:
        report = dev_inspect.inspect_project(python_project(tmp_path / "shop"))
        system, user = build_messages("make the total skip negative numbers", report, ["main.py", "shop/cart.py"],
                                      [("shop/cart.py", "class Cart: ...\n", True)], error_text="boom", lang="he")
        assert "Write understood" in system and "in Hebrew" in system
        assert user.startswith("REQUEST:\nmake the total") and "ERROR OR TEST OUTPUT" in user
        assert "=== shop/cart.py (2 lines) — CUT" in user and "FILE LIST (2 files)" in user
        assert str(tmp_path) not in user and "A small shop" not in user   # no local paths, no README


# ============================================================================ secrets in code
class TestMaskCode:
    def test_secret_values_are_hidden_and_the_code_stays_readable(self) -> None:
        redactor = Redactor()
        redactor.register("XAI_API_KEY", "registered-value-123")
        code = (f'API_KEY = "abcd1234efgh5678"\nclient = Grok("{SECRET}")\ntoken = get_token()\n'
                "headers = {'password': 'hunter2hunter2'}\nmax_tokens = \"1234567\"\n"
                'db = "postgres://admin:s3cretpass@host/db"\nother = "registered-value-123"\n')
        masked, hidden = redactor.mask_code(code)
        assert hidden == 5
        assert 'API_KEY = "[REDACTED]"' in masked and "token = get_token()" in masked
        assert SECRET not in masked and "hunter2" not in masked and "s3cretpass" not in masked
        assert "postgres://admin:[REDACTED]@host/db" in masked and 'max_tokens = "1234567"' in masked
        assert "[REDACTED:XAI_API_KEY]" in masked
        assert redactor.mask_code("") == ("", 0)


# ============================================================================ test output
class TestFailures:
    def test_pytest_vitest_jest_and_node(self) -> None:
        pytest_out = ("FAILED tests/test_cart.py::test_total - AssertionError: assert 3 == 4\n"
                      "ERROR tests/test_db.py::test_conn\n1 failed, 1 error, 3 passed in 0.12s\n")
        assert parse_failures(pytest_out) == [
            {"name": "tests/test_cart.py::test_total", "message": "AssertionError: assert 3 == 4"},
            {"name": "tests/test_db.py::test_conn", "message": "error"}]
        vitest = " ✓ src/a.test.ts > works 2ms\n × src/sum.test.ts > adds numbers 3ms\n Test Files  1 failed (2)\n"
        assert parse_failures(vitest) == [{"name": "src/sum.test.ts > adds numbers", "message": ""}]
        assert parse_failures("  ● Cart › totals\n")[0]["name"] == "Cart › totals"
        assert parse_failures("not ok 2 - adds numbers\n")[0]["name"] == "adds numbers"
        assert parse_failures("all good\n5 passed\n") == []
