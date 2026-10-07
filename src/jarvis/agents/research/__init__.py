"""Research Agent: web research, comparing sources, summaries, data analysis, documentation.

With an AI provider the model searches (server-side search when the provider has it, otherwise
the local `web_search` tool), reads sources and writes a cited report. Offline it still searches
the web, reads the best pages and builds an extractive summary with sources.
"""

from __future__ import annotations

import asyncio
import csv
import io
import re
import statistics
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from jarvis.agents.base import AgentResult, AgentTask, BaseAgent, RunContext, skill, t
from jarvis.agents.research.summarize import summarize as extractive_summary
from jarvis.agents.research.weather import (
    FORECAST_URL,
    GEOCODE_URL,
    forecast_query,
    format_weather,
    geocode_query,
)
from jarvis.common.enums import AgentId, ResultStatus
from jarvis.common.errors import KillSwitchEngaged, TaskCancelled
from jarvis.core.intents import I
from jarvis.tools.base import ToolResult

SKIP_HOSTS = ("youtube.com", "youtu.be", "facebook.com", "instagram.com", "tiktok.com", "x.com", "twitter.com",
              "pinterest.com", "linkedin.com")
_LOCATION_PATTERNS = (
    re.compile(r"(?:אני\s+)?(?:גר|גרה|מתגורר|מתגוררת|גרים)\s+ב([^\s,.;]+(?:\s+[^\s,.;]+)?)"),
    re.compile(r"\b(?:i\s+live\s+in|i'm\s+from|i\s+am\s+from|my\s+city\s+is)\s+([A-Za-z][A-Za-z .'-]{1,40})", re.I),
)


def slugify(text: str, limit: int = 48) -> str:
    slug = re.sub(r"[^\w֐-׿]+", "-", text.strip().lower()).strip("-")
    return (slug[:limit].rstrip("-") or "report")


