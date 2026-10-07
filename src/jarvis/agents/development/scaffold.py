"""Project templates for the Development Agent and the tiny, safe renderer that fills them.

Each template lives in `templates/<kind>/`: a `template.json` (name, how it runs and is tested,
UI strings in Hebrew and English, generated data files) and a `files/` tree copied into the new
project. Text files may use:

* `{{ name }}` / `{{ a.b }}` — a variable; HTML-escaped in .html/.svg/.xml files, raw elsewhere.
* `{{ name|json }}` — JSON-encoded and safe inside <script> (use it in .js files); other filters:
  `raw`, `html`, `slug`, `upper`, `lower`, `url`.
* `{{#if x}}…{{else}}…{{/if}}`, `{{#unless x}}…{{/unless}}` and `{{#each items}}…{{ this.name }}
  {{ @number }}…{{/each}}`.

There is no expression evaluation of any kind: names are looked up in the variables, nothing else.
File names may contain `__name__` placeholders. Nothing is ever written outside the destination.
"""

from __future__ import annotations

import html
import json
import re
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from jarvis.creative.palette import Palette

TEMPLATES_DIR = Path(__file__).with_name("templates")
TEXT_SUFFIXES = {".html", ".htm", ".css", ".js", ".mjs", ".cjs", ".json", ".md", ".txt", ".py", ".toml", ".cfg",
                 ".ini", ".yml", ".yaml", ".svg", ".xml", ".webmanifest", ".example", ".gitignore", ".env"}
HTML_SUFFIXES = {".html", ".htm", ".svg", ".xml"}
_IGNORED_PARTS = {"__pycache__", ".pytest_cache", "node_modules", ".venv", ".ruff_cache"}


class TemplateError(ValueError):
    """A template could not be parsed or rendered."""


class RunSpec(BaseModel):
    """How to run (or test) a generated project. `argv` may contain `{python}` and `{node}`."""

    argv: list[str] = Field(default_factory=list)
    cwd: str = "."
    url: str | None = None
    kind: Literal["static", "python", "node"] = "static"


class TemplateInfo(BaseModel):
    kind: str
    name: str
    name_he: str = ""
    description: str
    description_he: str = ""
    language: str
    entry: str
    run: RunSpec
    test: RunSpec | None = None
    requirements: list[str] = Field(default_factory=list)
    needs: list[str] = Field(default_factory=list)          # runtimes: "python", "node"
    generators: list[str] = Field(default_factory=list)      # extra generated files (see GENERATORS)
    strings: dict[str, dict[str, str]] = Field(default_factory=dict)
    files: list[str] = Field(default_factory=list)

    def label(self, lang: str) -> str:
        return self.name_he if lang == "he" and self.name_he else self.name

    def describe(self, lang: str) -> str:
        return self.description_he if lang == "he" and self.description_he else self.description


@dataclass
class ScaffoldResult:
    files: list[Path]
    info: TemplateInfo
    variables: dict[str, Any] = field(default_factory=dict)
    dest: Path | None = None


# ------------------------------------------------------------------------------------------ templates
@lru_cache(maxsize=1)
def _load_all() -> dict[str, TemplateInfo]:
    found: dict[str, TemplateInfo] = {}
    if not TEMPLATES_DIR.is_dir():
        return found
    for meta in sorted(TEMPLATES_DIR.glob("*/template.json")):
        data = json.loads(meta.read_text(encoding="utf-8"))
        root = meta.parent / "files"
        data["files"] = sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                               if p.is_file() and not _IGNORED_PARTS.intersection(p.parts)
                               and not p.name.endswith((".pyc", ".pyo")))
        info = TemplateInfo.model_validate(data)
        found[info.kind] = info
    return found


def list_templates() -> list[TemplateInfo]:
    return list(_load_all().values())


def get_template(kind: str) -> TemplateInfo:
    templates = _load_all()
    key = (kind or "").strip().lower()
    if key not in templates:
        raise KeyError(f"unknown template '{kind}' (available: {', '.join(templates)})")
    return templates[key]


