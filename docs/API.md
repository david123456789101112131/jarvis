# JARVIS local API (UI ⇄ core contract)

The core runs a FastAPI server on `127.0.0.1:<port>` (default 8765, next free port if busy).
The same server hosts the built UI (`/`), so the desktop window just loads
`http://127.0.0.1:<port>/`.

## Authentication

- A random token is created at every start and written only to `runtime.json` in the user's
  private data folder (which the agents' tools cannot read). The CLI uses it to find the
  running instance (`jarvis open`, `jarvis say`, a second launch brings the window forward).
- The page is served only to a session: the desktop window (and `jarvis open`) loads
  `/?launch=<token>`, which sets an HttpOnly, SameSite=Strict session cookie and redirects to
  `/` (or `/?view=orb`). The page then gets `window.__JARVIS__ = { token, version, wsUrl }`
  in a nonce'd script; a Content-Security-Policy lets no other script run.
- REST: header `X-JARVIS-Token: <token>` (or the session cookie).
  WebSocket: `ws://127.0.0.1:<port>/ws?token=<token>` (or the session cookie).
- Requests without the token, with a non-local `Host` (DNS rebinding), or with a foreign
  `Origin` get 403 (a WebSocket is closed with 4403). The server listens on 127.0.0.1 only.
- Agents' web tools may not call the server's port.
- Secrets are never returned by any endpoint or event (only "configured: true/false"); every
  bus message, and so every UI event, passes the redactor.

All bodies are JSON (UTF-8). Times are ISO-8601 UTC strings; `time` fields are local
`HH:MM:SS` strings ready to display. Errors: `{"error": {"code": str, "message": str}}` with a
4xx/5xx status.

## Data shapes

```ts
type AgentId = "research" | "system" | "development" | "creative" | "planner";
type AgentStatus = "offline" | "idle" | "thinking" | "working" | "waiting" | "completed" | "error";
type TaskStatus = "pending" | "planning" | "queued" | "running" | "waiting" | "verifying"
                | "completed" | "failed" | "cancelled" | "interrupted" | "skipped";
type Mode = "chat" | "normal" | "task" | "autonomous" | "debug";
type Priority = "critical" | "high" | "normal" | "low";
type VoiceState = "disabled" | "idle" | "wake" | "listening" | "processing" | "speaking" | "error";
type Theme = "blue" | "green" | "orange" | "red" | "purple" | "white";

interface ActivityEntry { id: string; ts: string; time: string; source: string; message: string;
  level: "debug" | "info" | "success" | "warning" | "error"; task_id?: string | null;
  agent_id?: string | null; kind: string; }

interface AgentView {
  id: AgentId; number: 1 | 2 | 3 | 4 | 5;
  name: string;            // "Research Agent"
  name_he: string;         // "סוכן מחקר"
  role: string;            // one-line description (English)
  role_he: string;
  status: AgentStatus;
  enabled: boolean;        // false after "stop agent" until started again
  killed: boolean;         // per-agent kill switch engaged
  current: { task_id: string; subtask_id: string | null; title: string } | null;
  progress: number;        // 0..1 of the current subtask
  last_action: string | null; last_action_at: string | null;
  errors: { at: string; message: string }[];   // newest first, max 10
  permissions: string[];   // capability names, e.g. "fs.read"
  queue: number;           // waiting assignments
  stats: { completed: number; failed: number; avg_duration_s: number | null };
  skills: string[];
}

interface SubTaskView {
  id: string; agent: AgentId; title: string; instruction: string; status: TaskStatus;
  progress: number; attempts: number; depends_on: string[]; optional: boolean;
  started_at: string | null; finished_at: string | null;
  summary: string | null; error: string | null; files_created: string[];
}

interface TaskSummary {
  id: string; title: string; goal: string; status: TaskStatus; progress: number;
  priority: Priority; mode: Mode; agents: AgentId[]; source: "text" | "voice" | "api" | "system";
  created_at: string; updated_at: string; started_at: string | null; finished_at: string | null;
  summary: string | null; error: string | null;
}

interface TaskDetail extends TaskSummary {
  plan: { strategy: string; subtasks: SubTaskView[] } | null;
  timeline: { ts: string; time: string; kind: string; message: string; agent_id?: string | null;
              subtask_id?: string | null; level?: string }[];
  result: { summary: string; details: string | null; files_created: string[]; actions_taken: string[];
            issues: string[]; next_step: string | null } | null;
}

interface ChatMessage {
  id: string; role: "user" | "jarvis" | "system"; text: string; ts: string; time: string;
  source: "text" | "voice" | "api" | "system"; language: "he" | "en";
  kind: "answer" | "ack" | "plan" | "result" | "error" | "info";
  task_id?: string | null; intent?: string | null;
}

interface SystemStats {
  ts: string;
  cpu: { percent: number; per_core: number[]; cores: number; freq_mhz: number | null; name: string };
  memory: { percent: number; used_gb: number; total_gb: number };
  disks: { mount: string; percent: number; used_gb: number; total_gb: number }[];
  network: { up_kbps: number; down_kbps: number; sent_mb: number; recv_mb: number };
  battery: { percent: number; plugged: boolean } | null;
  uptime_s: number; processes: number; os: string; hostname: string;
}

interface ConfirmationRequest {
  id: string; agent_id: AgentId; task_id: string | null; tool: string; summary: string;
  reasons: string[]; risk: "safe" | "low" | "medium" | "high" | "critical";
  details: Record<string, unknown>; created_at: string; expires_at: string | null;
  status: "pending" | "approved" | "denied" | "expired" | "cancelled";
}

interface VoiceInfo {
  state: VoiceState; enabled: boolean; muted: boolean; wake_word: string;
  engines: { wake: string; stt: string; tts: string };      // "openwakeword", "faster_whisper", "edge", "none", ...
  devices: { input: string | null; output: string | null };
  last_transcript: string | null; error: string | null;
}

interface UISettings { theme_color: Theme; orb_size: number; orb_opacity: number; follow_mouse: boolean;
  always_active: boolean; mini_orb: boolean; }

interface StateSnapshot {
  version: string; assistant_name: string; user_name: string; language: "auto" | "he" | "en";
  mode: Mode; voice: VoiceInfo; ui: UISettings;
  agents: AgentView[];
  tasks: { active: TaskSummary[]; recent: TaskSummary[] };
  chat: ChatMessage[];                 // last 100
  activity: ActivityEntry[];           // last 200
  confirmations: ConfirmationRequest[];// pending
  kill_switch: { global: boolean; agents: Record<string, string> };
  system: SystemStats | null;
  providers: { active: string | null; anthropic: boolean; openai: boolean; local: boolean; fallbacks: boolean };
  files: { workspace: string; projects: string };
}
```

## REST endpoints

| Method & path | Body | Returns |
|---|---|---|
| GET `/api/health` | – | `{ok: true, version}` (no token needed) |
| GET `/api/state` | – | `StateSnapshot` |
| POST `/api/command` | `{text, source?: "text"\|"voice"}` | `{accepted, message: ChatMessage, intent: {name, complexity, agents}, task_id?: string, reply?: ChatMessage}` |
| GET `/api/chat?limit=100` | – | `ChatMessage[]` |
| DELETE `/api/chat` | – | `{ok}` (clears the visible conversation, keeps memory) |
| GET `/api/agents` | – | `AgentView[]` |
| GET `/api/agents/{id}` | – | `AgentView & {activity: ActivityEntry[]}` |
| POST `/api/agents/{id}/action` | `{action: "start"\|"stop"\|"restart"\|"kill"\|"release"}` | `AgentView` |
| POST `/api/agents/{id}/task` | `{instruction, priority?}` | `{task_id}` |
| GET `/api/tasks?status=&limit=50` | – | `TaskSummary[]` (newest first) |
| GET `/api/tasks/{id}` | – | `TaskDetail` |
| POST `/api/tasks/{id}/cancel` | – | `TaskSummary` |
| POST `/api/tasks/{id}/resume` | – | `TaskSummary` |
| POST `/api/tasks/{id}/retry` | – | `TaskSummary` (new attempt of failed subtasks) |
| GET `/api/system` | – | `SystemStats` |
| GET `/api/system/processes?limit=15` | – | `{name, pid, cpu, memory_mb}[]` |
| GET `/api/activity?limit=200&task_id=&agent_id=` | – | `ActivityEntry[]` |
| GET `/api/audit?limit=200` | – | audit entries `{id, ts, agent_id, task_id, tool, capability, action, risk, decision, outcome, reason, error, duration_ms}` |
| GET `/api/permissions` | – | `{agents: {[id]: {capabilities: string[], grants: Grant[], restricted_to_task, folder_scope, risk_ceiling}}, capabilities: string[]}` |
| POST `/api/permissions/grant` | `{agent_id, capability, roots?: string[], task_id?, ttl_s?, max_uses?, reason?}` | `Grant` |
| DELETE `/api/permissions/grants/{grant_id}` | – | `{ok}` |
| POST `/api/permissions/agents/{id}/reset` | – | `{ok}` |
| POST `/api/permissions/agents/{id}/restrict` | `{task_id?: string\|null, folders?: string[]\|null}` | `{ok}` |
| GET `/api/confirmations` | – | `ConfirmationRequest[]` |
| POST `/api/confirmations/{id}` | `{approved: boolean, remember?: boolean}` | `{ok}` |
| POST `/api/killswitch` | `{engage: boolean, agent_id?: AgentId, reason?: string}` | `{global, agents}` |
| POST `/api/mode` | `{mode}` | `{mode}` |
| GET `/api/memory/facts?q=` | – | `{id, text, category, created_at}[]` |
| POST `/api/memory/facts` | `{text, category?}` | fact |
| DELETE `/api/memory/facts/{id}` | – | `{ok}` |
| GET `/api/memory/search?q=&limit=20` | – | `{layer, id, text, score, ts, ref?}[]` |
| GET `/api/memory/projects` | – | `{id, name, path, description, updated_at, status}[]` |
| GET `/api/files/recent?limit=30` | – | `{path, name, size, task_id, agent_id, created_at}[]` |
| GET `/api/files/list?path=` | – | `{path, entries: {name, path, is_dir, size, modified}[]}` (workspace/projects only) |
| POST `/api/files/open` | `{path, reveal?: boolean}` | `{ok}` |
| GET `/api/settings` | – | settings object without secrets |
| PATCH `/api/settings` | partial settings, e.g. `{ui: {theme_color: "green"}}` | settings |
| GET `/api/secrets` | – | `{[name]: {label, configured, source}}` |
| PUT `/api/secrets/{name}` | `{value}` | `{configured: true, source}` |
| DELETE `/api/secrets/{name}` | – | `{configured: false}` |
| POST `/api/voice/{action}` | action ∈ `start, stop, mute, unmute, ptt_start, ptt_stop, cancel` | `VoiceInfo` |
| POST `/api/voice/speak` | `{text}` | `{ok}` |
| GET `/api/quick-tools?lang=he\|en` | – | `{id, label, label_he, icon, command}[]` (the radial menu entries) |
| POST `/api/window/{action}` | action ∈ `show, hide, toggle, mini, full, quit` | `{ok}` (409 when there is no desktop window) |

## WebSocket `/ws`

Every server message: `{ "type": string, "data": any, "ts": string }`.

| type | data | when |
|---|---|---|
| `hello` | `{version}` | on connect |
| `state` | `StateSnapshot` | on connect and after reconnect |
| `chat.message` | `ChatMessage` | user input accepted, JARVIS replies, acks, results |
| `agent.status` | `AgentView` | any agent field changed |
| `task.updated` | `TaskSummary` | created / status / progress change |
| `task.subtask` | `{task_id, subtask: SubTaskView}` | subtask change |
| `task.timeline` | `{task_id, entry}` | new timeline entry |
| `activity` | `ActivityEntry` | every activity line |
| `audit` | audit entry | every tool decision |
| `system.stats` | `SystemStats` | every 2 s |
| `voice.state` | `{state, muted, error, trigger}` | voice state change |
| `voice.level` | `{level}` (mic or speech RMS 0..1) | up to 15×/s while listening/speaking (sent straight to the UI, not via the bus) |
| `voice.transcript` | `{text, final: boolean, language}` | live (partial) and final transcripts |
| `voice.pipeline` | `{step: "transcribing"\|"understanding"\|"routing"\|"selecting_tool"\|"preparing_action"\|"executing"\|"done", status: "active"\|"done"\|"error", label}` | processing steps for the orb panel |
| `action` | `{id, title, detail?, icon?: string, progress?: number, status: "running"\|"done"\|"error"}` | current fast-path action card ("Opening Google Chrome…") |
| `speech` | `{text, language}` | what JARVIS is saying (speech bubble) |
| `confirm.request` | `ConfirmationRequest` | approval needed |
| `confirm.response` | `ConfirmationRequest` | decided / expired |
| `mode` | `{mode}` | mode changed |
| `killswitch` | `{global, agents}` | engaged / released |
| `notification` | `{id, level, title, message}` | toast |
| `settings` | `{ui: UISettings}` | UI settings changed |
| `ui.navigate` | `{screen: "agents"\|"tasks", task_id?}` | a voice/text command asked to show a screen ("show the agents") |
| `pong` | `{}` | answer to `{type: "ping"}` |

Client → server over WS (optional; REST works too): `{type: "command", text}`,
`{type: "confirm", id, approved, remember?}`, `{type: "ping"}` (server answers `pong`).

A client that falls more than 2000 events behind is closed with code 4408; it reconnects and
starts again from a fresh `state` snapshot.