class ResearchAgent(BaseAgent):
    id = AgentId.RESEARCH
    persona = (
        "Your job: find accurate, current information and turn it into clear, useful answers. Search the web, read "
        "the strongest sources (official documentation, the original publisher, reputable sites), compare them, "
        "point out where they disagree, and summarize. Analyze data the user gives you (files, tables) with real "
        "numbers. Cite every important claim with its source URL. Separate facts from estimates and say when "
        "something may be out of date. When asked to look for a solution to an error, find the root cause and the "
        "fix that the sources agree on."
    )
    llm_tools = ("web_search", "fetch_url", "http_request", "download_file", "read_file", "list_dir", "find_files",
                 "file_info", "write_file", "recall", "remember", "project_info")
    server_web_search = True

    # ------------------------------------------------------------------ research
    @skill(description="Research a question on the web and summarize the findings with sources",
           intents=(I.RESEARCH,))
    async def research(self, task: AgentTask, run: RunContext, query: str = "", save: bool = False,
                       max_sources: int = 4) -> AgentResult:
        query = (query or task.instruction).strip()
        if task.allow_llm and self.ai_available:
            return await self.run_llm(task, run)
        return await self.offline_research(task, run, query, save=save, max_sources=max_sources)

    async def offline_research(self, task: AgentTask, run: RunContext, query: str, *, save: bool = False,
                               max_sources: int = 4) -> AgentResult:
        lang = task.language
        await run.progress(0.05, "searching")
        found = await run.tool("web_search", query=query, max_results=8)
        if not found.ok:
            return AgentResult.failed(t(lang, f"החיפוש ברשת נכשל: {found.error}", f"The web search failed: {found.error}"),
                                      data={"code": "search_failed"})
        results = [r for r in (found.data or {}).get("results", [])
                   if not any(h in urlparse(r.get("url", "")).netloc for h in SKIP_HOSTS)]
        if not results:
            return AgentResult.failed(t(lang, f"לא נמצאו תוצאות עבור: {query}", f"No results for: {query}"))
        await run.progress(0.25, "reading sources")
        pages = await self._read_pages(run, results[: max(1, max_sources)])
        await run.progress(0.8, "summarizing")
        texts = [p["content"] for p in pages]
        snippets = [r.get("snippet", "") for r in results[:8] if r.get("snippet")]
        picked = extractive_summary(texts or snippets, query, max_sentences=7, max_chars=1600)
        sources = [{"title": p["title"] or p["url"], "url": p["url"]} for p in pages] or \
                  [{"title": r.get("title", ""), "url": r.get("url", "")} for r in results[:5]]
        if not picked:
            picked_text = [s for s in snippets[:4]]
        else:
            picked_text = [f"{s.text} [{s.source + 1}]" if pages else s.text for s in picked]
        lead = " ".join(s.text for s in picked[:2]) if picked else (snippets[0] if snippets else "")
        header = t(lang, f"סיכום מחקר: {query}", f"Research summary: {query}")
        lines = [f"# {header}", "", *[f"- {p}" for p in picked_text], "",
                 t(lang, "## מקורות", "## Sources"), *[f"{i}. {s['title']} — {s['url']}" for i, s in enumerate(sources, 1)]]
        if not pages:
            lines.append(t(lang, "\n(לא ניתן היה לקרוא את הדפים עצמם; הסיכום מבוסס על תקצירי תוצאות החיפוש.)",
                           "\n(The pages themselves could not be read; this is based on the search snippets.)"))
        details = "\n".join(lines)
        files: list[str] = []
        if save or task.context.get("goal"):
            path = f"workspace/research/{datetime.now():%Y%m%d-%H%M}-{slugify(query)}.md"
            saved = await run.tool("write_file", path=path, content=details)
            if saved.ok:
                files = saved.files_created or saved.files_modified
        status = ResultStatus.SUCCESS if pages else ResultStatus.PARTIAL
        return run.result(status=status, summary=lead[:500] or header, details=details, sources=sources,
                          result={"query": query, "points": picked_text, "sources": sources}, files_created=files,
                          next_step=None if pages else t(lang, "להוסיף מפתח AI לקבלת מחקר מעמיק יותר.",
                                                         "Add an AI key for deeper research."))

    async def _read_pages(self, run: RunContext, results: list[dict[str, Any]]) -> list[dict[str, str]]:
        async def one(r: dict[str, Any]) -> dict[str, str] | None:
            res = await run.tool("fetch_url", url=r["url"], max_chars=20000)
            if not res.ok or not (res.data or {}).get("content"):
                return None
            return {"url": res.data.get("url") or r["url"], "title": res.data.get("title") or r.get("title", ""),
                    "content": res.data["content"]}

        outcomes = await asyncio.gather(*(one(r) for r in results), return_exceptions=True)
        for item in outcomes:
            if isinstance(item, TaskCancelled | KillSwitchEngaged):
                raise item
        run.check_cancelled()
        return [o for o in outcomes if isinstance(o, dict)]

    # ------------------------------------------------------------------ summaries
    @skill(description="Summarize a document, a web page or a piece of text")
    async def summarize(self, task: AgentTask, run: RunContext, path: str = "", url: str = "", text: str = "",
                        query: str = "", max_sentences: int = 6) -> AgentResult:
        lang = task.language
        if task.allow_llm and self.ai_available:
            return await self.run_llm(task, run)
        title = ""
        if path:
            res, text, _more = await read_text(run, path, limit=200_000)
            if not res.ok:
                return AgentResult.failed(res.error or "read failed")
            title = res.data["path"]
        elif url:
            res = await run.tool("fetch_url", url=url, max_chars=40_000)
            if not res.ok:
                return AgentResult.failed(res.error or "fetch failed")
            text, title = res.data["content"], res.data.get("title") or url
        text = text or task.instruction
        picked = extractive_summary(text, query, max_sentences=max_sentences)
        if not picked:
            return AgentResult.failed(t(lang, "לא נמצא טקסט לסכם.", "There was no text to summarize."))
        body = "\n".join(f"- {s.text}" for s in picked)
        return run.result(summary=" ".join(s.text for s in picked[:2])[:500], details=body,
                          result={"source": title or None, "points": [s.text for s in picked]})

    # ------------------------------------------------------------------ data analysis
    @skill(description="Analyze a CSV/TSV table: columns, counts, numeric statistics and top values")
    async def analyze_data(self, task: AgentTask, run: RunContext, path: str = "", question: str = "") -> AgentResult:
        lang = task.language
        if not path:
            if task.allow_llm and self.ai_available:
                return await self.run_llm(task, run)
            return AgentResult.failed(t(lang, "צריך נתיב לקובץ הנתונים.", "A path to the data file is needed."))
        res, content, more = await read_text(run, path, limit=2_000_000)
        if not res.ok:
            return AgentResult.failed(res.error or "read failed")
        if more:  # analyze whole rows only
            content = content[: content.rfind("\n") + 1] or content
        stats = analyze_table(content)
        if stats is None:
            if task.allow_llm and self.ai_available:
                return await self.run_llm(task, run)
            return AgentResult.failed(t(lang, "הקובץ אינו טבלה שאפשר לנתח (CSV/TSV).",
                                        "The file is not a table I can analyze (CSV/TSV)."))
        if task.allow_llm and self.ai_available and question:
            task.context = {**task.context, "data_profile": stats}
            return await self.run_llm(task, run)
        lines = [t(lang, f"{stats['rows']} שורות, {len(stats['columns'])} עמודות.",
                   f"{stats['rows']} rows, {len(stats['columns'])} columns.")]
        if more:
            stats["partial"] = True
            lines[0] += t(lang, " (הקובץ גדול, נותחו השורות הראשונות בלבד.)",
                          " (The file is large; only the first rows were analyzed.)")
        for col in stats["columns"]:
            if col["type"] == "number":
                lines.append(f"- {col['name']}: min {col['min']}, max {col['max']}, mean {col['mean']}, "
                             f"median {col['median']}, sum {col['sum']}")
            else:
                top = ", ".join(f"{v} ({n})" for v, n in col["top"][:3])
                lines.append(f"- {col['name']}: {col['distinct']} distinct; top: {top}")
        return run.result(summary=lines[0], details="\n".join(lines), result=stats, data={"profile": stats})

    # ------------------------------------------------------------------ weather
    @skill(description="Current weather and forecast for a city (Open-Meteo)", intents=(I.WEATHER,))
    async def weather(self, task: AgentTask, run: RunContext, location: str | None = None,
                      when: str = "today") -> AgentResult:
        lang = task.language
        place_name = (location or "").strip() or await self._default_location()
        if not place_name:
            return AgentResult.failed(
                t(lang, "באיזו עיר? אפשר לומר למשל: מה מזג האוויר בחיפה.", "Which city? For example: weather in London."),
                data={"code": "need_location"},
                next_step=t(lang, "לומר לי 'תזכור שאני גר ב...' ואזכור את העיר.", "Tell me 'remember I live in ...'."))
        geo = await run.tool("http_request", method="GET", url=GEOCODE_URL, query=geocode_query(place_name, lang))
        if not geo.ok:
            return AgentResult.failed(t(lang, f"שירות מזג האוויר לא זמין: {geo.error}",
                                        f"The weather service is unavailable: {geo.error}"))
        body = (geo.data or {}).get("body") or {}
        places = body.get("results") if isinstance(body, dict) else None
        if not places:
            return AgentResult.failed(t(lang, f"לא מצאתי מקום בשם '{place_name}'.", f"I couldn't find a place called '{place_name}'."))
        place = places[0]
        forecast = await run.tool("http_request", method="GET", url=FORECAST_URL,
                                  query=forecast_query(float(place["latitude"]), float(place["longitude"])))
        if not forecast.ok or not isinstance((forecast.data or {}).get("body"), dict):
            return AgentResult.failed(t(lang, "לא הצלחתי לקבל תחזית.", "I couldn't get the forecast."))
        text, info = format_weather(place, forecast.data["body"], "tomorrow" if when == "tomorrow" else "today", lang)
        memory = self.runtime.memory
        if memory is not None:
            memory.short.note("weather_location", place_name)
        return run.result(summary=text, result=info, data=info,
                          sources=[{"title": "Open-Meteo", "url": "https://open-meteo.com"}])

    async def _default_location(self) -> str | None:
        memory = self.runtime.memory
        if memory is None:
            return None
        last = memory.short.get("weather_location")
        if last:
            return str(last)
        for fact in await memory.facts(limit=300):
            for pattern in _LOCATION_PATTERNS:
                m = pattern.search(fact.text)
                if m:
                    return m.group(1).strip()
        return None

    # ------------------------------------------------------------------ offline routing
    def pick_skill(self, task: AgentTask) -> tuple[str, dict[str, Any]] | None:
        picked = super().pick_skill(task)
        if picked is not None:
            return picked
        # Any free-form research instruction can be handled by searching the web.
        return "research", {"query": task.params.get("query") or task.instruction, **task.params}


