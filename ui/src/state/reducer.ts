import {
  ACTIVE_TASK_STATUSES,
  type ActivityEntry,
  type AgentView,
  type AuditEntry,
  type ChatMessage,
  type PipelineEvent,
  type PipelineStep,
  type ServerEvent,
  type StateSnapshot,
  type SubTaskView,
  type SystemStats,
  type TaskDetail,
  type TaskSummary,
  type TimelineEntry,
  type UISettings,
  type VoiceInfo,
  type VoiceState,
} from '../api/types';
import type { Action, AppState, OverlayState, PipelineStepState, SystemSample, Toast, ToastLevel } from './types';

export const CHAT_CAP = 300;
export const ACTIVITY_CAP = 600;
export const AUDIT_CAP = 500;
export const RECENT_TASKS_CAP = 60;
export const HISTORY_CAP = 120;
export const TOAST_CAP = 4;

export const PIPELINE_ORDER: readonly PipelineStep[] = [
  'transcribing',
  'understanding',
  'routing',
  'selecting_tool',
  'preparing_action',
];

export const DEFAULT_UI: UISettings = {
  theme_color: 'blue',
  orb_size: 1,
  orb_opacity: 0.92,
  follow_mouse: false,
  always_active: true,
  mini_orb: true,
};

export const DEFAULT_VOICE: VoiceInfo = {
  state: 'idle',
  enabled: false,
  muted: false,
  wake_word: 'jarvis',
  engines: { wake: 'none', stt: 'none', tts: 'none' },
  devices: { input: null, output: null },
  last_transcript: null,
  error: null,
};

export function emptyOverlay(at = 0): OverlayState {
  return { stateSince: at, transcript: null, pipeline: null, action: null, speech: null, error: null };
}

export function createInitialState(): AppState {
  return {
    connection: { status: 'idle', attempt: 0, nextRetryAt: null, lastError: null, connectedAt: null },
    hydrated: false,
    serverVersion: null,
    version: '',
    assistantName: 'JARVIS',
    userName: '',
    language: 'auto',
    mode: 'normal',
    voice: DEFAULT_VOICE,
    voiceLevel: 0,
    ui: DEFAULT_UI,
    agents: [],
    tasks: { active: [], recent: [] },
    taskDetails: {},
    liveSubtasks: {},
    chat: [],
    pendingChat: [],
    activity: [],
    audit: [],
    auditLoaded: false,
    confirmations: [],
    killSwitch: { global: false, agents: {} },
    system: null,
    systemHistory: [],
    providers: { active: null, anthropic: false, openai: false, local: false, fallbacks: false },
    files: { workspace: '', projects: '' },
    overlay: emptyOverlay(),
    toasts: [],
    navigation: null,
  };
}

// ---------------------------------------------------------------- helpers

function byTs<T extends { ts: string }>(a: T, b: T): number {
  return a.ts < b.ts ? -1 : a.ts > b.ts ? 1 : 0;
}

/** Replace the item with the same id in place, or insert it (at the front or the end). */
export function upsert<T extends { id: string }>(list: readonly T[], item: T, where: 'front' | 'end' = 'end'): T[] {
  const index = list.findIndex((x) => x.id === item.id);
  if (index >= 0) {
    const next = list.slice();
    next[index] = item;
    return next;
  }
  return where === 'front' ? [item, ...list] : [...list, item];
}

function cap<T>(list: T[], max: number, keep: 'first' | 'last'): T[] {
  if (list.length <= max) return list;
  return keep === 'last' ? list.slice(list.length - max) : list.slice(0, max);
}

function clamp(value: number, min: number, max: number, fallback: number): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) return fallback;
  return Math.min(max, Math.max(min, value));
}

export function normalizeUi(ui: Partial<UISettings> | undefined, base: UISettings = DEFAULT_UI): UISettings {
  const merged = { ...base, ...(ui ?? {}) };
  return {
    ...merged,
    orb_size: clamp(merged.orb_size, 0.4, 2, base.orb_size),
    orb_opacity: clamp(merged.orb_opacity, 0.1, 1, base.orb_opacity),
  };
}

