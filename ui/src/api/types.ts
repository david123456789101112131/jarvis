// Data shapes of the JARVIS local API (docs/API.md). Keep in sync with the core.

export type AgentId = 'research' | 'system' | 'development' | 'creative' | 'planner';
export type AgentStatus = 'offline' | 'idle' | 'thinking' | 'working' | 'waiting' | 'completed' | 'error';
export type TaskStatus =
  | 'pending'
  | 'planning'
  | 'queued'
  | 'running'
  | 'waiting'
  | 'verifying'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'interrupted'
  | 'skipped';
export type Mode = 'chat' | 'normal' | 'task' | 'autonomous' | 'debug';
export type Priority = 'critical' | 'high' | 'normal' | 'low';
export type VoiceState = 'disabled' | 'idle' | 'wake' | 'listening' | 'processing' | 'speaking' | 'error';
export type Theme = 'blue' | 'green' | 'orange' | 'red' | 'purple' | 'white';
export type Lang = 'he' | 'en';
export type ActivityLevel = 'debug' | 'info' | 'success' | 'warning' | 'error';
export type RiskLevel = 'safe' | 'low' | 'medium' | 'high' | 'critical';
export type InputSource = 'text' | 'voice' | 'api' | 'system';

export const AGENT_IDS: readonly AgentId[] = ['research', 'system', 'development', 'creative', 'planner'];
export const MODES: readonly Mode[] = ['chat', 'normal', 'task', 'autonomous', 'debug'];
export const THEMES: readonly Theme[] = ['blue', 'green', 'orange', 'red', 'purple', 'white'];
export const PRIORITIES: readonly Priority[] = ['critical', 'high', 'normal', 'low'];
export const ACTIVE_TASK_STATUSES: readonly TaskStatus[] = [
  'pending',
  'planning',
  'queued',
  'running',
  'waiting',
  'verifying',
];
export const TERMINAL_TASK_STATUSES: readonly TaskStatus[] = ['completed', 'failed', 'cancelled', 'skipped'];

export interface ActivityEntry {
  id: string;
  ts: string;
  time: string;
  source: string;
  message: string;
  level: ActivityLevel;
  task_id?: string | null;
  agent_id?: string | null;
  kind: string;
}

export interface AgentView {
  id: AgentId;
  number: 1 | 2 | 3 | 4 | 5;
  name: string;
  name_he: string;
  role: string;
  role_he: string;
  status: AgentStatus;
  enabled: boolean;
  killed: boolean;
  current: { task_id: string; subtask_id: string | null; title: string } | null;
  progress: number;
  last_action: string | null;
  last_action_at: string | null;
  errors: { at: string; message: string }[];
  permissions: string[];
  queue: number;
  /** Subtasks this agent is executing right now. */
  running?: number;
  stats: { completed: number; failed: number; avg_duration_s: number | null };
  skills: string[];
}

export interface AgentDetail extends AgentView {
  activity: ActivityEntry[];
}

export interface SubTaskView {
  id: string;
  agent: AgentId;
  title: string;
  instruction: string;
  status: TaskStatus;
  progress: number;
  attempts: number;
  depends_on: string[];
  optional: boolean;
  started_at: string | null;
  finished_at: string | null;
  summary: string | null;
  error: string | null;
  files_created: string[];
}

export interface TaskSummary {
  id: string;
  title: string;
  goal: string;
  status: TaskStatus;
  progress: number;
  priority: Priority;
  mode: Mode;
  agents: AgentId[];
  source: InputSource;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
  summary: string | null;
  error: string | null;
}

export interface TimelineEntry {
  ts: string;
  time: string;
  kind: string;
  message: string;
  agent_id?: string | null;
  subtask_id?: string | null;
  level?: string;
}

export interface TaskResult {
  summary: string;
  details: string | null;
  files_created: string[];
  actions_taken: string[];
  issues: string[];
  next_step: string | null;
}