# ------------------------------------------------------------------------------------------ renderer
_TAG = re.compile(r"\{\{\s*(#if|#unless|#each|/if|/unless|/each|else\b)?\s*([^{}]*?)\s*\}\}")
_PATH = re.compile(r"^(?:@[a-z]+|this|[A-Za-z_][\w-]*)(?:\.[\w-]+)*$")


@dataclass
class _Text:
    text: str


@dataclass
class _Var:
    path: str
    filters: list[str]


@dataclass
class _Block:
    kind: str  # if | unless | each
    path: str
    body: list[Any] = field(default_factory=list)
    other: list[Any] = field(default_factory=list)
    in_else: bool = False


def parse(source: str) -> list[Any]:
    """Parse a template into nodes (raises TemplateError on a malformed template)."""
    root: list[Any] = []
    stack: list[_Block] = []

    def target() -> list[Any]:
        if not stack:
            return root
        top = stack[-1]
        return top.other if top.in_else else top.body

    pos = 0
    for m in _TAG.finditer(source):
        if m.start() > pos:
            target().append(_Text(source[pos:m.start()]))
        pos = m.end()
        op, arg = m.group(1), m.group(2).strip()
        if op in ("#if", "#unless", "#each"):
            if not _PATH.match(arg):
                raise TemplateError(f"bad block argument '{arg}'")
            block = _Block(op[1:], arg)
            target().append(block)
            stack.append(block)
        elif op == "else":
            if not stack or stack[-1].in_else:
                raise TemplateError("{{else}} outside a block")
            stack[-1].in_else = True
        elif op in ("/if", "/unless", "/each"):
            if not stack or stack[-1].kind != op[1:]:
                raise TemplateError(f"unexpected {{{{{op}}}}}")
            stack.pop()
        else:
            parts = [p.strip() for p in arg.split("|")]
            if not parts[0] or not _PATH.match(parts[0]):
                raise TemplateError(f"bad variable '{arg}'")
            for f in parts[1:]:
                if f not in FILTERS:
                    raise TemplateError(f"unknown filter '{f}'")
            target().append(_Var(parts[0], parts[1:]))
    if stack:
        raise TemplateError(f"unclosed {{{{#{stack[-1].kind} {stack[-1].path}}}}}")
    if pos < len(source):
        target().append(_Text(source[pos:]))
    return root