function sortAgents(agents: AgentView[]): AgentView[] {
  return agents.slice().sort((a, b) => a.number - b.number);
}

export function isActiveTask(task: Pick<TaskSummary, 'status'>): boolean {
  return ACTIVE_TASK_STATUSES.includes(task.status);
}

export function systemSample(stats: SystemStats, at: number): SystemSample {
  const parsed = Date.parse(stats.ts);
  const disk = stats.disks.length ? stats.disks[0].percent : 0;
  return {
    t: Number.isFinite(parsed) ? parsed : at,
    cpu: stats.cpu.percent,
    mem: stats.memory.percent,
    disk,
    up: stats.network.up_kbps,
    down: stats.network.down_kbps,
  };
}

function toastLevel(level: string): ToastLevel {
  if (level === 'success' || level === 'warning' || level === 'error') return level;
  if (level === 'warn') return 'warning';
  if (level === 'critical') return 'error';
  return 'info';
}

function pushToast(toasts: Toast[], toast: Toast): Toast[] {
  const without = toasts.filter((t) => t.id !== toast.id);
  return cap([...without, toast], TOAST_CAP, 'last');
}

// ---------------------------------------------------------------- tasks

function applyTaskSummary(state: AppState, task: TaskSummary): AppState {
  const active = isActiveTask(task);
  const inActive = state.tasks.active.some((t) => t.id === task.id);
  const activeList = active
    ? upsert(state.tasks.active, task, 'front')
    : inActive
      ? state.tasks.active.filter((t) => t.id !== task.id)
      : state.tasks.active;
  const recentList = active
    ? state.tasks.recent.filter((t) => t.id !== task.id)
    : cap(upsert(state.tasks.recent.filter((t) => t.id !== task.id), task, 'front'), RECENT_TASKS_CAP, 'first');
  const cached = state.taskDetails[task.id];
  const taskDetails = cached ? { ...state.taskDetails, [task.id]: { ...cached, ...task } } : state.taskDetails;
  return { ...state, tasks: { active: activeList, recent: recentList }, taskDetails };
}

function applySubtask(state: AppState, taskId: string, subtask: SubTaskView): AppState {
  const cached = state.taskDetails[taskId];
  if (cached) {
    const plan = cached.plan ?? { strategy: '', subtasks: [] };
    const detail: TaskDetail = { ...cached, plan: { ...plan, subtasks: upsert(plan.subtasks, subtask) } };
    return { ...state, taskDetails: { ...state.taskDetails, [taskId]: detail } };
  }
  const live = state.liveSubtasks[taskId] ?? [];
  return { ...state, liveSubtasks: { ...state.liveSubtasks, [taskId]: upsert(live, subtask) } };
}

function sameTimeline(a: TimelineEntry, b: TimelineEntry): boolean {
  return a.ts === b.ts && a.kind === b.kind && a.message === b.message;
}

function applyTimeline(state: AppState, taskId: string, entry: TimelineEntry): AppState {
  const cached = state.taskDetails[taskId];
  if (!cached) return state;
  if (cached.timeline.some((e) => sameTimeline(e, entry))) return state;
  const detail: TaskDetail = { ...cached, timeline: [...cached.timeline, entry] };
  return { ...state, taskDetails: { ...state.taskDetails, [taskId]: detail } };
}

function applyTaskDetail(state: AppState, detail: TaskDetail): AppState {
  const live = state.liveSubtasks[detail.id];
  let merged = detail;
  if (live?.length) {
    const plan = detail.plan ?? { strategy: '', subtasks: [] };
    let subtasks = plan.subtasks;
    for (const sub of live) {
      const known = subtasks.find((s) => s.id === sub.id);
      // The fetched detail is authoritative unless the event shows later progress.
      if (!known || (known.status === sub.status && sub.progress > known.progress)) subtasks = upsert(subtasks, sub);
    }
    merged = { ...detail, plan: { ...plan, subtasks } };
  }
  const liveSubtasks = { ...state.liveSubtasks };
  delete liveSubtasks[detail.id];
  let next: AppState = { ...state, taskDetails: { ...state.taskDetails, [detail.id]: merged }, liveSubtasks };
  // Keep the active/recent lists consistent with the freshest summary we have.
  const summary = state.tasks.active.find((t) => t.id === detail.id) ?? state.tasks.recent.find((t) => t.id === detail.id);
  if (summary && summary.updated_at <= detail.updated_at) {
    const { plan: _plan, timeline: _timeline, result: _result, ...rest } = detail;
    next = applyTaskSummary(next, rest);
    next = { ...next, taskDetails: { ...next.taskDetails, [detail.id]: merged } };
  }
  return next;
}

