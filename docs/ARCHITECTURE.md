# JARVIS — Architecture

JARVIS is a Windows desktop AI assistant: one central manager ("JARVIS") that understands
voice and text commands, plans work, splits it between exactly five specialist agents, runs
them in parallel through a controlled tool layer, verifies what they did, and reports one
clear answer.

This document is the contract between the modules. Code that disagrees with it is a bug in
one of the two.

---

## 1. Technology choice

Requirements that drove the choice: fast, stable, easy to run and maintain, extensible,
strong voice support, deep OS access, modern UI.

| Option | Strengths | Weaknesses | Verdict |
|---|---|---|---|
| **Python core + web UI in WebView2** | Best local voice stack (openWakeWord, faster-whisper with Hebrew, edge-tts Hebrew neural voices, SAPI offline); official Anthropic/OpenAI SDKs; full OS access (psutil, PowerShell, pywin32, ctypes); web tech gives the HUD look with little code; UI decoupled over WebSocket | Python startup ~1 s; needs packaging for end users | **Chosen** |
| Electron + Node.js | Modern UI, one language | ~150 MB+ RAM idle, weak voice/ML ecosystem in Node, OS automation needs native modules | Rejected |
| Tauri (Rust) + web UI | Small, fast shell | Rust slows iteration; AI/voice libraries thin; would still need a Python sidecar | Rejected |
| C# / .NET (WPF, WinUI 3) | Best native Windows integration, System.Speech | AI-agent ecosystem weaker; HUD-style animated UI is much more work; harder to test cross-platform | Rejected |
| Python + Qt (PySide6/QML) | Single language, native widgets | Glass/neon/particle animations are heavier to build and maintain than with web tech; QML is niche | Rejected |

**Stack**

- **Python 3.11+ / asyncio** — core, orchestration, agents, tools, memory, voice.
- **FastAPI + uvicorn** — local API bound to `127.0.0.1`, protected by a per-run token;
  REST for queries/actions, one WebSocket for live events.
- **React + TypeScript + Vite** (`ui/`) with **Three.js** for the JARVIS core orb; the
  build output is committed to `src/jarvis/ui/static/` so running from source needs no Node.
- **pywebview** (Edge WebView2, built into Windows 10/11) hosts the UI in a native window;
  fallback is an Edge `--app` window, then the default browser.
- **SQLite** (with FTS5) behind a `MemoryBackend` interface.
- **PyInstaller** (one-folder) + **Inno Setup** for the installer.

## 2. Layering

```
UI (React HUD in WebView2)  ⇄  Server (FastAPI REST + WebSocket, token-protected, 127.0.0.1)
                                   │
Voice (wake word → STT → intent → TTS) ──► JARVIS Core (modes, intents, commands, context)
                                   │
                           Task Orchestrator (plan → schedule DAG → retry/fallback → verify → synthesize)
                                   │
                            Agent Manager (lifecycle, inboxes, kill switch, status)
                                   │
        Research · System · Development · Creative · Planner   (exactly five agents)
                                   │
                    Tool Layer (ToolExecutor: kill switch → permission → risk → confirmation → audit)
                                   │
                Host adapter (Windows / POSIX) · Files · Browser/Web · APIs · Local services

Cross-cutting: Communication Bus · Memory · Permissions · Security · Logging · Configuration · AI providers
```

Rules:
1. Agents never touch the OS directly. Every side effect goes through a `Tool` invoked via
   the `ToolExecutor`, so every action is permission-checked, risk-assessed, confirmed when
   needed, cancellable and audited.
2. Agents never talk to each other directly. JARVIS (orchestrator) assigns work and passes
   results along as context. All assignments/results travel as `Message`s on the bus.
3. The core never imports UI or server code. The server subscribes to the bus.
4. Secrets are only readable by AI providers and explicit secret-aware tools; they are
   redacted from logs, bus messages, UI payloads and memory.

## 3. Package map (`src/jarvis/`)