def _json(value: Any) -> str:
    out = json.dumps(value, ensure_ascii=False)
    return (out.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def _slug(value: Any) -> str:
    slug = re.sub(r"[^\w\-]+", "-", str(value).strip().lower(), flags=re.UNICODE).strip("-")
    return slug or "item"


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


FILTERS: dict[str, Callable[[Any], str]] = {
    "raw": _text,
    "html": lambda v: html.escape(_text(v), quote=True),
    "json": _json,
    "slug": _slug,
    "upper": lambda v: _text(v).upper(),
    "lower": lambda v: _text(v).lower(),
    "url": lambda v: quote(_text(v), safe=""),
}
_TERMINAL = {"raw", "html", "json", "url"}
_MISSING = object()


class _Scope:
    def __init__(self, variables: dict[str, Any]) -> None:
        self.frames: list[dict[str, Any]] = [variables]

    def lookup(self, path: str) -> Any:
        head, *rest = path.split(".")
        value: Any = _MISSING
        if head.startswith("@") or head == "this":
            for frame in reversed(self.frames):
                if head in frame:
                    value = frame[head]
                    break
        else:
            for frame in reversed(self.frames[1:]):
                this = frame.get("this")
                if isinstance(this, dict) and head in this:
                    value = this[head]
                    break
            else:
                value = self.frames[0].get(head, _MISSING)
        for part in rest:
            if isinstance(value, dict) and part in value:
                value = value[part]
            elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
                value = value[int(part)]
            else:
                value = _MISSING
                break
        if value is _MISSING:
            raise TemplateError(f"unknown variable '{path}'")
        return value


def _render(nodes: list[Any], scope: _Scope, html_mode: bool, out: list[str]) -> None:
    for node in nodes:
        if isinstance(node, _Text):
            out.append(node.text)
        elif isinstance(node, _Var):
            value = scope.lookup(node.path)
            filters = list(node.filters)
            if not filters or filters[-1] not in _TERMINAL:
                filters.append("html" if html_mode else "raw")
            for name in filters:
                value = FILTERS[name](value)
            out.append(value)
        elif node.kind in ("if", "unless"):
            truthy = bool(scope.lookup(node.path))
            if node.kind == "unless":
                truthy = not truthy
            _render(node.body if truthy else node.other, scope, html_mode, out)
        else:  # each
            items = scope.lookup(node.path)
            if isinstance(items, dict):
                items = [{"key": k, "value": v} for k, v in items.items()]
            if not isinstance(items, list | tuple):
                raise TemplateError(f"'{node.path}' is not a list")
            if not items:
                _render(node.other, scope, html_mode, out)
            for i, item in enumerate(items):
                scope.frames.append({"this": item, "@index": i, "@number": i + 1, "@first": i == 0,
                                     "@last": i == len(items) - 1})
                try:
                    _render(node.body, scope, html_mode, out)
                finally:
                    scope.frames.pop()


def render_string(source: str, variables: dict[str, Any], *, html_mode: bool = False) -> str:
    out: list[str] = []
    _render(parse(source), _Scope(variables), html_mode, out)
    return "".join(out)


_NAME_VAR = re.compile(r"__([A-Za-z][A-Za-z0-9_]*)__")


def _render_name(rel: str, variables: dict[str, Any]) -> str:
    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in variables:
            return m.group(0)
        return _slug(variables[key])

    return _NAME_VAR.sub(sub, rel)


def _is_text(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES or path.name in TEXT_SUFFIXES or path.name in {
        "Dockerfile", "Procfile", "LICENSE"}


def _safe_target(dest: Path, rel: str) -> Path:
    target = (dest / rel).resolve()
    if target != dest and dest not in target.parents:
        raise TemplateError(f"refusing to write outside the project folder: {rel}")
    return target


def render_template(kind: str, dest: Path, variables: dict[str, Any] | None = None, *,
                    overwrite: bool = False) -> ScaffoldResult:
    """Create a project from template `kind` in `dest` and return the files written."""
    info = get_template(kind)
    dest = Path(dest).expanduser()
    if dest.exists() and not dest.is_dir():
        raise FileExistsError(f"{dest} exists and is not a folder")
    if dest.is_dir() and any(dest.iterdir()) and not overwrite:
        raise FileExistsError(f"{dest} already exists and is not empty")
    given = dict(variables or {})
    if "palette_css" not in given:  # not built by default_variables(): fill in the defaults
        given = {**default_variables(kind, str(given.get("description", "")), str(given.get("lang", "he"))), **given}
    values = _finish_variables(given, info)
    dest.mkdir(parents=True, exist_ok=True)
    dest = dest.resolve()
    root = TEMPLATES_DIR / info.kind / "files"
    written: list[Path] = []
    for rel in info.files:
        src = root / rel
        target = _safe_target(dest, _render_name(rel, values))
        target.parent.mkdir(parents=True, exist_ok=True)
        if _is_text(src):
            text = src.read_text(encoding="utf-8")
            try:
                rendered = render_string(text, values, html_mode=src.suffix.lower() in HTML_SUFFIXES)
            except TemplateError as exc:
                raise TemplateError(f"{info.kind}/{rel}: {exc}") from exc
            target.write_text(rendered, encoding="utf-8", newline="\n")
        else:
            shutil.copyfile(src, target)
        written.append(target)
    for name in info.generators:
        for rel, content in GENERATORS[name](values):
            target = _safe_target(dest, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content, encoding="utf-8", newline="\n")
            written.append(target)
    return ScaffoldResult(files=written, info=info, variables=values, dest=dest)


# ------------------------------------------------------------------------------------------ variables
_NAMED = re.compile(r"(?:בשם|שנקרא|שנקראת|called|named)\s+[\"'“”״]?([^\"'“”״,.;\n]{2,40})", re.I)


def _brand_from_brief(brief: str) -> str | None:
    m = _NAMED.search(brief or "")
    return m.group(1).strip() if m else None


_STORE_SETTINGS = {
    "he": {"currency": "₪", "currency_code": "ILS", "locale": "he-IL", "vat_rate": 0.18, "shipping_fee": 30,
           "free_shipping_over": 300, "prices_include_vat": True},
    "en": {"currency": "$", "currency_code": "USD", "locale": "en-US", "vat_rate": 0.0, "shipping_fee": 7.99,
           "free_shipping_over": 75, "prices_include_vat": False},
}


def default_variables(kind: str, brief: str = "", lang: str = "he", *, palette: Palette | None = None,
                      brand: str | None = None) -> dict[str, Any]:
    """Sensible variables for a template from a short brief ("חנות לפולי קפה", "fitness tracker").

    `palette` (for example the brand kit from the Creative Agent) and `brand` replace the colors and
    the name that would otherwise be derived from the brief.
    """
    from jarvis.creative.domains import GENERIC, detect_domain
    from jarvis.creative.icons import icon_svg
    from jarvis.creative.logo import favicon_svg, initials
    from jarvis.creative.palette import generate_palette
    from jarvis.creative.texts import landing_copy

    info = get_template(kind)
    lang = "he" if str(lang).lower().startswith("he") else "en"
    brief = (brief or "").strip()
    topic = brief or info.label(lang)
    copy = landing_copy(topic, lang=lang)
    brand = (brand or "").strip() or _brand_from_brief(brief) or str(copy.get("name") or info.label(lang))
    palette = palette or generate_palette(f"{kind} {brief}".strip())
    domain = detect_domain(brief)
    products_src = domain.products or GENERIC.products
    features = [{"icon": f["icon"], "title": f["title"], "text": f["text"],
                 "icon_svg": icon_svg(str(f["icon"]), size=28, color="currentColor")}
                for f in copy.get("features", [])]  # type: ignore[union-attr]
    products = []
    for i, p in enumerate(products_src):
        products.append({
            "id": p.key, "name": p.name[lang], "category": p.category[lang], "price": float(p.price[lang]),
            "description": p.description[lang], "icon": p.icon, "image": f"images/products/{p.key}.svg",
            "rating": round(4.2 + ((i * 37) % 8) / 10, 1), "stock": 5 + (i * 13) % 40, "featured": i < 3,
        })
    categories = list(dict.fromkeys(p["category"] for p in products))
    values: dict[str, Any] = {
        "kind": kind, "lang": lang, "dir": "rtl" if lang == "he" else "ltr", "title": brand, "brand": brand,
        "description": brief or str(copy.get("subheadline", "")), "brief": brief,
        "headline": copy.get("headline", ""), "subheadline": copy.get("subheadline", ""), "cta": copy.get("cta", ""),
        "secondary_cta": copy.get("secondary_cta", ""), "about": copy.get("about", ""),
        "label": copy.get("label", ""), "essence": copy.get("essence", ""), "offer": copy.get("offer", ""),
        "emoji": copy.get("emoji", ""), "audience": list(copy.get("audience", [])),  # type: ignore[arg-type]
        "features": features, "testimonials": list(copy.get("testimonials", [])),  # type: ignore[arg-type]
        "initials": initials(brand) or brand[:1], "year": date.today().year,
        "favicon_svg": favicon_svg(brand, palette), "palette_css": palette.to_css(),
        "colors": {role: palette.hex(role) for role in ("primary", "secondary", "accent", "background", "surface",
                                                         "text", "muted", "border", "on_primary")},
        "products": products, "categories": categories, "domain": domain.key,
        "email": "hello@example.com", "phone": "03-555-0123" if lang == "he" else "+1 (555) 010-0123",
        "address": "רחוב הרצל 1, תל אביב" if lang == "he" else "100 Main Street, Springfield",
        **_STORE_SETTINGS[lang],
    }
    return values


def _finish_variables(values: dict[str, Any], info: TemplateInfo) -> dict[str, Any]:
    lang = "he" if str(values.get("lang", "he")).startswith("he") else "en"
    values["lang"] = lang
    values["dir"] = "rtl" if lang == "he" else "ltr"
    values.setdefault("brand", values.get("title", info.label(lang)))
    values["slug"] = _slug(values.get("slug") or values.get("title") or info.kind)
    ascii_name = re.sub(r"[^a-z0-9]+", "-", str(values.get("title", "")).lower()).strip("-")
    values["package_name"] = values.get("package_name") or (ascii_name if len(ascii_name) >= 3 else f"{info.kind}-project")
    values["is_he"] = lang == "he"
    strings = info.strings.get(lang) or info.strings.get("en") or {}
    values["ui"] = {**strings, **(values.get("ui") or {})}
    values["store_config"] = {
        "currency": values.get("currency"), "currencyCode": values.get("currency_code"),
        "locale": values.get("locale"), "vatRate": values.get("vat_rate"), "shippingFee": values.get("shipping_fee"),
        "freeShippingOver": values.get("free_shipping_over"), "pricesIncludeVat": values.get("prices_include_vat"),
        "storageKey": f"{values['slug']}-cart",
    }
    return values


# ------------------------------------------------------------------------------------------ generators
def _product_images(values: dict[str, Any]) -> Iterable[tuple[str, str]]:
    from jarvis.creative.icons import icon_inner
    from jarvis.creative.palette import mix

    colors = values.get("colors") or {}
    primary = colors.get("primary", "#4f46e5")
    secondary = colors.get("secondary", "#7c3aed")
    accent = colors.get("accent", "#f59e0b")
    for i, p in enumerate(values.get("products") or []):
        start = mix(primary, secondary, (i % 4) / 4)
        end = mix(secondary, accent, ((i + 1) % 3) / 3)
        inner = icon_inner(str(p.get("icon") or "package"), color="#ffffff")
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 400" width="400" height="400" role="img">'
            f"<title>{html.escape(str(p.get('name', '')))}</title><defs>"
            f'<linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{start}"/>'
            f'<stop offset="1" stop-color="{end}"/></linearGradient></defs>'
            '<rect width="400" height="400" fill="url(#g)"/>'
            '<circle cx="330" cy="70" r="90" fill="#ffffff" opacity=".08"/>'
            '<circle cx="60" cy="350" r="120" fill="#ffffff" opacity=".06"/>'
            '<g transform="translate(110 110) scale(7.5)" fill="none" stroke="#ffffff" stroke-width="1.6" '
            f'stroke-linecap="round" stroke-linejoin="round">{inner}</g></svg>'
        )
        yield f"images/products/{_slug(p.get('id') or i)}.svg", svg


def _products_js(values: dict[str, Any]) -> Iterable[tuple[str, str]]:
    note = ("// רשימת המוצרים של החנות. אפשר לערוך, להוסיף ולמחוק מוצרים כאן.\n" if values.get("lang") == "he"
            else "// The store's catalog. Edit, add or remove products here.\n")
    products = values.get("products") or []
    body = json.dumps(products, ensure_ascii=False, indent=2)
    config = json.dumps(values.get("store_config") or {}, ensure_ascii=False, indent=2)
    yield "data/products.js", (
        f"{note}(function (root) {{\n  'use strict';\n  var products = {body};\n  var config = {config};\n"
        "  if (typeof module === 'object' && module.exports) {\n"
        "    module.exports = { products: products, config: config };\n"
        "  } else {\n    root.STORE_PRODUCTS = products;\n    root.STORE_CONFIG = config;\n  }\n"
        "})(typeof self !== 'undefined' ? self : this);\n"
    )


def _favicon(values: dict[str, Any]) -> Iterable[tuple[str, str]]:
    yield "favicon.svg", str(values.get("favicon_svg") or "")


GENERATORS: dict[str, Callable[[dict[str, Any]], Iterable[tuple[str, str | bytes]]]] = {
    "product_images": _product_images,
    "products_js": _products_js,
    "favicon": _favicon,
}