async def read_text(run: RunContext, path: str, *, limit: int) -> tuple[ToolResult, str, bool]:
    """Read up to `limit` characters of a file page by page (read_file returns at most 60,000 per
    call). Returns the first page's result, the text and whether more was left unread."""
    first: ToolResult | None = None
    parts: list[str] = []
    offset, size = 0, 0
    while True:
        res = await run.tool("read_file", path=path, offset=offset, max_chars=60_000)
        first = first or res
        if not res.ok:
            return res, "", False
        data = res.data or {}
        chunk = str(data.get("content") or "")
        parts.append(chunk)
        size += len(chunk)
        offset = data.get("next_offset") or 0
        if not offset or not chunk:
            return first, "".join(parts), False
        if size >= limit:
            return first, "".join(parts), True


def analyze_table(content: str) -> dict[str, Any] | None:
    sample = content[:5000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        if "\t" in sample:
            dialect = csv.excel_tab  # type: ignore[assignment]
        elif "," in sample:
            dialect = csv.excel  # type: ignore[assignment]
        else:
            return None
    rows = list(csv.reader(io.StringIO(content), dialect))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if len(rows) < 2 or len(rows[0]) < 2:
        return None
    header, body = rows[0], rows[1:]
    columns = []
    for i, name in enumerate(header):
        values = [r[i].strip() for r in body if i < len(r) and r[i].strip()]
        numbers = []
        for v in values:
            try:
                numbers.append(float(v.replace(",", "").replace("₪", "").replace("$", "").replace("%", "")))
            except ValueError:
                pass
        if values and len(numbers) >= 0.8 * len(values):
            columns.append({
                "name": name, "type": "number", "count": len(numbers),
                "min": round(min(numbers), 4), "max": round(max(numbers), 4),
                "mean": round(statistics.fmean(numbers), 4), "median": round(statistics.median(numbers), 4),
                "sum": round(sum(numbers), 4),
            })
        else:
            counts: dict[str, int] = {}
            for v in values:
                counts[v] = counts.get(v, 0) + 1
            top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:10]
            columns.append({"name": name, "type": "text", "count": len(values), "distinct": len(counts), "top": top})
    return {"rows": len(body), "columns": columns}


__all__ = ["ResearchAgent", "analyze_table"]