| Package | Responsibility |
|---|---|
| `common/` | Enums (`Priority`, `AgentStatus`, `TaskStatus`, `Mode`, `RiskLevel`, `AgentId`, `MessageType`), ids, time, errors, language helpers. No internal deps. |
| `config/` | Pydantic settings, TOML loading (defaults → user file → env `JARVIS__SECTION__KEY`), paths (platformdirs). |
| `bus/` | `Message` model and `MessageBus` (point-to-point priority mailboxes + topic pub/sub + request/reply + recorder hook). |
| `logs/` | Logging setup (rotating JSON file + console), redaction filter, `ActivityLog` (human readable timeline), `AuditLog`. |
| `security/` | `SecretStore` (keyring/env/.env), `Redactor`, `KillSwitch`, `PathGuard`, process sandbox helpers. |
| `permissions/` | Capabilities, grants, `PermissionManager`, `RiskAssessor`, `ConfirmationBroker`, default policies. |
| `ai/` | Provider-neutral types, `AIProvider` interface, Anthropic / OpenAI / OpenAI-compatible local providers, `ProviderRouter` (role → provider/model/effort, fallback chain). |
| `memory/` | `MemoryBackend` interface, SQLite backend, Short-Term / Long-Term / Project / Task memory, command history, `MemoryManager` facade. |
| `tools/` | `Tool` base, registry, executor, and tools: filesystem, process, shell, system info, web, code, git, http API, creative assets, Windows settings, memory. |
| `host/` | OS adapter interface + Windows and POSIX implementations, installed-app index with Hebrew/English aliases. |
| `agents/` | `BaseAgent`, `AgentManager`, `AgentResult`, and the five agents with deterministic skills plus an LLM tool loop. |
| `orchestration/` | `TaskPlan`/`SubTask`, rule-based and LLM planning, scheduler, retry/loop guard, cancellation, verification, synthesis. |
| `core/` | `JarvisCore`: modes, intent detection (bilingual), command router, responses, session context. |
| `voice/` | Audio I/O, wake word engines, VAD/endpointing, STT engines, TTS engines with phrase cache, `VoicePipeline`, `VoiceService`. |
| `computer/` | Computer control: a local vision model (Holo3.1, via llama.cpp) proposes actions; the backend (Win32 screen/input), the safety policy (judged from what Windows reports under the pointer), the look→think→act session, the model server, and `jarvis computer install`. Behind `use_computer`/`computer_act` in the tool layer. |
| `server/` | FastAPI app, WebSocket hub, REST routes, static UI hosting. |
| `ui/` | Desktop shell (pywebview windows) + built static web UI. |
| `app.py` | Composition root: builds and wires every service; start/stop lifecycle. |

## 4. Core data contracts

### 4.1 Bus `Message`
```
id, task_id, agent_id, sender, recipient, type, priority, instruction, context,
permissions, timestamp, status, result, correlation_id, parent_id, topic
```
`type` ∈ `task.assign | task.progress | task.result | task.cancel | agent.status |
agent.command | confirm.request | confirm.response | log | event`.
Point-to-point messages (with `recipient`) are queued in the recipient's priority mailbox;
every message is also published on its topic for observers (UI, recorder, debug view).

### 4.2 `AgentResult` (what every agent returns)
```
status (success | partial | failed | cancelled), progress (0..1), result (any JSON),
summary, errors[], files_created[], actions_taken[], next_step, data{}
```

### 4.3 `TaskPlan` / `SubTask`
A plan is a DAG of subtasks. Each subtask: `id, agent, title, instruction, depends_on[],
priority, skill?, params{}, acceptance_criteria[], expected_files[], verify[], timeout_s,
max_retries, optional` plus runtime state (`status, attempts, result, error, timestamps`).
Plans are persisted after every state change, which is what makes resume possible.

### 4.4 Priorities and statuses
- Priority: `CRITICAL > HIGH > NORMAL > LOW`. Mailboxes and the scheduler order by
  priority, then FIFO. One execution slot per agent is reserved for CRITICAL work.
- Agent status: `Offline, Idle, Thinking, Working, Waiting, Completed, Error`.
- Task status: `pending, planning, queued, running, waiting, verifying, completed, failed,
  cancelled, interrupted, skipped`.

## 5. Command flow

1. **Input** (text from UI/CLI, or voice transcript) → `JarvisCore.handle_input`.
2. **Control commands** (stop task, stop all agents, kill switch, mode switch, show agents,
   agent management) execute immediately.
3. **Intent detection** (bilingual Hebrew/English rules, no LLM) yields
   `Intent(name, slots, confidence, agents, complexity)`.
4. **Fast path** — simple intents (open/close app, system status, create folder, volume,
   lock, open website, time) run one agent skill directly through the Agent Manager. No LLM
   call, typically well under a second.
5. **Complex path** — JARVIS acknowledges at once ("קיבלתי. אני מחלק את המשימה בין
   הסוכנים.") and hands the goal to the orchestrator, which plans (Planner agent: LLM or
   rule-based templates), schedules subtasks in parallel by dependency and priority,
   retries/falls back on failure, verifies results, sends follow-ups, and synthesizes one
   answer (LLM, or a structured template offline).
6. **Unknown/conversational** — LLM chat with conversation + memory context when a provider
   is configured; otherwise a helpful offline answer.

## 6. Modes