// ---------------------------------------------------------------- voice overlay

function transitionOverlay(overlay: OverlayState, prev: VoiceState, next: VoiceState, at: number): OverlayState {
  if (next === 'wake' || (next === 'listening' && prev !== 'wake')) {
    return emptyOverlay(at);
  }
  if (next === 'error') {
    return { ...overlay, stateSince: at, error: overlay.error && at - overlay.error.at < 5000 ? overlay.error : { message: null, detail: null, at } };
  }
  return { ...overlay, stateSince: at };
}

function freshSteps(): PipelineStepState[] {
  return PIPELINE_ORDER.map((step) => ({ step, status: 'pending', label: null }));
}

export function applyPipeline(overlay: OverlayState, event: PipelineEvent, at: number): OverlayState {
  let pipeline = overlay.pipeline;
  const restart =
    !pipeline || (pipeline.finished && event.status === 'active') || (event.step === 'transcribing' && event.status === 'active' && pipeline.steps.some((s) => s.status === 'done'));
  if (restart || !pipeline) {
    pipeline = { steps: freshSteps(), startedAt: at, updatedAt: at, finished: false };
    // A new pipeline is a new interaction: drop what belonged to the previous one.
    const keepTranscript = overlay.transcript && at - overlay.transcript.at < 4000;
    overlay = { ...overlay, transcript: keepTranscript ? overlay.transcript : null, action: null, speech: null, error: null };
  }

  let steps = pipeline.steps.slice();
  let finished: boolean = pipeline.finished;
  if (event.step === 'done') {
    steps = steps.map((s) => (s.status === 'error' ? s : { ...s, status: 'done' }));
    finished = true;
  } else {
    let index = steps.findIndex((s) => s.step === event.step);
    if (index < 0) {
      steps.push({ step: event.step, status: 'pending', label: null });
      index = steps.length - 1;
    }
    steps[index] = { ...steps[index], status: event.status, label: event.label || steps[index].label };
    if (event.status === 'active' || event.status === 'done') {
      // Steps before the current one are complete even if the core did not say so.
      steps = steps.map((s, i) => (i < index && (s.status === 'pending' || s.status === 'active') ? { ...s, status: 'done' } : s));
    }
    if (event.status === 'error') finished = true;
  }
  return { ...overlay, pipeline: { steps, startedAt: pipeline.startedAt, updatedAt: at, finished } };
}

// ---------------------------------------------------------------- snapshot

function applySnapshot(state: AppState, snap: StateSnapshot, at: number): AppState {
  const voiceChanged = snap.voice.state !== state.voice.state;
  const system = snap.system ?? state.system;
  return {
    ...state,
    hydrated: true,
    version: snap.version,
    assistantName: snap.assistant_name || 'JARVIS',
    userName: snap.user_name ?? '',
    language: snap.language ?? 'auto',
    mode: snap.mode,
    voice: snap.voice,
    voiceLevel: 0,
    ui: normalizeUi(snap.ui),
    agents: sortAgents(snap.agents ?? []),
    tasks: { active: snap.tasks?.active ?? [], recent: snap.tasks?.recent ?? [] },
    chat: cap((snap.chat ?? []).slice().sort(byTs), CHAT_CAP, 'last'),
    activity: cap((snap.activity ?? []).slice().sort(byTs), ACTIVITY_CAP, 'last'),
    confirmations: (snap.confirmations ?? []).filter((c) => c.status === 'pending'),
    killSwitch: snap.kill_switch ?? { global: false, agents: {} },
    system,
    systemHistory: snap.system ? cap([...state.systemHistory, systemSample(snap.system, at)], HISTORY_CAP, 'last') : state.systemHistory,
    providers: snap.providers ?? state.providers,
    files: snap.files ?? state.files,
    overlay: voiceChanged ? emptyOverlay(at) : state.overlay,
  };
}