export interface TaskDetail extends TaskSummary {
  plan: { strategy: string; subtasks: SubTaskView[] } | null;
  timeline: TimelineEntry[];
  result: TaskResult | null;
}

export type ChatKind = 'answer' | 'ack' | 'plan' | 'result' | 'error' | 'info';

export interface ChatMessage {
  id: string;
  role: 'user' | 'jarvis' | 'system';
  text: string;
  ts: string;
  time: string;
  source: InputSource;
  language: Lang;
  kind: ChatKind;
  task_id?: string | null;
  intent?: string | null;
}

export interface SystemStats {
  ts: string;
  cpu: { percent: number; per_core: number[]; cores: number; freq_mhz: number | null; name: string };
  memory: { percent: number; used_gb: number; total_gb: number };
  disks: { mount: string; percent: number; used_gb: number; total_gb: number }[];
  network: { up_kbps: number; down_kbps: number; sent_mb: number; recv_mb: number };
  battery: { percent: number; plugged: boolean } | null;
  uptime_s: number;
  processes: number;
  os: string;
  hostname: string;
}

export interface ConfirmationRequest {
  id: string;
  agent_id: AgentId;
  task_id: string | null;
  tool: string;
  summary: string;
  reasons: string[];
  risk: RiskLevel;
  details: Record<string, unknown>;
  created_at: string;
  expires_at: string | null;
  status: 'pending' | 'approved' | 'denied' | 'expired' | 'cancelled';
}

export interface VoiceInfo {
  state: VoiceState;
  enabled: boolean;
  muted: boolean;
  wake_word: string;
  engines: { wake: string; stt: string; tts: string };
  devices: { input: string | null; output: string | null };
  last_transcript: string | null;
  error: string | null;
}

export interface UISettings {
  theme_color: Theme;
  orb_size: number;
  orb_opacity: number;
  follow_mouse: boolean;
  always_active: boolean;
  mini_orb: boolean;
}

export interface KillSwitchState {
  global: boolean;
  agents: Record<string, string>;
}

export interface ProvidersInfo {
  active: string | null;
  anthropic: boolean;
  openai: boolean;
  local: boolean;
  fallbacks: boolean;
}

export interface StateSnapshot {
  version: string;
  assistant_name: string;
  user_name: string;
  language: 'auto' | 'he' | 'en';
  mode: Mode;
  voice: VoiceInfo;
  ui: UISettings;
  agents: AgentView[];
  tasks: { active: TaskSummary[]; recent: TaskSummary[] };
  chat: ChatMessage[];
  activity: ActivityEntry[];
  confirmations: ConfirmationRequest[];
  kill_switch: KillSwitchState;
  system: SystemStats | null;
  providers: ProvidersInfo;
  files: { workspace: string; projects: string };
}

export interface AuditEntry {
  id: string;
  ts: string;
  agent_id: string | null;
  task_id: string | null;
  tool: string;
  capability: string;
  action: string;
  risk: string;
  decision: 'allowed' | 'denied' | 'confirmed' | 'rejected' | 'blocked' | 'error' | string;
  outcome: 'pending' | 'ok' | 'error' | 'skipped' | string;
  reason: string;
  error: string | null;
  duration_ms: number | null;
  /** Tool arguments as audited (already redacted by the core). */
  params?: Record<string, unknown>;
}

/** Permission grant (shape of the core's `Grant` model; not spelled out in docs/API.md). */
export interface Grant {
  id: string;
  agent_id: string;
  capability: string;
  roots: string[];
  task_id: string | null;
  expires_at: string | null;
  max_uses: number | null;
  uses: number;
  source: 'policy' | 'user' | 'jarvis' | string;
  reason: string;
  created_at: string;
}

export interface AgentPermissions {
  capabilities: string[];
  grants: Grant[];
  restricted_to_task: string | null;
  folder_scope: string[] | null;
  risk_ceiling: string;
}