| Mode | Behaviour | Confirmation threshold |
|---|---|---|
| CHAT | Conversation only, no tool actions | n/a (actions refused) |
| NORMAL | Fast path + orchestration | risk ≥ HIGH |
| TASK | Every request is planned and orchestrated (plan shown) | risk ≥ HIGH |
| AUTONOMOUS | Long chains, automatic follow-ups, larger budgets | risk ≥ HIGH (HIGH may be pre-approved; CRITICAL always asks) |
| DEBUG | NORMAL + verbose routing/plan/tool traces in chat and logs | risk ≥ MEDIUM |

## 7. Security model

- **Capabilities** (`fs.read`, `fs.write`, `fs.delete`, `process.list`, `process.launch`,
  `process.kill`, `shell.execute`, `system.info`, `system.settings`, `system.power`,
  `net.search`, `net.fetch`, `net.api`, `code.execute`, `code.install`, `git`,
  `memory.read`, `memory.write`, `ai.generate`, `notify`, `clipboard`, `screen.read`,
  `input.control`).
- **Grants** per agent with optional scope: folder roots, task id, expiry, max uses.
  JARVIS (or the user) can grant, revoke, and narrow grants at runtime.
- **Path guard**: writes allowed only under configured roots (workspace and projects by
  default); system folders and credential stores are always denied; traversal and
  symlink escapes are resolved before checking.
- **Risk assessment** per call (e.g. recursive or broad deletion, installers, registry,
  power actions, encoded PowerShell, data leaving the machine).
- **Confirmation broker**: actions at/above the mode threshold wait for explicit approval in
  the UI (or by voice). No answer before the timeout means **deny**.
- **Kill switches**: per agent and global (UI button, voice "עצור הכול", API). Engaging
  cancels running work, kills child processes, and blocks the tool layer until released.
- **Computer control**: the local vision model only *proposes* an action; each one is a
  `computer_act` call through the whole gate above, and its risk is judged from what Windows
  itself reports under the pointer (UI Automation + window), never from the model's words.
  Own windows, password fields, system/credential windows and `Ctrl+Alt+Del` are refused;
  a session also stops the instant the user presses Esc twice or touches the mouse/keyboard.
- **Secrets**: OS keyring (Windows Credential Manager) or environment; never sent to the UI,
  never logged (redaction filter on every log record, bus payload and UI event); child
  processes get an environment with secret variables stripped; outgoing HTTP payloads are
  scanned for secrets.
- **Local server**: binds 127.0.0.1 only, requires the per-run token, checks Host/Origin.

## 8. Reliability

- Retry with exponential backoff, per-subtask retry limit, error fingerprinting (the same
  error three times stops retrying), global dispatch budget per task (no infinite loops).
- Timeouts per subtask and per task; cooperative cancellation tokens checked by agents and
  tools; child processes killed on cancel.
- Fallbacks: a research helper subtask feeds failing development work; alternate agent for
  generic file/command work; optional subtasks can fail without failing the task.
- Self-verification: files exist and are non-empty, commands exit 0, tests pass, HTML
  references resolve, acceptance criteria checked (LLM judge when available).
- Checkpointing after every subtask; interrupted tasks are resumable without redoing
  completed subtasks.

## 9. Voice pipeline (latency budget)

```
mic 16 kHz frames (80 ms) → wake word (on-device, continuous) → listen + VAD endpointing
→ STT (faster-whisper, local; or cloud) → intent (rules, <5 ms) → action
→ TTS (cached phrases play instantly; neural voice for dynamic text)
```
Design rules: the wake-word engine runs continuously in its own thread; acknowledgements
are pre-synthesized and cached at startup; complex work is acknowledged by voice before it
starts; barge-in (saying the wake word) interrupts speech.

## 10. UI

One window, dark neon-blue HUD. Screens: **Command Center** (JARVIS core orb with voice
state, live transcript, pipeline steps, action card; agents panel; task execution; system
monitor; activity log; chat; files & system quick actions), **Agents** dashboard (name,
status, current task, progress, last action, errors, permissions, activity, controls),
**Tasks** (history with full timeline replay, resume/cancel), **Memory**, **Security**
(grants, pending confirmations, audit, kill switches) and **Settings** (providers and keys
status, voice, orb customization). A compact always-on-top orb window mirrors the reference
design's minimal states and offers the quick-tools ring.

At sign-in Windows starts JARVIS with `--background`: only the orb is on screen and the wake word
is live, so JARVIS is used hands-free. `jarvis.host.autostart` keeps the per-user Run entry in step
with `general.start_with_windows`; only the windowed app writes it. "Show yourself" / "hide
yourself" (and any voice command that opens a screen) bring the main window up or send it back.

## 11. Extending

- New tool: subclass `Tool`, declare capability, params model and risk; register it.
- New provider: implement `AIProvider`; add it to the router's registry.
- New memory backend: implement `MemoryBackend`.
- New skill for an agent: decorate an async method with `@skill(...)`.
- The five-agent roster is fixed by design; new abilities are added as skills and tools.