function applyChat(state: AppState, message: ChatMessage): AppState {
  const exists = state.chat.some((m) => m.id === message.id);
  let chat = exists ? upsert(state.chat, message) : [...state.chat, message];
  if (!exists && chat.length > 1 && chat[chat.length - 2].ts > message.ts) chat = chat.slice().sort(byTs);
  let pendingChat = state.pendingChat;
  if (message.role === 'user') {
    const index = pendingChat.findIndex((p) => p.text.trim() === message.text.trim());
    if (index >= 0) pendingChat = pendingChat.filter((_, i) => i !== index);
  }
  return { ...state, chat: cap(chat, CHAT_CAP, 'last'), pendingChat };
}

function applyActivity(state: AppState, entry: ActivityEntry): AppState {
  const tail = state.activity.slice(-50);
  if (tail.some((e) => e.id === entry.id)) return state;
  return { ...state, activity: cap([...state.activity, entry], ACTIVITY_CAP, 'last') };
}

function applyAudit(state: AppState, entry: AuditEntry): AppState {
  if (state.audit.some((e) => e.id === entry.id)) return { ...state, audit: upsert(state.audit, entry) };
  return { ...state, audit: cap([entry, ...state.audit], AUDIT_CAP, 'first') };
}

// ---------------------------------------------------------------- events

export function applyEvent(state: AppState, event: ServerEvent, at: number): AppState {
  switch (event.type) {
    case 'hello':
      return { ...state, serverVersion: event.data?.version ?? null };
    case 'state':
      return applySnapshot(state, event.data, at);
    case 'chat.message':
      return applyChat(state, event.data);
    case 'agent.status':
      return { ...state, agents: sortAgents(upsert(state.agents, event.data)) };
    case 'task.updated':
      return applyTaskSummary(state, event.data);
    case 'task.subtask':
      return applySubtask(state, event.data.task_id, event.data.subtask);
    case 'task.timeline':
      return applyTimeline(state, event.data.task_id, event.data.entry);
    case 'activity':
      return applyActivity(state, event.data);
    case 'audit':
      return applyAudit(state, event.data);
    case 'system.stats':
      return {
        ...state,
        system: event.data,
        systemHistory: cap([...state.systemHistory, systemSample(event.data, at)], HISTORY_CAP, 'last'),
      };
    case 'voice.state': {
      const prev = state.voice.state;
      const next = event.data.state;
      const streaming = next === 'listening' || next === 'speaking';
      const level = typeof event.data.level === 'number' ? Math.max(0, Math.min(1, event.data.level)) : streaming && prev === next ? state.voiceLevel : 0;
      const voice: VoiceInfo = {
        ...state.voice,
        state: next,
        muted: typeof event.data.muted === 'boolean' ? event.data.muted : state.voice.muted,
        // Any live state means the voice service runs; "disabled" means it does not.
        enabled: next !== 'disabled',
        error: 'error' in event.data ? (event.data.error ?? null) : state.voice.error,
      };
      if (prev === next && voice.muted === state.voice.muted && voice.enabled === state.voice.enabled && voice.error === state.voice.error) {
        return level === state.voiceLevel ? state : { ...state, voiceLevel: level };
      }
      return { ...state, voice, voiceLevel: level, overlay: prev === next ? state.overlay : transitionOverlay(state.overlay, prev, next, at) };
    }
    case 'voice.level': {
      const raw = Number(event.data?.level);
      const level = Number.isFinite(raw) ? Math.max(0, Math.min(1, raw)) : 0;
      return level === state.voiceLevel ? state : { ...state, voiceLevel: level };
    }
    case 'voice.transcript': {
      const { text, final, language } = event.data;
      const voice = final ? { ...state.voice, last_transcript: text } : state.voice;
      return { ...state, voice, overlay: { ...state.overlay, transcript: { text, final, language, at } } };
    }
    case 'voice.pipeline':
      return { ...state, overlay: applyPipeline(state.overlay, event.data, at) };
    case 'action': {
      const prev = state.overlay.action;
      const action = { ...event.data, at, startedAt: prev && prev.id === event.data.id ? prev.startedAt : at };
      const error =
        event.data.status === 'error' ? { message: event.data.title, detail: event.data.detail ?? null, at } : state.overlay.error;
      return { ...state, overlay: { ...state.overlay, action, error } };
    }
    case 'speech':
      return { ...state, overlay: { ...state.overlay, speech: { text: event.data.text, language: event.data.language, at } } };
    case 'confirm.request':
      return event.data.status === 'pending'
        ? { ...state, confirmations: upsert(state.confirmations, event.data) }
        : { ...state, confirmations: state.confirmations.filter((c) => c.id !== event.data.id) };
    case 'confirm.response':
      return event.data.status === 'pending'
        ? { ...state, confirmations: upsert(state.confirmations, event.data) }
        : { ...state, confirmations: state.confirmations.filter((c) => c.id !== event.data.id) };
    case 'mode':
      return { ...state, mode: event.data.mode };
    case 'killswitch':
      return { ...state, killSwitch: { global: !!event.data.global, agents: event.data.agents ?? {} } };
    case 'notification':
      return {
        ...state,
        toasts: pushToast(state.toasts, {
          id: event.data.id,
          level: toastLevel(event.data.level),
          title: event.data.title,
          message: event.data.message,
          at,
          source: 'server',
        }),
      };
    case 'settings':
      return { ...state, ui: normalizeUi(event.data.ui, state.ui) };
    case 'ui.navigate':
      return {
        ...state,
        navigation: { screen: String(event.data?.screen ?? ''), param: event.data?.task_id ?? null, seq: (state.navigation?.seq ?? 0) + 1 },
      };
    case 'pong':
      return state;
    default:
      return state;
  }
}

