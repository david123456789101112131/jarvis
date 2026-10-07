"""Project templates: every template renders, links resolve, and its own tests pass."""

from __future__ import annotations

import html.parser
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis.agents.development import scaffold
from jarvis.agents.development.scaffold import TemplateError, render_string

NODE = shutil.which("node")
WEB = ("website", "store", "webapp", "game")
ALL = ("website", "store", "webapp", "game", "api", "script", "bot", "node-api")


class _Links(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.refs: list[str] = []
        self.root_attrs: dict[str, str | None] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "html":
            self.root_attrs = values
        for name in ("href", "src"):
            ref = values.get(name)
            if ref:
                self.refs.append(ref)


def _local(ref: str) -> str | None:
    if ref.startswith(("http:", "https:", "mailto:", "tel:", "#", "data:", "javascript:")) or "{" in ref:
        return None
    return ref.split("#", 1)[0].split("?", 1)[0] or None


def render(kind: str, dest: Path, lang: str = "he", brief: str = "") -> scaffold.ScaffoldResult:
    variables = scaffold.default_variables(kind, brief or f"{kind} project", lang)
    return scaffold.render_template(kind, dest, variables)


def test_all_templates_are_listed():
    kinds = {t.kind for t in scaffold.list_templates()}
    assert kinds == set(ALL)
    for info in scaffold.list_templates():
        assert info.files and info.entry in info.files
        assert info.description and info.description_he and info.name_he
        assert info.run.kind in ("static", "python", "node")
    with pytest.raises(KeyError):
        scaffold.get_template("spaceship")


@pytest.mark.parametrize("kind", ALL)
def test_renders_without_leftover_placeholders(kind, tmp_path):
    result = render(kind, tmp_path / kind)
    assert result.files
    for path in result.files:
        assert path.is_file()
        assert tmp_path / kind in path.parents
        if path.suffix in (".png", ".ico"):
            continue
        text = path.read_text(encoding="utf-8")
        assert "{{" not in text and "}}" not in text.replace("}}}", ""), f"placeholder left in {path.name}"
        if path.suffix == ".json":
            json.loads(text)
    assert (tmp_path / kind / result.info.entry).is_file()


@pytest.mark.parametrize("kind", WEB)
@pytest.mark.parametrize("lang", ["he", "en"])
def test_web_pages_link_to_real_files(kind, lang, tmp_path):
    dest = tmp_path / f"{kind}-{lang}"
    result = render(kind, dest, lang)
    pages = [p for p in result.files if p.suffix == ".html"]
    assert pages
    for page in pages:
        parser = _Links()
        parser.feed(page.read_text(encoding="utf-8"))
        assert parser.root_attrs.get("dir") == ("rtl" if lang == "he" else "ltr")
        assert parser.root_attrs.get("lang") == lang
        for ref in parser.refs:
            target = _local(ref)
            if target:
                assert (page.parent / target).exists(), f"{page.name} links to missing {ref}"


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
@pytest.mark.parametrize("kind", ["website", "store", "webapp", "game", "node-api"])
def test_js_templates_pass_their_own_tests(kind, tmp_path):
    dest = tmp_path / kind
    render(kind, dest, "he" if kind != "node-api" else "en")
    for js in dest.rglob("*.js"):
        check = subprocess.run([NODE, "--check", str(js)], capture_output=True, text=True, timeout=60)
        assert check.returncode == 0, f"{js.name}: {check.stderr}"
    run = subprocess.run([NODE, "--test"], cwd=dest, capture_output=True, text=True, timeout=180)
    assert run.returncode == 0, run.stdout[-2000:] + run.stderr[-2000:]
    assert "# fail 0" in run.stdout


@pytest.mark.parametrize("kind", ["api", "script", "bot"])
def test_python_templates_pass_their_own_tests(kind, tmp_path):
    if kind == "api":
        pytest.importorskip("fastapi")
        pytest.importorskip("httpx")
    dest = tmp_path / kind
    render(kind, dest, "en")
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("PYTEST_ADDOPTS", None)
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=dest,
                         capture_output=True, text=True, timeout=300, env=env)
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-2000:]


def test_store_data_follows_language_and_brief(tmp_path):
    he = scaffold.default_variables("store", "חנות לפולי קפה בשם 'קפה שקד'", "he")
    assert he["brand"] == "קפה שקד" and he["currency_code"] == "ILS" and he["dir"] == "rtl"
    assert he["products"] and all(p["price"] > 0 for p in he["products"])
    assert any("קפה" in p["name"] or "קפה" in p["category"] for p in he["products"])
    en = scaffold.default_variables("store", "a bookstore", "en")
    assert en["currency_code"] == "USD" and en["dir"] == "ltr"
    result = scaffold.render_template("store", tmp_path / "shop", he)
    images = [p for p in result.files if p.parent.name == "products"]
    assert len(images) == len(he["products"])
    data = (tmp_path / "shop" / "data" / "products.js").read_text(encoding="utf-8")
    assert he["products"][0]["name"] in data and '"currencyCode": "ILS"' in data


def test_refuses_non_empty_destination_unless_overwrite(tmp_path):
    dest = tmp_path / "site"
    dest.mkdir()
    (dest / "keep.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(FileExistsError):
        render("website", dest)
    render_again = scaffold.render_template("website", dest, scaffold.default_variables("website", "x", "en"),
                                            overwrite=True)
    assert (dest / "keep.txt").read_text(encoding="utf-8") == "mine"
    assert (dest / "index.html").is_file() and render_again.files


def test_renderer_escaping_and_blocks():
    values = {"a": "<b>&\"", "items": [{"n": 1}, {"n": 2}], "flag": False, "cfg": {"x": "y"}, "name": "root"}
    out = render_string("{{ a }}|{{ a|raw }}|{{#each items}}[{{ n }}:{{ @number }}:{{ name }}]{{/each}}|"
                        "{{#if flag}}T{{else}}F{{/if}}|{{#unless flag}}U{{/unless}}|{{ cfg.x }}", values, html_mode=True)
    assert out == '&lt;b&gt;&amp;&quot;|<b>&"|[1:1:root][2:2:root]|F|U|y'
    assert render_string("{{ a|json }}", {"a": "</script><x>"}) == '"\\u003c/script\\u003e\\u003cx\\u003e"'
    assert render_string("{{ a }}", {"a": "<raw>"}) == "<raw>"
    assert render_string("{{ t|slug }}", {"t": "Hello World!"}) == "hello-world"


@pytest.mark.parametrize("source", [
    "{{ missing }}", "{{ a.__class__ }}", "{{ __import__('os') }}", "{{#if a}}open", "{{/if}}", "{{ a|evil }}",
    "{{ a + 1 }}",
])
def test_renderer_rejects_bad_templates(source):
    with pytest.raises(TemplateError):
        render_string(source, {"a": "x"})


def test_never_writes_outside_the_destination(tmp_path):
    with pytest.raises(TemplateError):
        scaffold._safe_target(tmp_path.resolve(), "../escape.txt")
    assert scaffold._render_name("src/__slug__.py", {"slug": "My App"}) == "src/my-app.py"