export interface PermissionsView {
  agents: Partial<Record<AgentId, AgentPermissions>>;
  capabilities: string[];
}

export interface GrantRequest {
  agent_id: AgentId;
  capability: string;
  roots?: string[];
  task_id?: string;
  ttl_s?: number;
  max_uses?: number;
  reason?: string;
}

export type FactCategory = 'preference' | 'fact' | 'decision' | 'note';
export const FACT_CATEGORIES: readonly FactCategory[] = ['preference', 'fact', 'decision', 'note'];

export interface MemoryFact {
  id: string;
  text: string;
  category: FactCategory | string;
  source?: string;
  importance?: number;
  task_id?: string | null;
  project_id?: string | null;
  created_at: string;
  updated_at?: string;
}

export interface MemorySearchHit {
  layer: string;
  id: string;
  text: string;
  score: number;
  ts: string;
  ref?: string | null;
}

export interface MemoryProject {
  id: string;
  name: string;
  path: string | null;
  description: string;
  updated_at: string;
  status: string;
}

export interface RecentFile {
  path: string;
  name: string;
  size: number | null;
  task_id: string | null;
  agent_id: string | null;
  created_at: string;
}

export interface FileListing {
  path: string;
  entries: { name: string; path: string; is_dir: boolean; size: number; modified: string }[];
}

export interface SecretStatus {
  label: string;
  configured: boolean;
  source: string | null;
}

export interface QuickTool {
  id: string;
  label: string;
  label_he: string;
  icon: string;
  command: string;
}

export interface ProcessInfo {
  name: string;
  pid: number;
  cpu: number;
  memory_mb: number;
}

export interface CommandResponse {
  accepted: boolean;
  message: ChatMessage;
  intent: { name: string; complexity: string; agents: AgentId[] };
  task_id?: string;
  reply?: ChatMessage;
}

/**
 * Settings object returned by GET /api/settings (the core's `Settings` model minus secrets).
 * Only the sections the UI edits are typed; everything else is passed through untouched.
 */
export interface SettingsObject {
  general?: {
    assistant_name?: string;
    user_name?: string;
    language?: 'auto' | 'he' | 'en';
    default_mode?: Mode;
    [k: string]: unknown;
  };
  voice?: {
    enabled?: boolean;
    speak_responses?: boolean;
    always_listening?: boolean;
    wake_word_model?: string;
    stt_language?: 'auto' | 'he' | 'en';
    tts_voice_he?: string;
    tts_voice_en?: string;
    input_device?: string;
    output_device?: string;
    [k: string]: unknown;
  };
  ui?: Partial<UISettings> & Record<string, unknown>;
  ai?: { provider?: string; [k: string]: unknown };
  [section: string]: unknown;
}

export type DeepPartialSettings = {
  [K in keyof SettingsObject]?: SettingsObject[K] extends object ? Partial<SettingsObject[K]> : SettingsObject[K];
};

export type PipelineStep =
  | 'transcribing'
  | 'understanding'
  | 'routing'
  | 'selecting_tool'
  | 'preparing_action'
  | 'executing'
  | 'done';

export interface VoiceStateEvent {
  state: VoiceState;
  level?: number;
  muted: boolean;
  /** Why the voice service is in the error (or disabled) state, in the user's language. */
  error?: string | null;
  /** What started the state: "wake", "ptt", "follow_up"… */
  trigger?: string | null;
}

/** Microphone / speech level, ~15 times a second while listening or speaking. */
export interface VoiceLevelEvent {
  level: number;
}

/** The core asks the UI to show a screen ("show me the agents"). */
export interface NavigateEvent {
  screen: string;
  task_id?: string | null;
}

export interface TranscriptEvent {
  text: string;
  final: boolean;
  language: string;
}

export interface PipelineEvent {
  step: PipelineStep;
  status: 'active' | 'done' | 'error';
  label: string;
}

