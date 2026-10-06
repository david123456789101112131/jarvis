"""Web tools: search, read a page, call an HTTP API, download a file.

Search uses Brave or Tavily when their API keys are configured, otherwise DuckDuckGo's HTML
endpoint (no key needed). Page reading extracts the readable text of HTML pages and PDFs.
Network access can be switched off or limited to an allowlist in the security settings.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from pydantic import BaseModel, Field

from jarvis.common.enums import Capability, RiskLevel
from jarvis.common.errors import PermissionDenied, ToolError
from jarvis.permissions.models import PathUse
from jarvis.tools.base import ActionPlan, Tool, ToolContext, ToolResult
from jarvis.tools.fs import EXECUTABLE_EXT

C = Capability
MAX_PAGE_CHARS = 40_000


def check_network(ctx: ToolContext, url: str) -> str:
    sec = ctx.services.settings.security
    if not sec.network_enabled:
        raise PermissionDenied("Network access is turned off in the security settings.")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ToolError(f"Not a web address: {url}")
    host = parsed.hostname.lower()
    if sec.network_allowlist and not any(host == d or host.endswith("." + d) for d in sec.network_allowlist):
        raise PermissionDenied(f"{host} is not in the network allowlist.")
    return host


def html_to_text(html: str, base_url: str = "") -> tuple[str, str, list[dict[str, str]]]:
    """(title, readable text, links) of an HTML page."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.get_text(" ", strip=True) if soup.title else "")[:300]
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "form", "nav", "footer", "header", "aside",
                     "template"]):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    lines: list[str] = []
    for el in root.find_all(["h1", "h2", "h3", "h4", "p", "li", "pre", "td", "th", "blockquote", "dd", "dt"]):
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        if el.name in ("h1", "h2", "h3", "h4"):
            lines.append("#" * int(el.name[1]) + " " + text)
        elif el.name == "li":
            lines.append("- " + text)
        else:
            lines.append(text)
    if not lines:
        lines = [root.get_text("\n", strip=True)]
    deduped: list[str] = []
    for line in lines:
        if not deduped or deduped[-1] != line:
            deduped.append(line)
    links = []
    for a in root.find_all("a", href=True)[:80]:
        href = a["href"]
        if href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        links.append({"text": a.get_text(" ", strip=True)[:120], "url": str(httpx.URL(base_url).join(href))
                      if base_url else href})
    return title, re.sub(r"\n{3,}", "\n\n", "\n".join(deduped)).strip(), links


# ------------------------------------------------------------------------------------------ search
class SearchParams(BaseModel):
    query: str
    max_results: int = Field(8, ge=1, le=20)
    language: Literal["auto", "he", "en"] = "auto"


class WebSearchTool(Tool):
    name = "web_search"
    description = "Search the web. Returns titles, URLs and snippets; read promising pages with fetch_url."
    capabilities = (C.NET_SEARCH,)
    Params = SearchParams
    read_only = True
    risk = RiskLevel.LOW
    timeout_s = 45.0

    async def plan(self, params: SearchParams, ctx: ToolContext) -> ActionPlan:
        return ActionPlan(summary=f"Search the web for: {params.query}", outbound=params.query,
                          destination="web search")

    async def run(self, params: SearchParams, ctx: ToolContext) -> ToolResult:
        if not ctx.services.settings.security.network_enabled:
            return ToolResult.fail("Network access is turned off in the security settings.", denied="permission")
        provider = ctx.services.settings.web.search_provider
        secrets = ctx.services.secrets
        brave = secrets.get("BRAVE_API_KEY") if secrets else None
        tavily = secrets.get("TAVILY_API_KEY") if secrets else None
        client = ctx.services.http()
        errors: list[str] = []
        order: list[str] = []
        if provider in ("auto", "brave") and brave:
            order.append("brave")
        if provider in ("auto", "tavily") and tavily:
            order.append("tavily")
        order.append("duckduckgo")
        for name in order:
            try:
                if name == "brave":
                    results = await _brave(client, params, brave or "")
                elif name == "tavily":
                    results = await _tavily(client, params, tavily or "")
                else:
                    results = await _duckduckgo(client, params)
            except (httpx.HTTPError, ToolError, ValueError, KeyError) as exc:
                errors.append(f"{name}: {type(exc).__name__}: {str(exc)[:200]}")
                continue
            if results:
                return ToolResult(summary=f"{len(results)} results for '{params.query}' ({name})",
                                  data={"query": params.query, "provider": name, "results": results})
            errors.append(f"{name}: no results")
        return ToolResult.fail(f"Web search failed: {'; '.join(errors)}", data={"query": params.query})


