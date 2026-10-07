"""JARVIS Core: where every request enters, from the UI, the microphone or the local API.

    text / transcript ─► confirmation answer? ─► follow-up answer? ─► intent (rules, <5 ms)
        ├─ control command (kill switch, agents, tasks, mode, memory, time…) → answered at once
        ├─ simple intent → one agent skill through the Agent Manager (fast path, no model)
        ├─ complex intent → acknowledged at once, then planned and run by the Task Orchestrator
        └─ anything else → conversation with the AI model, which can also hand work to the agents

The core keeps the conversation (chat memory and command history), tracks context ("open it"
after a build opens the project), answers approval requests by voice, announces results of
background work, and owns the current mode. It never touches the OS itself: all work goes
through the agents and their permission-checked tools.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from jarvis.agents.base import AgentResult, AgentTask, t
from jarvis.ai.types import AIMessage
from jarvis.bus.message import event
from jarvis.common.cancel import CancelToken
from jarvis.common.clock import iso, utcnow
from jarvis.common.enums import AgentId, Mode, Priority, TaskStatus
from jarvis.common.errors import ProviderError, TaskRefused
from jarvis.common.ids import new_id
from jarvis.common.text import detect_language, normalize
from jarvis.core import replies as R
from jarvis.core.intents import ACTION_INTENTS, I, Intent, IntentDetector
from jarvis.memory.models import ChatRecord

if TYPE_CHECKING:
    from jarvis.agents.manager import AgentManager
    from jarvis.ai.router import ProviderRouter
    from jarvis.bus import MessageBus
    from jarvis.config.settings import Settings
    from jarvis.logs.activity import ActivityLog
    from jarvis.memory.manager import MemoryManager
    from jarvis.orchestration.orchestrator import TaskOrchestrator
    from jarvis.orchestration.task import JarvisTask
    from jarvis.permissions.confirm import ConfirmationBroker, ConfirmationRequest
    from jarvis.security.killswitch import KillEvent, KillSwitch

log = logging.getLogger(__name__)

Speaker = Callable[[str, str], Awaitable[Any]]
Source = Literal["text", "voice", "api", "system"]

FAST_WAIT_S = 8.0           # a fast-path command answers within this, else it reports when done
QUESTION_TTL_S = 90.0       # how long a question JARVIS asked ("which city?") waits for its answer
CHAT_HISTORY_TURNS = 12
SPOKEN_LIMIT = 400          # characters of a result that are read aloud (the rest is on screen)

# Words that refer back to the last thing JARVIS worked with ("open it").
_REFERENCES = {"אותו", "אותה", "אותם", "זה", "את זה", "it", "that", "this", "them", "the result", "התוצאה"}
_YES = re.compile(r"^(?:כן|כן כן|אשר|מאשר|מאשרת|תאשר|תאשרי|אישור|בטח|בסדר|קדימה|יאללה|סבבה|אפשר|תעשה את זה|"
                  r"yes|yeah|yep|sure|ok|okay|approve|approved|confirm|confirmed|go ahead|do it|allow)"
                  r"(?:\s+(?:תודה|please|thanks))?$", re.I)
_NO = re.compile(r"^(?:לא|לא לא|אל|אל תעשה את זה|אל תאשר|תדחה|דחה|אני לא מאשר|אני לא מאשרת|בטל|תבטל|עזוב|"
                 r"no|nope|deny|denied|reject|don't|do not|cancel|stop|don't do it)(?:\s+(?:תודה|thanks))?$", re.I)
_PREFERENCE = re.compile(r"^(?:אני\s+(?:מעדיף|מעדיפה|אוהב|אוהבת|שונא|שונאת|רוצה\s+תמיד)|תמיד\s|אף\s+פעם\s|"
                         r"i\s+(?:prefer|like|love|hate|always|never)|always\s|never\s|my\s+favou?rite)", re.I)
_PIPELINE_LABELS = {
    "understanding": ("מבין את הבקשה…", "Understanding…"),
    "routing": ("מנתב", "Routing"),
    "selecting_tool": ("בוחר כלי", "Selecting a tool"),
    "preparing_action": ("מכין את הפעולה…", "Preparing…"),
    "executing": ("מבצע…", "Executing…"),
    "done": ("הושלם", "Done"),
}


class ChatTurn(BaseModel):
    """The model's answer to something the rules did not recognise."""

    reply: str = Field(description="What JARVIS says now, in the user's language. When run_task is true: one short "
                                   "sentence acknowledging the work.")
    run_task: bool = Field(description="True when the user asks JARVIS to do something on the computer or to "
                                       "produce work (files, code, websites, research with current information, "
                                       "designs, plans) that the agents should carry out")
    task: str = Field(description="When run_task is true: the request restated as one complete, self-contained "
                                  "instruction in the user's language; otherwise an empty string")


CHAT_SYSTEM = """You are {name}, the personal AI assistant of {user} on their Windows computer. You speak like a calm,
capable assistant: warm, brief and precise. Replies may be read aloud, so keep them short (one to four sentences)
unless the user asks for detail; no tables or headings, and code only when asked.
Always answer in the user's language ({language}).
Five agents do the actual work on the computer: Research (web research, comparisons, summaries), System (files,
folders, apps, settings, system health), Development (apps, websites, code, tests), Creative (ideas, design, logos,
images, texts, presentations) and Planner (plans, stages, schedules).
Never say you did something on the computer unless it was done.{mode_rule}
Now: {now}. Mode: {mode}.{tasks}{memory}"""

_DELEGATE_RULE = """
When the user wants something done on the computer or wants work produced (files, code, a website, a design, a
plan, research that needs current information), set run_task and restate the request in "task"; the agents will do
it and report back. Answer questions, advice and conversation yourself with run_task false."""
_CHAT_MODE_RULE = """
You are in chat mode: you only talk. If the user asks you to act on the computer, say that in normal mode you
would hand it to the agents."""


@dataclass
class CommandResult:
    """What the core did with one input (the `/api/command` response, and the voice reply)."""

    accepted: bool
    message: dict[str, Any] | None
    intent: dict[str, Any]
    task_id: str | None = None
    reply: dict[str, Any] | None = None
    speech: dict[str, Any] | None = None      # {text, ack_key, language, follow_up} for the voice channel

    def view(self) -> dict[str, Any]:
        return {"accepted": self.accepted, "message": self.message, "intent": self.intent, "task_id": self.task_id,
                "reply": self.reply}