export interface ActionEvent {
  id: string;
  title: string;
  detail?: string;
  icon?: string;
  progress?: number;
  /** `info`: JARVIS asked the user for a missing detail ("which city?"); neither done nor failed. */
  status: 'running' | 'done' | 'error' | 'info';
}

export interface SpeechEvent {
  text: string;
  language: string;
}

export interface NotificationEvent {
  id: string;
  level: string;
  title: string;
  message: string;
}

export type AgentAction = 'start' | 'stop' | 'restart' | 'kill' | 'release';
export type VoiceAction = 'start' | 'stop' | 'mute' | 'unmute' | 'ptt_start' | 'ptt_stop' | 'cancel';
/** Desktop window actions (POST /api/window/{action}); 409 when JARVIS runs without a window. */
export type WindowAction = 'show' | 'hide' | 'toggle' | 'mini' | 'full' | 'quit';

/** Every server → client WebSocket message: `{type, data, ts}`. */
export type ServerEvent =
  | { type: 'hello'; data: { version: string }; ts?: string }
  | { type: 'state'; data: StateSnapshot; ts?: string }
  | { type: 'chat.message'; data: ChatMessage; ts?: string }
  | { type: 'agent.status'; data: AgentView; ts?: string }
  | { type: 'task.updated'; data: TaskSummary; ts?: string }
  | { type: 'task.subtask'; data: { task_id: string; subtask: SubTaskView }; ts?: string }
  | { type: 'task.timeline'; data: { task_id: string; entry: TimelineEntry }; ts?: string }
  | { type: 'activity'; data: ActivityEntry; ts?: string }
  | { type: 'audit'; data: AuditEntry; ts?: string }
  | { type: 'system.stats'; data: SystemStats; ts?: string }
  | { type: 'voice.state'; data: VoiceStateEvent; ts?: string }
  | { type: 'voice.level'; data: VoiceLevelEvent; ts?: string }
  | { type: 'voice.transcript'; data: TranscriptEvent; ts?: string }
  | { type: 'voice.pipeline'; data: PipelineEvent; ts?: string }
  | { type: 'action'; data: ActionEvent; ts?: string }
  | { type: 'speech'; data: SpeechEvent; ts?: string }
  | { type: 'confirm.request'; data: ConfirmationRequest; ts?: string }
  | { type: 'confirm.response'; data: ConfirmationRequest; ts?: string }
  | { type: 'mode'; data: { mode: Mode }; ts?: string }
  | { type: 'killswitch'; data: KillSwitchState; ts?: string }
  | { type: 'notification'; data: NotificationEvent; ts?: string }
  | { type: 'settings'; data: { ui: UISettings }; ts?: string }
  | { type: 'ui.navigate'; data: NavigateEvent; ts?: string }
  | { type: 'pong'; data?: unknown; ts?: string };

export type ServerEventType = ServerEvent['type'];

/** Client → server WebSocket messages (REST works too). */
export type ClientMessage =
  | { type: 'command'; text: string }
  | { type: 'confirm'; id: string; approved: boolean; remember?: boolean }
  | { type: 'ping' };

export const SERVER_EVENT_TYPES: readonly ServerEventType[] = [
  'hello',
  'state',
  'chat.message',
  'agent.status',
  'task.updated',
  'task.subtask',
  'task.timeline',
  'activity',
  'audit',
  'system.stats',
  'voice.state',
  'voice.level',
  'voice.transcript',
  'voice.pipeline',
  'action',
  'speech',
  'confirm.request',
  'confirm.response',
  'mode',
  'killswitch',
  'notification',
  'settings',
  'ui.navigate',
  'pong',
];

export function isServerEvent(value: unknown): value is ServerEvent {
  if (!value || typeof value !== 'object') return false;
  const type = (value as { type?: unknown }).type;
  return typeof type === 'string' && (SERVER_EVENT_TYPES as readonly string[]).includes(type);
}