// ---------------------------------------------------------------- root reducer

export function reducer(state: AppState, action: Action): AppState {
  switch (action.type) {
    case 'event':
      return applyEvent(state, action.event, action.at);
    case 'connection':
      return { ...state, connection: action.info };
    case 'task.detail':
      return applyTaskDetail(state, action.detail);
    case 'audit.loaded': {
      const known = new Set(state.audit.map((e) => e.id));
      const merged = [...state.audit, ...action.entries.filter((e) => !known.has(e.id))].sort((a, b) => (a.ts < b.ts ? 1 : a.ts > b.ts ? -1 : 0));
      return { ...state, audit: cap(merged, AUDIT_CAP, 'first'), auditLoaded: true };
    }
    case 'chat.pending':
      return { ...state, pendingChat: [...state.pendingChat, action.pending] };
    case 'chat.pendingDone':
      return {
        ...state,
        pendingChat: action.failed
          ? state.pendingChat.map((p) => (p.clientId === action.clientId ? { ...p, failed: true } : p))
          : state.pendingChat.filter((p) => p.clientId !== action.clientId),
      };
    case 'chat.cleared':
      return { ...state, chat: [], pendingChat: [] };
    case 'toast.push':
      return { ...state, toasts: pushToast(state.toasts, { ...action.toast, at: action.toast.at ?? Date.now() }) };
    case 'toast.dismiss':
      return { ...state, toasts: state.toasts.filter((t) => t.id !== action.id) };
    case 'ui.local':
      return { ...state, ui: normalizeUi(action.patch, state.ui) };
    case 'confirmation.resolved':
      return { ...state, confirmations: state.confirmations.filter((c) => c.id !== action.id) };
    case 'overlay.dismiss':
      return { ...state, overlay: { ...emptyOverlay(action.at) } };
    default:
      return state;
  }
}