@dataclass
class _Turn:
    text: str                  # the command without the wake word
    raw: str
    intent: Intent
    lang: str
    source: str
    cmd_id: str
    message: dict[str, Any] | None = None
    agent: AgentId | None = None      # an explicit (agent, skill) chosen by the core (e.g. "open it")
    skill: str | None = None


@dataclass
class _Question:
    """JARVIS asked for a missing detail; the next answer completes the original command."""

    intent: str
    slots: dict[str, Any]
    slot: str
    lang: str
    expires: float


@dataclass
class _Fast:
    """A fast-path command still running."""

    token: CancelToken
    title: str
    icon: str
    agent: AgentId
    source: str
    lang: str
    cmd_id: str
    started: datetime = field(default_factory=utcnow)
    asking: asyncio.Event = field(default_factory=asyncio.Event)   # set when it waits for an approval
    deferred: bool = False      # the reply already went out; the result is announced when it arrives


class JarvisCore:
    def __init__(
        self,
        *,
        settings: Settings,
        manager: AgentManager,
        orchestrator: TaskOrchestrator,
        memory: MemoryManager | None,
        bus: MessageBus,
        broker: ConfirmationBroker,
        kill_switch: KillSwitch,
        activity: ActivityLog | None = None,
        router: Callable[[], ProviderRouter | None] | None = None,
        detector: IntentDetector | None = None,
    ) -> None:
        self.settings = settings
        self.manager = manager
        self.orchestrator = orchestrator
        self.memory = memory
        self.bus = bus
        self.broker = broker
        self.kill_switch = kill_switch
        self.activity = activity
        self._router = router or (lambda: manager.runtime.router)
        self.detector = detector or IntentDetector()
        self._mode = Mode(settings.general.default_mode)
        self._speaker: Speaker | None = None
        self._question: _Question | None = None
        self._fast: dict[str, _Fast] = {}
        self._task_commands: dict[str, str] = {}      # task id -> command id (to record how it ended)
        self._quiet: set[str] = set()                  # tasks whose end needs no announcement (the user stopped them)
        self._chat: deque[dict[str, Any]] = deque(maxlen=100)   # recent messages when there is no memory
        self._bg: set[asyncio.Task[Any]] = set()
        self._started = False
        self._closing = False

    # ================================================================== lifecycle
    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def router(self) -> ProviderRouter | None:
        return self._router()

    @property
    def ai(self) -> bool:
        router = self.router
        return router is not None and router.available

    def set_speaker(self, speaker: Speaker | None) -> None:
        """`speaker(text, language)` reads text aloud (the voice service); None when voice is off."""
        self._speaker = speaker

    async def start(self) -> list[JarvisTask]:
        """Start the orchestrator; tasks a previous run left unfinished are announced in the chat."""
        if self._started:
            return []
        self._started = True
        self.orchestrator.add_listener(self._on_task_event)
        self.broker.add_listener(self._on_confirm_request)
        self.kill_switch.add_listener(self._on_kill)
        recovered = await self.orchestrator.start()
        paused = [x for x in recovered if x.status == TaskStatus.INTERRUPTED and not self.orchestrator.is_running(x.id)]
        if paused:
            lang = self._reply_language(paused[0].language)
            names = ", ".join(f"'{x.title}'" for x in paused[:3])
            await self.say(t(lang, f"משימות שנקטעו כש-JARVIS נסגר: {names}. אפשר להגיד \"תמשיך את המשימה\" כדי להמשיך "
                                   "מאותה נקודה.",
                             f"Tasks cut off when JARVIS closed: {names}. Say \"continue the task\" to pick up where "
                             "they stopped."), kind="info", lang=lang, task_id=paused[0].id)
        return recovered

    async def shutdown(self) -> None:
        self._closing = True
        for fast in list(self._fast.values()):
            fast.token.cancel("JARVIS is shutting down")
        await self.orchestrator.shutdown()
        for task in list(self._bg):
            task.cancel()

    # ================================================================== input
    async def handle_input(self, text: str, *, source: Source | str = "text", language: str | None = None) -> CommandResult:
        """Handle one command or message. Returns quickly: long work continues in the background and its
        result arrives as a chat message (and speech, for voice commands)."""
        raw = " ".join((text or "").split())
        intent = self.detector.detect(raw)
        lang = self._reply_language(intent.language if re.search(r"[A-Za-z֐-׿]", intent.text or raw)
                                    else (language or detect_language(raw, "he")))
        intent_view = {"name": intent.name, "complexity": intent.complexity,
                       "agents": [a.value for a in intent.agents]}
        if not raw:
            return CommandResult(False, None, intent_view)
        cmd_id = new_id("cmd")
        if self.memory is not None:
            with contextlib.suppress(Exception):
                cmd_id = (await self.memory.add_command(raw, source=str(source), intent=intent.name)).id
        message = await self.say(raw, role="user", kind="answer", lang=lang, source=str(source), intent=intent.name)
        turn = _Turn(text=intent.text or raw, raw=raw, intent=intent, lang=lang, source=str(source), cmd_id=cmd_id,
                     message=message)
        await self._pipeline("understanding", "done", lang, intent.name)
        try:
            result = await self._route(turn)
        except Exception as exc:
            log.exception("Command failed: %s", raw)
            reply = await self.say(t(lang, f"משהו השתבש: {exc}", f"Something went wrong: {exc}"), kind="error",
                                   lang=lang, intent=intent.name)
            await self._pipeline("done", "error", lang)
            await self._record(turn, ok=False)
            return CommandResult(True, message, intent_view, reply=reply,
                                 speech={"ack_key": "error", "language": lang, "follow_up": False})
        result.message = message
        result.intent = {**intent_view, "name": turn.intent.name, "complexity": turn.intent.complexity}
        if turn.source != "voice":
            await self._pipeline("done", "done", lang)
        return result

    async def voice_command(self, text: str, language: str | None = None) -> CommandResult:
        return await self.handle_input(text, source="voice", language=language)

    # ================================================================== routing
    async def _route(self, turn: _Turn) -> CommandResult:
        answer = self._approval_answer(turn.text)
        if answer is not None:
            return await self._answer_approval(turn, answer)
        self._fill_question(turn)
        self._resolve_reference(turn)
        intent = turn.intent
        if self._mode == Mode.DEBUG:
            await self.say(f"[debug] intent={intent.name} ({intent.complexity}, {intent.confidence:.2f}) "
                           f"slots={intent.slots} agents={[a.value for a in intent.agents]}", kind="info",
                           lang=turn.lang, intent=intent.name)
        handler = self._instant.get(intent.name)
        if handler is not None:
            return await handler(self, turn)
        if self._mode == Mode.CHAT and (intent.name in ACTION_INTENTS or turn.skill):
            return await self._reply(turn, R.chat_mode_refusal(turn.lang), kind="info", ok=False)
        if intent.name == I.RESUME_TASK:
            return await self._resume(turn)
        if intent.name == I.CHAT:
            return await self._converse(turn)
        if self.kill_switch.global_engaged:
            return await self._reply(turn, t(turn.lang, "מתג החירום פעיל, ולכן הסוכנים לא מבצעים פעולות. אפשר להגיד "
                                                         "\"שחרר את מתג החירום\".",
                                             "The kill switch is engaged, so the agents take no actions. Say \"release "
                                             "the kill switch\"."), kind="error", ok=False)
        if intent.name == I.AGENT_ASSIGN:
            return await self._assign(turn)
        if intent.complexity == "simple" or turn.skill:
            return await self._fast_path(turn)
        return await self._task(turn)

    # -- context ---------------------------------------------------------------------------------
    def _fill_question(self, turn: _Turn) -> None:
        """"Which city?" — "Haifa": the answer completes the command that asked for it."""
        q = self._question
        if q is None:
            return
        self._question = None
        intent = turn.intent
        # Only a bare answer ("בחיפה", "from the desktop") completes the question; anything that reads as a
        # command of its own (even the same kind of command, said again in full) is a new command.
        if time.monotonic() > q.expires or intent.name != I.CHAT:
            return
        value = re.sub(r"^(?:in|at|for|to)\s+", "", turn.text.strip(" .,!?"), flags=re.I)
        if not value or len(value.split()) > 6:
            return
        slots = {**q.slots, q.slot: value}
        turn.intent = Intent(q.intent, slots, 0.9, turn.lang, list(intent.agents) or [], "simple", turn.text,
                             intent.had_wake_word)

    def _resolve_reference(self, turn: _Turn) -> None:
        """"Open it" right after a build opens the project; after opening an app, "close it" closes it."""
        intent = turn.intent
        if intent.name not in (I.OPEN_APP, I.CLOSE_APP) or self.memory is None:
            return
        target = normalize(str(intent.slots.get("app") or ""))
        if target not in _REFERENCES:
            return
        entities = self.memory.short.entities
        keys = ("last_app",) if intent.name == I.CLOSE_APP else ("last_project", "last_app", "last_file", "last_url")
        found = [(str(entities.get(f"{k}_at") or ""), k) for k in keys if entities.get(k)]
        if not found:
            return
        _, key = max(found)
        value = str(entities[key])
        if key == "last_project":
            project = value
            turn.intent = Intent("open_project", {"name": project, "app_name": project}, 0.9, turn.lang,
                                 [AgentId.SYSTEM], "simple", turn.text)
            turn.agent, turn.skill = AgentId.SYSTEM, "open_project"
        elif key == "last_app":
            turn.intent = Intent(intent.name, {"app": value, "app_name": value}, 0.9, turn.lang, [AgentId.SYSTEM],
                                 "simple", turn.text)
        elif key == "last_file":
            turn.intent = Intent(I.OPEN_FOLDER, {"path": value, "name": value.replace("\\", "/").rsplit("/", 1)[-1]},
                                 0.9, turn.lang, [AgentId.SYSTEM], "simple", turn.text)
        else:
            turn.intent = Intent(I.OPEN_WEBSITE, {"url": value, "site": value}, 0.9, turn.lang, [AgentId.SYSTEM],
                                 "simple", turn.text)

    # -- approvals ---------------------------------------------------------------------------------
    def _approval_answer(self, text: str) -> bool | None:
        if not self.broker.pending():
            return None
        clean = normalize(text).strip(" .,!?")
        if _YES.match(clean):
            return True
        if _NO.match(clean):
            return False
        return None

    async def _answer_approval(self, turn: _Turn, approved: bool) -> CommandResult:
        req = self.broker.latest_pending()
        if req is None:
            return await self._reply(turn, t(turn.lang, "אין בקשה שממתינה לאישור.", "Nothing is waiting for approval."))
        if approved and turn.source == "voice" and (req.irreversible or req.risk == "critical"):
            return await self._reply(turn, R.approval_needs_click(req.summary, turn.lang), kind="info",
                                     follow_up=False, task_id=req.task_id)
        self.broker.respond(req.id, approved, by=f"user ({turn.source})")
        turn.intent = Intent("confirm", {"approved": approved, "id": req.id}, 0.95, turn.lang, [], "instant",
                             turn.text)
        await self._log(t(turn.lang, f"{'אושר' if approved else 'נדחה'} בקול/טקסט: {req.summary}",
                          f"{'Approved' if approved else 'Denied'} by the user: {req.summary}"),
                        level="success" if approved else "warning", agent_id=req.agent_id, task_id=req.task_id)
        return await self._reply(turn, R.approval_answer(approved, req.summary, turn.lang), follow_up=False,
                                 task_id=req.task_id)

    # -- the fast path ---------------------------------------------------------------------------------
    async def _fast_path(self, turn: _Turn) -> CommandResult:
        intent = turn.intent
        if turn.agent and turn.skill:
            agent, skill_name = turn.agent, turn.skill
        else:
            pair = self.manager.skill_for_intent(intent.name)
            if pair is None:
                return await self._task(turn)
            agent, skill_name = pair
        if self._mode == Mode.TASK:      # task mode: even a simple command becomes a (one-step) task
            title, _icon = R.action_title(intent.name, intent.slots, turn.lang)
            plan = self.orchestrator.direct_plan(agent, turn.text, title=title.rstrip("…"), skill=skill_name,
                                                 params=dict(intent.slots), lang=turn.lang)
            return await self._task(turn, plan=plan)
        lang = turn.lang
        title, icon = R.action_title(intent.name, intent.slots, lang)
        await self._pipeline("routing", "done", lang, R.agent_name(agent, lang))
        await self._pipeline("selecting_tool", "done", lang, skill_name)
        await self._pipeline("executing", "active", lang, title)
        await self._action(turn.cmd_id, title, icon, "running")
        token = CancelToken()
        fast = _Fast(token=token, title=title, icon=icon, agent=agent, source=turn.source, lang=lang, cmd_id=turn.cmd_id)
        self._fast[turn.cmd_id] = fast
        request = AgentTask(task_id=turn.cmd_id, agent=agent, skill=skill_name, params=dict(intent.slots),
                            instruction=turn.text, title=title.rstrip("…"), priority=Priority.HIGH, mode=self._mode,
                            language=lang)
        running = asyncio.ensure_future(self.manager.run(request, cancel=token))
        asking = asyncio.ensure_future(fast.asking.wait())
        try:
            await asyncio.wait({running, asking}, timeout=FAST_WAIT_S, return_when=asyncio.FIRST_COMPLETED)
        finally:
            asking.cancel()
        if running.done():
            self._fast.pop(turn.cmd_id, None)
            return await self._fast_done(turn, fast, running.result())
        # Still running (waiting for an approval, or simply slow): answer now, report the result later.
        fast.deferred = True
        running.add_done_callback(lambda f: self._spawn(self._fast_finished_later(turn, fast, f)))
        if fast.asking.is_set():
            req = next((r for r in self.broker.pending() if r.task_id == turn.cmd_id), None)
            text = R.waiting_for_approval(agent.value, req.summary if req else title, lang)
            return await self._reply(turn, text, kind="info", ok=None, ack="confirm", follow_up=True,
                                     speak_text=False)
        return await self._reply(turn, t(lang, f"{title} זה לוקח קצת זמן; אעדכן כשזה יסתיים.",
                                         f"{title} This takes a moment; I'll tell you when it's done."),
                                 kind="ack", ok=None, ack="on_it", speak_text=False)

    async def _fast_done(self, turn: _Turn, fast: _Fast, result: AgentResult) -> CommandResult:
        lang = turn.lang
        ok = result.ok
        summary = result.summary or (fast.title if ok else t(lang, "הפעולה נכשלה.", "The action failed."))
        state = self._settle(turn, result)
        await self._action(turn.cmd_id, summary, fast.icon, state, progress=1.0)
        await self._pipeline("executing", "error" if state == "error" else "done", lang, summary)
        text = summary
        if result.details and turn.intent.name in (I.SYSTEM_STATUS, I.FIND_FILES):
            text += "\n" + result.details[:3000]
        await self._record(turn, ok=ok)
        return await self._reply(turn, text, kind="result" if ok else "info" if state == "info" else "error", ok=ok,
                                 speak=summary, follow_up=not ok or self._question is not None)

    def _settle(self, turn: _Turn, result: AgentResult) -> str:
        """The action card's end state; a question back ("which city?") waits for the user's answer."""
        if result.asks_user:
            self._question = _Question(turn.intent.name, dict(turn.intent.slots), "location", turn.lang,
                                       time.monotonic() + QUESTION_TTL_S)
            return "info"
        return "done" if result.ok else "error"

    async def _fast_finished_later(self, turn: _Turn, fast: _Fast, future: asyncio.Future[AgentResult]) -> None:
        self._fast.pop(turn.cmd_id, None)
        if future.cancelled() or self._closing:
            return
        result = future.result()
        lang = turn.lang
        ok = result.ok
        summary = result.summary or fast.title
        state = self._settle(turn, result)
        await self._action(turn.cmd_id, summary, fast.icon, state, progress=1.0)
        await self._pipeline("executing", "error" if state == "error" else "done", lang, summary)
        await self._record(turn, ok=ok)
        if result.status.value == "cancelled" and turn.cmd_id in self._quiet:
            return
        await self.say(summary, kind="result" if ok else "info" if state == "info" else "error", lang=lang,
                       intent=turn.intent.name)
        if turn.source == "voice":
            await self._speak(summary, lang)

    # -- tasks ------------------------------------------------------------------------------------------
    async def _task(self, turn: _Turn, *, plan: Any = None, title: str | None = None) -> CommandResult:
        intent = turn.intent
        lang = turn.lang
        if self._mode == Mode.CHAT:
            return await self._reply(turn, R.chat_mode_refusal(lang), kind="info", ok=False)
        await self._pipeline("routing", "done", lang, t(lang, "מחלק בין הסוכנים", "Splitting between the agents"))
        try:
            task = await self.orchestrator.submit(
                turn.text, title=title, intent=None if intent.name == I.CHAT else intent.name,
                slots=dict(intent.slots), language=lang, mode=self._mode, source=turn.source, plan=plan)
        except TaskRefused as exc:
            return await self._reply(turn, exc.message, kind="info", ok=False)
        self._task_commands[task.id] = turn.cmd_id
        await self._record(turn, task_id=task.id)
        agents = list(plan.agents) if plan is not None else list(intent.agents)
        text = R.complex_ack(agents, lang) if agents else t(lang, "קיבלתי. מתחיל לעבוד על זה.",
                                                             "Got it. Working on it.")
        if plan is not None and (len(plan.subtasks) > 1 or self._mode in (Mode.TASK, Mode.DEBUG)):
            # The orchestrator reports only the plans it makes. A plan given up front is shown with the ack
            # itself, so the order of the messages never depends on how fast the first step runs.
            text += "\n" + R.plan_message(task.detail_view()["plan"], lang)
        ack = "complex_ack" if len(agents) != 1 else "single_ack"
        return await self._reply(turn, text, kind="ack", ok=None, task_id=task.id, ack=ack, speak_text=False,
                                 follow_up=False)

    async def _assign(self, turn: _Turn) -> CommandResult:
        slots = turn.intent.slots
        agent = AgentId.parse(slots["agent"])
        instruction = str(slots.get("instruction") or turn.text)
        plan = self.orchestrator.direct_plan(agent, instruction, lang=turn.lang)
        # The task is the instruction ("give the Development Agent this task: X" is the task X).
        return await self._task(turn, plan=plan, title=plan.title)

    async def agent_task(self, agent: AgentId | str, instruction: str, *, priority: Priority | str | None = None,
                         source: str = "api", language: str | None = None) -> JarvisTask:
        """Give one agent a task directly (the Agents panel); it runs as a one-step task."""
        lang = self._reply_language(language or detect_language(instruction, "he"))
        plan = self.orchestrator.direct_plan(agent, instruction, lang=lang)
        return await self.orchestrator.submit(instruction, language=lang, mode=self._mode if self._mode != Mode.CHAT
                                              else Mode.NORMAL, priority=priority, source=source, plan=plan)

    async def _resume(self, turn: _Turn) -> CommandResult:
        lang = turn.lang
        if self.kill_switch.global_engaged:
            return await self._reply(turn, t(lang, "מתג החירום עדיין פעיל. אפשר להגיד \"שחרר את מתג החירום\" ואז להמשיך.",
                                             "The kill switch is still engaged. Release it first, then continue."),
                                     kind="info", ok=False)
        task_id = turn.intent.slots.get("task_id") or await self._latest_task(("interrupted",)) or \
            await self._latest_task(("failed", "cancelled"))
        if not task_id:
            return await self._reply(turn, R.nothing_to_resume(lang), kind="info", ok=False)
        try:
            task = await self.orchestrator.resume(task_id)
        except KeyError:
            return await self._reply(turn, R.nothing_to_resume(lang), kind="info", ok=False)
        except TaskRefused as exc:
            return await self._reply(turn, exc.message, kind="info", ok=False)
        self._task_commands[task.id] = turn.cmd_id
        subs = task.plan.subtasks if task.plan else []
        done = sum(1 for s in subs if s.status == TaskStatus.COMPLETED)
        await self._record(turn, task_id=task.id)
        return await self._reply(turn, R.resumed(task.title, done, len(subs), lang), kind="ack", ok=None,
                                 task_id=task.id, follow_up=False)

    async def _latest_task(self, statuses: tuple[str, ...]) -> str | None:
        best: tuple[str, str] | None = None
        for status in statuses:
            for row in await self.orchestrator.list_tasks(status=status, limit=1):
                if best is None or row["created_at"] > best[0]:
                    best = (row["created_at"], row["id"])
        return best[1] if best else None

    # -- conversation -------------------------------------------------------------------------------------
    async def _converse(self, turn: _Turn) -> CommandResult:
        lang = turn.lang
        router = self.router
        if router is None or not router.available:
            return await self._reply(turn, R.offline_chat(lang), kind="info")
        await self._pipeline("routing", "active", lang, t(lang, "חושב…", "Thinking…"))
        system = await self._chat_system(turn)
        messages = self._chat_messages(turn)
        try:
            if self._mode == Mode.CHAT:
                response = await router.complete(router.request("chat", messages, system=system))
                decision = ChatTurn(reply=response.text.strip(), run_task=False, task="")
            else:
                decision = await router.structured("chat", messages, ChatTurn, system=system)
        except ProviderError as exc:
            await self._pipeline("routing", "error", lang, exc.message[:80])
            return await self._reply(turn, t(lang, f"לא הצלחתי לקבל תשובה ממודל ה-AI: {exc.message}",
                                             f"I couldn't get an answer from the AI model: {exc.message}"),
                                     kind="error", ok=False)
        await self._pipeline("routing", "done", lang, t(lang, "סוכנים" if decision.run_task else "שיחה",
                                                        "agents" if decision.run_task else "conversation"))
        if decision.run_task and decision.task.strip() and self._mode != Mode.CHAT:
            if self.kill_switch.global_engaged:
                return await self._reply(turn, t(lang, "מתג החירום פעיל, ולכן הסוכנים לא מבצעים פעולות.",
                                                 "The kill switch is engaged, so the agents take no actions."),
                                         kind="error", ok=False)
            try:
                task = await self.orchestrator.submit(decision.task.strip(), language=lang, mode=self._mode,
                                                      source=turn.source)
            except TaskRefused as exc:
                return await self._reply(turn, exc.message, kind="info", ok=False)
            self._task_commands[task.id] = turn.cmd_id
            await self._record(turn, task_id=task.id)
            turn.intent = Intent(I.CHAT, {"task": decision.task}, turn.intent.confidence, lang, [], "complex",
                                 turn.text)
            text = decision.reply.strip() or R.complex_ack([], lang)
            return await self._reply(turn, text, kind="ack", ok=None, task_id=task.id, follow_up=False)
        return await self._reply(turn, decision.reply.strip() or R.offline_chat(lang))

    async def _chat_system(self, turn: _Turn) -> str:
        lang = turn.lang
        memory_text = ""
        if self.memory is not None:
            with contextlib.suppress(Exception):
                recall = await self.memory.recall(turn.text)
                if not recall.is_empty():
                    memory_text = "\nWhat you remember about the user:\n" + recall.format(1500)
        active = self.orchestrator.active_tasks()
        tasks = ("\nTasks running now: " + "; ".join(f"{x.title} ({x.status.value})" for x in active[:5])) if active \
            else ""
        return CHAT_SYSTEM.format(
            name=self.settings.general.assistant_name or "JARVIS", user=self.settings.general.user_name or "the user",
            language="Hebrew" if lang == "he" else "English",
            mode_rule=_CHAT_MODE_RULE if self._mode == Mode.CHAT else _DELEGATE_RULE,
            now=datetime.now().strftime("%A %Y-%m-%d %H:%M"), mode=self._mode.value, tasks=tasks, memory=memory_text)

    def _chat_messages(self, turn: _Turn) -> list[AIMessage]:
        history: list[ChatRecord] = []
        if self.memory is not None:
            history = [r for r in self.memory.short.recent(CHAT_HISTORY_TURNS + 1)
                       if r.role in ("user", "jarvis") and r.kind != "info"]
            if history and turn.message and history[-1].id == turn.message.get("id"):
                history = history[:-1]
        messages: list[AIMessage] = []
        for rec in history[-CHAT_HISTORY_TURNS:]:
            role: Literal["user", "assistant"] = "user" if rec.role == "user" else "assistant"
            if messages and messages[-1].role == role:
                messages[-1] = AIMessage(role=role, text=f"{messages[-1].text}\n{rec.text}")
            else:
                messages.append(AIMessage(role=role, text=rec.text))
        while messages and messages[0].role != "user":
            messages.pop(0)
        if messages and messages[-1].role == "user":
            messages[-1] = AIMessage.user(f"{messages[-1].text}\n{turn.text}")
        else:
            messages.append(AIMessage.user(turn.text))
        return messages

    # ================================================================== instant commands
    async def _time(self, turn: _Turn) -> CommandResult:
        return await self._reply(turn, R.time_now(turn.lang))

    async def _date(self, turn: _Turn) -> CommandResult:
        return await self._reply(turn, R.date_today(turn.lang))

    async def _help(self, turn: _Turn) -> CommandResult:
        return await self._reply(turn, R.help_text(turn.lang), speak=R.help_spoken(turn.lang))

    async def _greeting(self, turn: _Turn) -> CommandResult:
        return await self._reply(turn, R.greeting(turn.lang, self.settings.general.user_name))

    async def _thanks(self, turn: _Turn) -> CommandResult:
        return await self._reply(turn, R.thanks(turn.lang), follow_up=False)

    async def _set_mode_cmd(self, turn: _Turn) -> CommandResult:
        mode = Mode(turn.intent.slots["mode"])
        if mode == self._mode:
            return await self._reply(turn, R.mode_already(mode, turn.lang))
        await self.set_mode(mode, source=turn.source)
        return await self._reply(turn, R.mode_changed(mode, turn.lang))

    async def set_mode(self, mode: Mode | str, *, source: str = "api") -> Mode:
        mode = Mode(mode)
        if mode != self._mode:
            old, self._mode = self._mode, mode
            await self._publish("mode", {"mode": mode.value})
            await self._log(f"Mode: {old.value} → {mode.value}", kind="mode")
        return self._mode

    async def _show_agents(self, turn: _Turn) -> CommandResult:
        views = self.manager.views()
        await self._publish("ui.navigate", {"screen": "agents"})
        return await self._reply(turn, R.agents_overview(views, turn.lang), speak=R.agents_spoken(views, turn.lang))

    async def _show_tasks(self, turn: _Turn) -> CommandResult:
        active = [x.summary_view() for x in self.orchestrator.active_tasks()]
        paused = await self.orchestrator.list_tasks(status="interrupted", limit=5)
        await self._publish("ui.navigate", {"screen": "tasks"})
        text = R.tasks_overview(active, paused, turn.lang)
        return await self._reply(turn, text, speak=text.split("\n", 1)[0])

    async def _show_history(self, turn: _Turn) -> CommandResult:
        lang = turn.lang
        if turn.intent.slots.get("last"):
            rows = await self.orchestrator.list_tasks(limit=1)
            if not rows:
                return await self._reply(turn, t(lang, "עוד לא היו משימות.", "There are no tasks yet."))
            detail = await self.orchestrator.detail(rows[0]["id"]) or {}
            await self._publish("ui.navigate", {"screen": "tasks", "task_id": rows[0]["id"]})
            story = R.task_story(detail, lang)
            return await self._reply(turn, story, speak=story.split("\n", 1)[0], task_id=rows[0]["id"])
        await self._publish("ui.navigate", {"screen": "tasks"})
        lines: list[str] = []
        if self.memory is not None:
            with contextlib.suppress(Exception):
                for c in (await self.memory.command_history(11))[1:]:     # [0] is this command
                    mark = "✓" if c.ok else "✗" if c.ok is False else "•"
                    lines.append(f"{mark} {c.ts.astimezone():%H:%M} {c.text}")
        if not lines:
            return await self._reply(turn, t(lang, "עוד אין היסטוריה.", "There's no history yet."))
        text = t(lang, "הפקודות האחרונות:", "Recent commands:") + "\n" + "\n".join(lines)
        return await self._reply(turn, text, speak=t(lang, "הנה הפקודות האחרונות.", "Here are the recent commands."))

    async def _stop_task(self, turn: _Turn) -> CommandResult:
        lang = turn.lang
        reason = t(lang, "נעצר על ידי המשתמש", "stopped by the user")
        slots = turn.intent.slots
        fast = list(self._fast.values())
        if slots.get("all"):
            for f in fast:
                self._quiet.add(f.cmd_id)
                f.token.cancel(reason)
            ids = [x.id for x in self.orchestrator.active_tasks()]
            self._quiet.update(ids)
            n = await self.orchestrator.cancel_all(reason)
            if not n and not fast:
                return await self._reply(turn, R.no_task_to_stop(lang), kind="info", follow_up=False)
            return await self._reply(turn, R.tasks_cancelled(n + len(fast), lang), ack="cancelled", speak_text=False,
                                     follow_up=False)
        task_id = slots.get("task_id")
        if task_id is None:
            active = sorted(self.orchestrator.active_tasks(), key=lambda x: x.created_at)
            if fast and (not active or fast[-1].started >= active[-1].created_at):
                newest = fast[-1]              # the most recent thing started is a fast-path command
                self._quiet.add(newest.cmd_id)
                newest.token.cancel(reason)
                await self._action(newest.cmd_id, t(lang, "בוטל", "Cancelled"), newest.icon, "error", progress=1.0)
                return await self._reply(turn, t(lang, "עצרתי את הפעולה.", "I stopped the action."),
                                         ack="cancelled", speak_text=False, follow_up=False)
            if not active:
                return await self._reply(turn, R.no_task_to_stop(lang), kind="info", follow_up=False)
            task_id = active[-1].id
        self._quiet.add(task_id)
        task = await self.orchestrator.cancel(task_id, reason)
        if task is None:
            return await self._reply(turn, R.no_task_to_stop(lang), kind="info", follow_up=False)
        return await self._reply(turn, R.task_cancelled(task.title, lang), task_id=task.id, ack="cancelled",
                                 speak_text=False, follow_up=False)

    async def _kill(self, turn: _Turn) -> CommandResult:
        n = len(self.orchestrator.active_tasks()) + len(self._fast)
        await self.engage_kill_switch(t(turn.lang, "עצירת חירום מהמשתמש", "emergency stop by the user"))
        return await self._reply(turn, R.kill_engaged(turn.lang, n), ack="stopped", speak_text=False, follow_up=False)

    async def engage_kill_switch(self, reason: str, agent_id: str | None = None) -> None:
        if agent_id is None:
            self._quiet.update(x.id for x in self.orchestrator.active_tasks())
            for f in list(self._fast.values()):
                self._quiet.add(f.cmd_id)
                f.token.cancel(reason)
        await self.kill_switch.engage(reason, agent_id=agent_id)   # agents, tasks and approvals react (listeners)

    async def _release(self, turn: _Turn) -> CommandResult:
        if not self.kill_switch.global_engaged and not self.kill_switch.engaged_agents():
            return await self._reply(turn, R.kill_not_engaged(turn.lang), kind="info")
        await self.release_kill_switch()
        paused = await self.orchestrator.list_tasks(status="interrupted", limit=1)
        return await self._reply(turn, R.kill_released(turn.lang, paused))

    async def release_kill_switch(self, agent_id: str | None = None) -> None:
        if agent_id is not None:
            await self.kill_switch.release(agent_id=agent_id)
            return
        for agent in list(self.kill_switch.engaged_agents()):
            await self.kill_switch.release(agent_id=agent)
        if self.kill_switch.global_engaged:
            await self.kill_switch.release()

    async def _agent_command(self, turn: _Turn) -> CommandResult:
        name = turn.intent.name
        agent = AgentId.parse(turn.intent.slots["agent"])
        action = {I.AGENT_STOP: "stop", I.AGENT_ACTIVATE: "start", I.AGENT_RESTART: "restart"}[name]
        reason = t(turn.lang, "נעצר על ידי המשתמש", "stopped by the user")
        if action == "stop":
            await self.manager.stop_agent(agent, reason)
        else:
            if self.kill_switch.is_engaged(agent.value) and not self.kill_switch.global_engaged:
                await self.kill_switch.release(agent_id=agent.value)
            if self.kill_switch.global_engaged:
                return await self._reply(turn, t(turn.lang, "מתג החירום הכללי פעיל. אפשר להגיד \"שחרר את מתג החירום\".",
                                                 "The global kill switch is engaged. Say \"release the kill switch\"."),
                                         kind="info", ok=False)
            if action == "start":
                await self.manager.start_agent(agent)
            else:
                await self.manager.restart_agent(agent)
        paused = await self.orchestrator.list_tasks(status="interrupted", limit=1) if action != "stop" else []
        return await self._reply(turn, R.agent_control(action, agent, turn.lang, paused))

    async def _stop_all_agents(self, turn: _Turn) -> CommandResult:
        await self.manager.stop_all(t(turn.lang, "כל הסוכנים נעצרו על ידי המשתמש", "all agents stopped by the user"))
        return await self._reply(turn, R.all_agents_stopped(turn.lang), follow_up=False)

    async def _remember(self, turn: _Turn) -> CommandResult:
        if self.memory is None:
            return await self._reply(turn, R.memory_off(turn.lang), kind="error", ok=False)
        fact_text = str(turn.intent.slots.get("fact") or "").strip()
        category = "preference" if _PREFERENCE.search(fact_text) else "fact"
        fact = await self.memory.remember(fact_text, category, source="user")
        self.memory.short.note("last_fact", fact.id)
        return await self._reply(turn, R.remembered(turn.lang))

    async def _forget(self, turn: _Turn) -> CommandResult:
        if self.memory is None:
            return await self._reply(turn, R.memory_off(turn.lang), kind="error", ok=False)
        query = str(turn.intent.slots.get("query") or "").strip()
        if normalize(query) in _REFERENCES:
            last = self.memory.short.get("last_fact")
            n = int(await self.memory.forget(str(last))) if last else 0
        else:
            n = await self.memory.forget_matching(query)
        return await self._reply(turn, R.forgot(n, turn.lang))

    async def _recall(self, turn: _Turn) -> CommandResult:
        lang = turn.lang
        if self.memory is None:
            return await self._reply(turn, R.memory_off(lang), kind="error", ok=False)
        query = str(turn.intent.slots.get("query") or "").strip()
        recall = await self.memory.recall(query)
        words = [w for w in normalize(query).split() if len(w) > 1]
        prefs = [f.text for f in recall.preferences if any(w in normalize(f.text) for w in words)]
        items = prefs + [f.text for f in recall.facts] + [
            p.name + (f" ({p.path})" if p.path else "") for p in recall.projects]
        items = list(dict.fromkeys(items))
        if not items:
            return await self._reply(turn, R.recall_nothing(query, lang))
        router = self.router
        if router is not None and router.available:
            prompt = (f"The user asks: {turn.text}\nWhat you remember:\n" + "\n".join(f"- {x}" for x in items[:12])
                      + f"\nAnswer briefly in {'Hebrew' if lang == 'he' else 'English'} from these memories only.")
            with contextlib.suppress(ProviderError):
                answer = (await router.ask("chat", prompt, system="You are JARVIS, a personal assistant.")).strip()
                if answer:
                    return await self._reply(turn, answer)
        return await self._reply(turn, R.recall_list(query, items, lang), speak=items[0])

    _instant: dict[str, Callable[[JarvisCore, _Turn], Awaitable[CommandResult]]] = {
        I.TIME: _time, I.DATE: _date, I.HELP: _help, I.GREETING: _greeting, I.THANKS: _thanks,
        I.SET_MODE: _set_mode_cmd, I.SHOW_AGENTS: _show_agents, I.SHOW_TASKS: _show_tasks,
        I.SHOW_HISTORY: _show_history, I.STOP_TASK: _stop_task, I.KILL_SWITCH: _kill, I.KILL_RELEASE: _release,
        I.AGENT_STOP: _agent_command, I.AGENT_ACTIVATE: _agent_command, I.AGENT_RESTART: _agent_command,
        I.STOP_ALL_AGENTS: _stop_all_agents, I.REMEMBER: _remember, I.FORGET: _forget, I.RECALL: _recall,
    }

    # ================================================================== background events
    async def _on_task_event(self, kind: str, task: JarvisTask) -> None:
        if self._closing:
            return
        lang = task.language
        if kind == "planned" and task.plan is not None and (len(task.plan.subtasks) > 1
                                                             or task.mode in (Mode.TASK, Mode.DEBUG)):
            await self.say(R.plan_message(task.detail_view()["plan"], lang), kind="plan", lang=lang, task_id=task.id)
            return
        if kind != "finished":
            return
        quiet = task.id in self._quiet
        self._quiet.discard(task.id)
        cmd_id = self._task_commands.pop(task.id, None)
        if cmd_id and self.memory is not None:
            with contextlib.suppress(Exception):
                await self.memory.update_command(cmd_id, ok=task.status == TaskStatus.COMPLETED, task_id=task.id)
        if quiet and task.status in (TaskStatus.CANCELLED, TaskStatus.INTERRUPTED):
            return       # the user stopped it and already got an answer
        outcome = task.outcome
        summary = outcome.summary if outcome else (task.error or task.title)
        text = summary
        if outcome is not None:
            if outcome.details:
                text += "\n" + outcome.details
            if outcome.next_step and task.status != TaskStatus.COMPLETED:
                text += "\n" + t(lang, f"הצעד הבא: {outcome.next_step}", f"Next step: {outcome.next_step}")
        kind_out = {TaskStatus.COMPLETED: "result", TaskStatus.FAILED: "error"}.get(task.status, "info")
        await self.say(text, kind=kind_out, lang=lang, task_id=task.id, intent=task.intent)
        if task.source == "voice" and task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.INTERRUPTED):
            await self._speak(summary, lang)

    async def _on_confirm_request(self, req: ConfirmationRequest) -> None:
        if self._closing:
            return
        fast = self._fast.get(req.task_id or "")
        if fast is not None and not fast.deferred:
            fast.asking.set()            # the command's own reply asks the question
            return
        task = None
        if req.task_id and fast is None:
            task = await self.orchestrator.get(req.task_id)
        lang = self._reply_language(fast.lang if fast else task.language if task else detect_language(req.summary, "he"))
        text = R.waiting_for_approval(req.agent_id, req.summary, lang)
        await self.say(text, kind="info", lang=lang, task_id=req.task_id)
        source = fast.source if fast else task.source if task else None
        if source == "voice":
            await self._speak(text, lang)

    async def _on_kill(self, evt: KillEvent) -> None:
        if not evt.engaged:
            return
        for req in self.broker.pending():
            if evt.agent_id is None or req.agent_id == evt.agent_id:
                self.broker.respond(req.id, False, by="kill switch")
        if evt.agent_id is None:
            for f in list(self._fast.values()):
                f.token.cancel(f"kill switch: {evt.reason}")

    # ================================================================== output
    async def say(self, text: str, *, kind: str = "answer", lang: str | None = None, role: str = "jarvis",
                  source: str = "system", task_id: str | None = None, intent: str | None = None) -> dict[str, Any]:
        """Post a chat message (saved to memory, pushed to the UI). Returns the ChatMessage view."""
        lang = lang or detect_language(text, "he")
        rec = ChatRecord(role=role, text=text, source=source, language=lang, kind=kind, task_id=task_id,  # type: ignore[arg-type]
                         intent=intent)
        if self.memory is not None:
            try:
                rec = await self.memory.add_chat(rec)
            except Exception:
                log.exception("Saving a chat message failed")
        view = rec.view()
        self._chat.append(view)
        await self._publish("chat.message", view)
        return view

    async def chat_history(self, limit: int = 100) -> list[dict[str, Any]]:
        if self.memory is not None:
            with contextlib.suppress(Exception):
                return [r.view() for r in await self.memory.chat_history(limit)]
        return list(self._chat)[-limit:]

    async def clear_chat(self) -> None:
        self._chat.clear()
        if self.memory is not None:
            self.memory.clear_visible_chat()

    async def _reply(self, turn: _Turn, text: str, *, kind: str = "answer", ok: bool | None = True,
                     task_id: str | None = None, speak: str | None = None, ack: str | None = None,
                     follow_up: bool = True, speak_text: bool = True) -> CommandResult:
        """Answer the turn: a chat message, the command's outcome (`ok`; None = known later) and the speech."""
        reply = await self.say(text, kind=kind, lang=turn.lang, task_id=task_id, intent=turn.intent.name)
        if ok is not None:
            await self._record(turn, ok=ok and kind != "error")
        spoken = None
        if speak_text:
            spoken = (speak if speak is not None else text).strip()
            if len(spoken) > SPOKEN_LIMIT:
                spoken = spoken[:SPOKEN_LIMIT].rsplit(" ", 1)[0] + "…"
        speech = {"text": spoken or None, "ack_key": ack, "language": turn.lang, "follow_up": follow_up}
        intent = {"name": turn.intent.name, "complexity": turn.intent.complexity,
                  "agents": [a.value for a in turn.intent.agents]}
        return CommandResult(True, turn.message, intent, task_id=task_id, reply=reply, speech=speech)

    async def _record(self, turn: _Turn, *, ok: bool | None = None, task_id: str | None = None) -> None:
        if self.memory is None:
            return
        with contextlib.suppress(Exception):
            await self.memory.update_command(turn.cmd_id, ok=ok, task_id=task_id, intent=turn.intent.name)

    async def _speak(self, text: str, lang: str) -> None:
        speaker = self._speaker
        if speaker is None or not text:
            return
        spoken = text if len(text) <= SPOKEN_LIMIT else text[:SPOKEN_LIMIT].rsplit(" ", 1)[0] + "…"
        try:
            res = speaker(spoken, lang)
            if inspect.isawaitable(res):
                await res
        except Exception:
            log.warning("Speaking failed", exc_info=True)

    async def _pipeline(self, step: str, status: str, lang: str, detail: str = "") -> None:
        he, en = _PIPELINE_LABELS.get(step, (step, step))
        if detail and step not in ("understanding", "done"):
            label = t(lang, he, en).rstrip("…") + f": {detail}"     # "מבצע: יוצר תיקייה…", not "מבצע…: …"
        else:
            label = t(lang, he, en)
        await self._publish("voice.pipeline", {"step": step, "status": status, "label": label})

    async def _action(self, action_id: str, title: str, icon: str, status: str, *, progress: float | None = None,
                      detail: str | None = None) -> None:
        await self._publish("action", {"id": action_id, "title": title, "detail": detail, "icon": icon,
                                       "progress": progress, "status": status, "ts": iso(utcnow())})

    async def _publish(self, topic: str, data: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            await self.bus.publish(event(topic, data, sender="jarvis"))

    async def _log(self, message: str, *, level: str = "info", kind: str = "core", agent_id: str | None = None,
                   task_id: str | None = None) -> None:
        if self.activity is None:
            return
        with contextlib.suppress(Exception):
            await self.activity.log(message, source="JARVIS", level=level, kind=kind,  # type: ignore[arg-type]
                                    agent_id=agent_id, task_id=task_id)

    def _reply_language(self, detected: str | None) -> str:
        fixed = self.settings.general.language
        if fixed in ("he", "en"):
            return fixed
        return detected if detected in ("he", "en") else "he"

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)


__all__ = ["CHAT_SYSTEM", "ChatTurn", "CommandResult", "JarvisCore"]
