# JARVIS local API (UI ⇄ core contract)

The core runs a FastAPI server on `127.0.0.1:<port>` (default 8765, next free port if busy).
The same server hosts the built UI (`/`), so the desktop window just loads
`http://127.0.0.1:<port>/`.

## Authentication

- A random token is created at every start and written only to `runtime.json` in the user's
  private data folder (which the agents' tools cannot read). The CLI uses it to find the
  running instance (`jarvis open`, `jarvis say`, a second launch brings the window forward).
- The page is served only to a session: the desktop window (and `jarvis open`) loads
  `/?launch=<token>`, which sets an HttpOnly, SameSite=Strict session cookie (for this run of
  JARVIS; at most 32 windows) and redirects to `/` (or `/?view=orb`), so the token leaves the
  address bar. The page then gets `window.__JARVIS__ = { version, wsUrl, apiBase }` in a
  nonce'd script: no credentials, because its REST calls and its WebSocket are authorised by the
  cookie, which scripts cannot read. A Content-Security-Policy lets no other script run.
- Other clients (the CLI, the tests): REST header `X-JARVIS-Token: <token>`;
  WebSocket `ws://127.0.0.1:<port>/ws?token=<token>`.
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

interface Job {                        // one action of a work screen (code, images, video, apps)
  id: string;
  kind: string;                        // "dev.propose", "image.create", "apps.look", …
  area: "code" | "images" | "video" | "apps";
  title: string;                       // what JARVIS understood it was asked to do
  plan: string[];                      // what it is about to do
  real: boolean;                       // false: a demonstration; nothing was sent, created or changed
  status: "running" | "done" | "failed" | "cancelled";
  steps: { key: string; label: string; status: "pending" | "running" | "done" | "failed" | "skipped";
           detail: string; ms: number | null }[];
  progress: number | null;             // 0..1, null while it cannot be measured
  message: string;                     // the latest progress line
  result: Record<string, unknown> | null; error: string | null;
  sends: string[];                     // what leaves the computer, and where to
  cost: string | null;                 // the estimated price, when it costs money
  created_at: string; finished_at: string | null; ms: number | null;
  can_retry: boolean; retry_of: string | null;
  params: Record<string, unknown>;     // what it was started with (for "try again")
  lang: "he" | "en";
  keep_result: boolean;                // false: the history file keeps only the result's summary
}

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
  providers: { active: string | null; anthropic: boolean; xai: boolean; openai: boolean; local: boolean;
               fallbacks: boolean; agents: Record<AgentId, string | null> };
  files: { workspace: string; projects: string };
  jobs: Job[];                         // the work screens' latest 50, newest first
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
| POST `/api/window/{action}` | action ∈ `show, hide, toggle, mini, full, reveal, quit` (`reveal`: show the main window only if it is hidden) | `{ok}` (409 when there is no desktop window) |

## Work screens (code, images, video, apps, privacy)

The code, image, video and apps screens run their actions as jobs. Every POST that starts one answers at
once with its `Job` (status `running`); its steps, progress and result then arrive as `job` events on `/ws`.
The tools a job uses go through the tool executor like every agent action, so the permission manager, the
approval dialog, the kill switches and the audit log all apply; a declined approval ends the job with a
readable error and nothing done. Bodies take an optional `lang: "he"|"en"` for the language of the job's
messages. A refusal the user can act on ("open the project again", "turn on changes", "connect xAI") is
409 `{"error": {code, message}}` in that language, with secrets redacted.

### Jobs, file dialogs and speed numbers

| Method & path | Body | Returns |
|---|---|---|
| GET `/api/jobs?area=&kind=&limit=50` | – | `Job[]` (newest first, at most 200) |
| GET `/api/jobs/{id}` | – | `Job` (404 if unknown) |
| POST `/api/jobs/{id}/cancel` | – | `Job` (it stops at its next check and ends `cancelled`) |
| POST `/api/jobs/{id}/retry` | – | the new `Job` (`retry_of` = id); 409 while it runs or after a restart |
| DELETE `/api/jobs` | – | `{cleared}` (finished jobs; running ones stay) |
| POST `/api/dialog` | `{kind: "open"\|"folder"\|"save", title?, directory?, save_name?, file_types?, multiple?}` | `{paths: string[]}` (empty when the user cancels); 409 when JARVIS runs without its window (the screens then take a typed path) |
| GET `/api/metrics?prefix=` | – | `{summary: {[name]: {count, ok, median_ms, p90_ms, min_ms, max_ms, last_ms, last_at}}, snapshots: {[label]: {at, summary}}}` |
| POST `/api/metrics/snapshot` | `{label}` (1–40 chars) | `{at, summary}` (kept under that label, e.g. "before") |
| DELETE `/api/metrics` | – | `{ok}` |

A file reaches JARVIS only when the user picks it in `/api/dialog` or types its path; nothing is read
before that.

### Code & development (`/api/dev/*`)

A project is opened from a folder the user picked, which gives the Development agent read access to that
folder for the session. A change is first proposed (nothing written), shown as a diff per file, can be edited
by hand or have files left out, and is written only on apply, after "allow changes" (`write`) and the
approval dialog. Every applied change can be undone.

| Method & path | Body | Returns |
|---|---|---|
| GET `/api/dev/status` | – | `{available, engines, needs, prices_checked, mode, sessions, tools: {python, node, git}, limits: {files, chars}}` |
| POST `/api/dev/open` | `{path}` | `{session, job}` (job `dev.inspect`: languages, frameworks, dependencies, run and test commands; no AI) |
| POST `/api/dev/sessions/{id}/write` | `{allow: boolean}` | session |
| DELETE `/api/dev/sessions/{id}` | – | `{closed}` (its folder access ends) |
| POST `/api/dev/sessions/{id}/inspect` | `{request?, error_text?, refresh?}` | `Job` (`dev.inspect`, or `dev.files` for the files a request or an error is about) |
| POST `/api/dev/sessions/{id}/propose` | `{request (2–4000 chars), files? (≤24), mode?: "auto"\|"fast"\|"strong", error_text?, parent_id?, engine?}` | `Job` (`dev.propose`: a short plan, assumptions and the change; nothing is written) |
| GET `/api/dev/sessions/{id}/changes` | – | `{id, request, summary, status, created_at, files, can_undo, model, cost_usd}[]` |
| GET `/api/dev/changes/{id}` | – | the change with `files: {path, action, why, include, edited, status, added, removed, diff, content, editable, masked, blocked, problems, warnings, eol, new_file}[]`, `digest`, `undo_digest`, `can_apply`, `writable` (404 if unknown; secret-looking values masked) |
| PUT `/api/dev/changes/{id}/files` | `{path, content?, include?}` | the change (one file edited by hand, or left in or out) |
| POST `/api/dev/changes/{id}/apply` | – | `Job` (`dev.apply`) |
| POST `/api/dev/changes/{id}/undo` | – | `Job` (`dev.undo`: the files as they were before the apply) |
| POST `/api/dev/changes/{id}/discard` | – | the change (dropped, nothing written) |
| POST `/api/dev/sessions/{id}/tests` | `{command?}` | `Job` (`dev.tests`: the project's own test command; the result says what ran, what passed and what failed, from the real output) |
| POST `/api/dev/sessions/{id}/fix` | `{tests_job_id?, error_text?, request?, mode?, engine?}` | `Job` (`dev.fix`: a proposed fix, reviewed like any change) |

`mode`: `fast` is quick thinking (lower effort: faster and cheaper), `strong` deep thinking, and `auto`
decides by the size of what is sent and the words of the request; both use the same model. `engine` is the
AI the user pressed (the local model; a cloud AI only with `ai.allow_paid` on, and then only when told, see
`ai.cloud_only_when_told`).

### Image and video creation (`/api/media/*`)

Real requests are made on this computer by the local engine (`media.engine = "local"`, the default), or go to
Grok Imagine (xAI) through the tool executor (approval rules apply) when `media.engine = "xai"` and
`ai.allow_paid` is on. With `demo: true` nothing is made and the result is a placeholder marked as a
demonstration (`real: false`).

| Method & path | Body | Returns |
|---|---|---|
| GET `/api/media/status?lang=he\|en` | – | `MediaStatus` |
| GET `/api/media/items?kind=image\|video` | – | `MediaItem[]` (newest first, up to 200) |
| GET `/api/media/file/{id}` | – | the preview's bytes (its MIME, `nosniff`, a sandboxing CSP); 404 for an unknown id or a file outside `<workspace>/media` |
| DELETE `/api/media/items/{id}` | – | `{deleted: true}` (404 if unknown); removes JARVIS's preview only, never a saved copy |
| DELETE `/api/media/items?kind=` | – | `{cleared: number}` |
| POST `/api/media/images` | `{description, style?, model?, count? (1–4), aspect_ratio?, resolution? ("1k"\|"2k"), source_item?, source_path?, demo?}` | `Job` (`image.create`, area `images`) |
| POST `/api/media/images/refine` | `{item_id, change?, variation?, count?}` | `Job` (`image.refine`) |
| POST `/api/media/videos` | `{scene, characters?, motion?, lighting?, style?, model?, seconds? (1–15), aspect_ratio?, resolution?, image_item?, image_path?, reference_items?, reference_paths?, demo?}` | `Job` (`video.create`, area `video`) |
| POST `/api/media/videos/refine` | `{item_id, change}` | `Job` (`video.refine`) |
| POST `/api/media/save` | `{item_id, target (full path), overwrite?}` | `Job` (`image.save` / `video.save`) |

Reference images for a video: at most 3, Full model only, up to 720p, not together with a first frame.
Refusals `{"error": {code, message, details?}}`: 409 `unavailable` · 409 `demo` · 409 `exists`
(details `{path, renamed}`) · 404 `not_found` · 400 `media_request` · 422 `invalid`.

- `MediaStatus`: `{available, xai, network, demo, service, host, key_name, missing[], reason, offer, media_dir,
  counts: {image, video}}`.
- `MediaItem`: `{id, kind, job_id, path, format: "PNG"|"JPEG"|"WEBP"|"MP4"|"SVG", mime, bytes, width, height,
  seconds, real, description, prompt, params, usd, cost, parent, saved_to[], created_at, name, url}`.
- Job results: create and refine `{summary, items, usd|null, estimated, real}`; save
  `{summary, path, renamed, item_id}`.
- Prices (xAI, read 2026-10-19): image $0.02 per image; image-quality $0.05 (1k) / $0.07 (2k); video-1.5-lite
  $0.02 / 0.03 / 0.14 per second (480p / 720p / 1080p); video-1.5 Full $0.08 / 0.14 / 0.25 per second; each input
  image adds $0.002 (image) or $0.01 (the others). The real cost comes back in `usage.cost_in_usd_ticks`
  (10¹⁰ ticks = $1) and is what the result shows when present.

### Apps & tools (`/api/apps/*`)

Nothing happens in the background: every look is one screenshot, on the user's click, read by the local
computer-control model (or the model server set in `computer.server_url`). JARVIS never types or clicks in
sign-in screens, password fields, password managers or Windows Security. The actions answer 409 while the
kill switch is engaged.

| Method & path | Body | Returns |
|---|---|---|
| GET `/api/apps/status?lang=` | – | `{ways: {key, label, detail, available, reason, needs}[], stop, limits, note, computer_enabled, busy, task}` (`ways`: launch, windows, uia, vision, connectors) |
| GET `/api/apps/list?q=&refresh=` | – | `{installed: {…, can: ["open"], why_not}[], total, matched, windows, errors}` |
| POST `/api/apps/open` | `{name}` | `Job` (`apps.open`) |
| POST `/api/apps/look` | `{task_id?, window?}` | `Job` (`apps.look`: the app, the screen and what JARVIS understood it sees) |
| POST `/api/apps/correct` | `{text, task_id?}` | `{task}` (what JARVIS got wrong, used in the task's next look and suggestion) |
| POST `/api/apps/suggest` | `{goal, task_id?, window?}` | `Job` (`apps.suggest`: one next step with its preview; nothing is done) |
| POST `/api/apps/step` | `{suggestion}` (the suggest job's id) | `Job` (`apps.step`: that step, after approval; sending, deleting, publishing and paying are asked again right before) |
| DELETE `/api/apps/task/{id}` | – | `{ended}` (with any approval remembered for that task) |

### Privacy & data (`/api/privacy`)

| Method & path | Body | Returns |
|---|---|---|
| GET `/api/privacy` | – | `PrivacyInventory` |
| POST `/api/privacy/clear` | `{items: PrivacyKey[]}` (1–13) | `{results: {[key]: {ok, count?, reason?: "busy"\|"error", in_use?}}, inventory: PrivacyInventory}`; 422 for an unknown kind |

- `PrivacyKey`: `chat, commands, tasks, memory, projects, files, jobs, code, media, speed, logs, temp,
  approvals`.
- `PrivacyInventory`: `{items: {key, count, bytes, where[], can_clear, busy, kept?, running?, cached?}[],
  database: {where, bytes}, kept: {key: "keys"|"settings"|"work"|"models", where[], count?, names?}[],
  senses: {microphone, screen, cloud}}`. Counts, sizes and places only, never the data; API keys by name and
  store, never their values. `senses` says what can reach the microphone, the screen and the internet as
  JARVIS is set up right now (for example `tts_online` when the reply text goes to Microsoft's online voice).
- Deleting is for good: the memory database is rewritten so the deleted text is not left in the file or its
  journal. Work in progress stays: running jobs, interrupted tasks, and a screen's data while that screen is
  busy (`reason: "busy"`). `approvals` revokes only approvals the user gave; the default permissions stay.
  `logs` empties the files; one a running part of JARVIS writes is listed in `in_use`.
- After a delete, every open window gets a new `state` snapshot, and the activity log gets one line saying
  what was deleted.

## WebSocket `/ws`

Every server message: `{ "type": string, "data": any, "ts": string }`.

| type | data | when |
|---|---|---|
| `hello` | `{version}` | on connect |
| `state` | `StateSnapshot` | on connect, after reconnect, and after a privacy delete |
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
| `providers` | `{active, anthropic, xai, openai, local, fallbacks, agents: {AgentId: provider\|null}}` | an API key or an AI setting changed (each agent's card is sent again too) |
| `ui.navigate` | `{screen: "agents"\|"tasks", task_id?}` | a voice/text command asked to show a screen ("show the agents") |
| `job` | `Job` | a work-screen job started, moved to a step, made progress (at most every 0.15 s) or ended |
| `pong` | `{}` | answer to `{type: "ping"}` |

Client → server over WS (optional; REST works too): `{type: "command", text}`,
`{type: "confirm", id, approved, remember?}`, `{type: "ping"}` (server answers `pong`).

A client that falls more than 2000 events behind is closed with code 4408; it reconnects and
starts again from a fresh `state` snapshot.