async def _brave(client: httpx.AsyncClient, params: SearchParams, key: str) -> list[dict[str, str]]:
    q: dict[str, Any] = {"q": params.query, "count": params.max_results}
    if params.language != "auto":
        q["search_lang"] = params.language
    r = await client.get("https://api.search.brave.com/res/v1/web/search", params=q,
                         headers={"X-Subscription-Token": key, "Accept": "application/json"})
    r.raise_for_status()
    items = r.json().get("web", {}).get("results", [])
    return [{"title": i.get("title", ""), "url": i.get("url", ""), "snippet": _strip_tags(i.get("description", ""))}
            for i in items[: params.max_results] if i.get("url")]


async def _tavily(client: httpx.AsyncClient, params: SearchParams, key: str) -> list[dict[str, str]]:
    r = await client.post("https://api.tavily.com/search",
                          json={"api_key": key, "query": params.query, "max_results": params.max_results})
    r.raise_for_status()
    return [{"title": i.get("title", ""), "url": i.get("url", ""), "snippet": (i.get("content") or "")[:400]}
            for i in r.json().get("results", []) if i.get("url")]


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def parse_duckduckgo(html: str, limit: int) -> list[dict[str, str]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    out: list[dict[str, str]] = []
    for res in soup.select(".result"):
        a = res.select_one("a.result__a")
        if a is None or not a.get("href"):
            continue
        href = str(a["href"])
        if "duckduckgo.com/l/" in href:
            target = parse_qs(urlparse(href if href.startswith("http") else "https:" + href).query).get("uddg")
            href = unquote(target[0]) if target else href
        if "duckduckgo.com/y.js" in href or not href.startswith("http"):
            continue  # ads / internal links
        snippet = res.select_one(".result__snippet")
        out.append({"title": a.get_text(" ", strip=True), "url": href,
                    "snippet": snippet.get_text(" ", strip=True) if snippet else ""})
        if len(out) >= limit:
            break
    return out


async def _duckduckgo(client: httpx.AsyncClient, params: SearchParams) -> list[dict[str, str]]:
    data = {"q": params.query}
    if params.language == "he":
        data["kl"] = "il-he"
    r = await client.post("https://html.duckduckgo.com/html/", data=data,
                          headers={"Referer": "https://html.duckduckgo.com/"})
    r.raise_for_status()
    return parse_duckduckgo(r.text, params.max_results)


# ------------------------------------------------------------------------------------------ fetch
class FetchParams(BaseModel):
    url: str
    max_chars: int = Field(20000, ge=500, le=MAX_PAGE_CHARS)
    include_links: bool = False


class FetchUrlTool(Tool):
    name = "fetch_url"
    description = "Download a web page (or PDF) and return its readable text, title and optionally its links."
    capabilities = (C.NET_FETCH,)
    Params = FetchParams
    read_only = True
    risk = RiskLevel.LOW
    timeout_s = 60.0

    async def plan(self, params: FetchParams, ctx: ToolContext) -> ActionPlan:
        url = params.url if "://" in params.url else "https://" + params.url
        check_network(ctx, url)
        return ActionPlan(summary=f"Read {url}", destination=url)

    async def run(self, params: FetchParams, ctx: ToolContext) -> ToolResult:
        url = params.url if "://" in params.url else "https://" + params.url
        check_network(ctx, url)
        limit = ctx.services.settings.web.max_fetch_bytes
        client = ctx.services.http()
        async with client.stream("GET", url) as r:
            if r.status_code >= 400:
                return ToolResult.fail(f"{url} returned HTTP {r.status_code}", data={"status": r.status_code})
            ctype = r.headers.get("content-type", "").lower()
            body = bytearray()
            async for chunk in r.aiter_bytes():
                body.extend(chunk)
                if len(body) > limit:
                    break
            final_url = str(r.url)
        if "pdf" in ctype or final_url.lower().endswith(".pdf"):
            text, title, links = await asyncio.to_thread(_pdf_text, bytes(body)), Path(urlparse(final_url).path).name, []
        elif "html" in ctype or body[:200].lstrip().lower().startswith((b"<!doctype", b"<html")):
            encoding = r.charset_encoding or "utf-8"
            title, text, links = await asyncio.to_thread(html_to_text, bytes(body).decode(encoding, errors="replace"),
                                                         final_url)
        elif "json" in ctype or ctype.startswith("text/") or not ctype:
            text, title, links = bytes(body).decode(r.charset_encoding or "utf-8", errors="replace"), "", []
        else:
            return ToolResult.fail(f"{url} is not a readable page ({ctype or 'unknown type'}); use download_file.")
        truncated = len(text) > params.max_chars
        data: dict[str, Any] = {"url": final_url, "title": title, "content": text[: params.max_chars],
                                "truncated": truncated, "total_chars": len(text)}
        if params.include_links:
            data["links"] = links
        return ToolResult(summary=f"Read '{title or final_url}' ({len(text)} characters)", data=data)


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return "\n\n".join((p.extract_text() or "").strip() for p in reader.pages)


# ------------------------------------------------------------------------------------------ http api
class HttpParams(BaseModel):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    url: str
    query: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    json_body: Any = None
    text_body: str | None = None


class HttpRequestTool(Tool):
    name = "http_request"
    description = ("Call a web API (GET/POST/...). Data sent out of the computer is checked: secrets are never "
                   "sent and personal data needs approval.")
    capabilities = (C.NET_API,)
    Params = HttpParams
    risk = RiskLevel.MEDIUM
    timeout_s = 90.0

    async def plan(self, params: HttpParams, ctx: ToolContext) -> ActionPlan:
        check_network(ctx, params.url)
        outbound_parts = [json.dumps(params.query, ensure_ascii=False), json.dumps(params.headers, ensure_ascii=False)]
        if params.json_body is not None:
            outbound_parts.append(json.dumps(params.json_body, ensure_ascii=False, default=str))
        if params.text_body:
            outbound_parts.append(params.text_body)
        writes = params.method != "GET"
        return ActionPlan(summary=f"{params.method} {params.url}", outbound="\n".join(outbound_parts),
                          destination=params.url, risk=RiskLevel.HIGH if params.method == "DELETE" else None,
                          risk_reasons=["changes data on a remote service"] if writes else [])

    async def run(self, params: HttpParams, ctx: ToolContext) -> ToolResult:
        check_network(ctx, params.url)
        client = ctx.services.http()
        r = await client.request(params.method, params.url, params=params.query or None,
                                 headers=params.headers or None, json=params.json_body,
                                 content=params.text_body.encode() if params.text_body else None)
        ctype = r.headers.get("content-type", "")
        body: Any
        if "json" in ctype:
            try:
                body = r.json()
            except ValueError:
                body = r.text[:MAX_PAGE_CHARS]
        else:
            body = r.text[:MAX_PAGE_CHARS]
        ok = r.status_code < 400
        res = ToolResult(ok=ok, summary=f"{params.method} {params.url} -> HTTP {r.status_code}",
                         data={"status": r.status_code, "headers": dict(list(r.headers.items())[:30]), "body": body})
        if not ok:
            res.error = res.summary
        return res


# ------------------------------------------------------------------------------------------ download
class DownloadParams(BaseModel):
    url: str
    path: str = Field("", description="Destination file or folder (default: workspace/downloads)")


class DownloadFileTool(Tool):
    name = "download_file"
    description = "Download a file from the web to a folder."
    capabilities = (C.NET_FETCH, C.FS_WRITE)
    Params = DownloadParams
    risk = RiskLevel.MEDIUM
    timeout_s = 1800.0

    def _target(self, params: DownloadParams, ctx: ToolContext) -> Path:
        name = Path(urlparse(params.url).path).name or "download"
        if not params.path:
            return ctx.services.paths.workspace / "downloads" / name
        p = ctx.resolve(params.path)
        return p / name if (p.exists() and p.is_dir()) or params.path.endswith(("/", "\\")) else p

    async def plan(self, params: DownloadParams, ctx: ToolContext) -> ActionPlan:
        check_network(ctx, params.url)
        target = self._target(params, ctx)
        executable = target.suffix.lower() in EXECUTABLE_EXT
        return ActionPlan(summary=f"Download {params.url} to {target}", destination=params.url,
                          paths=[PathUse(path=str(target), access="write")],
                          risk=RiskLevel.HIGH if executable else None,
                          risk_reasons=["downloads a program or script"] if executable else [])

    async def run(self, params: DownloadParams, ctx: ToolContext) -> ToolResult:
        check_network(ctx, params.url)
        target = self._target(params, ctx)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".part")
        size = 0
        max_bytes = 2_000_000_000
        client = ctx.services.http()
        async with client.stream("GET", params.url) as r:
            if r.status_code >= 400:
                return ToolResult.fail(f"Download failed: HTTP {r.status_code}")
            total = int(r.headers.get("content-length") or 0)
            with tmp.open("wb") as fh:
                async for chunk in r.aiter_bytes(1 << 16):
                    ctx.check_cancelled()
                    fh.write(chunk)
                    size += len(chunk)
                    if size > max_bytes:
                        raise ToolError("File is larger than 2 GB; download stopped.")
                    if total:
                        await ctx.progress(size / total, f"{size // 1024} KB")
        tmp.replace(target)
        return ToolResult(summary=f"Downloaded {target.name} ({size // 1024} KB)", files_created=[str(target)],
                          data={"path": str(target), "bytes": size})


WEB_TOOLS: tuple[type[Tool], ...] = (WebSearchTool, FetchUrlTool, HttpRequestTool, DownloadFileTool)
