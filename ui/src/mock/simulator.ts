// In-browser simulator of the JARVIS core. Emits the same events as the real WebSocket
// (docs/API.md) and answers the REST calls through MockApi. Used when no core is attached.
import type {
  ActionEvent,
  ActivityEntry,
  AgentAction,
  AgentAI,
  AgentId,
  AgentView,
  AuditEntry,
  ChatMessage,
  CommandResponse,
  ConfirmationRequest,
  DeepPartialSettings,
  Grant,
  GrantRequest,
  InputSource,
  KillSwitchState,
  Lang,
  MemoryFact,
  MemoryProject,
  MemorySearchHit,
  Mode,
  PipelineEvent,
  Priority,
  ProviderName,
  ProvidersInfo,
  RecentFile,
  SecretStatus,
  ServerEvent,
  SettingsObject,
  StateSnapshot,
  SubTaskView,
  SystemStats,
  TaskDetail,
  TaskSummary,
  UISettings,
  VoiceAction,
  VoiceInfo,
  VoiceState,
} from '../api/types';
import { ACTIVE_TASK_STATUSES, MODES } from '../api/types';
import {
  AGENT_PROFILES,
  APP_NAMES,
  CAPABILITIES,
  GENERIC_REPLIES,
  GENERIC_TEMPLATE,
  L,
  LL,
  NOTIFICATIONS,
  PATHS,
  RISK_CEILINGS,
  TASK_TEMPLATES,
  VOICE_SCRIPTS,
  pick,
  type Bi,
  type ConfirmSpec,
  type SubtaskTemplate,
  type TaskTemplate,
  type VoiceScript,
} from './content';
import {
  agentLabel,
  seedActivity,
  seedAgents,
  seedAudit,
  seedChat,
  seedFacts,
  seedFiles,
  seedGrants,
  seedProjects,
  seedSecrets,
  seedSettings,
  seedTasks,
} from './seed';
import { clamp, clone, hasHebrew, hmsAt, isoAt, mockId, rand, unique } from './util';

type Listener = (event: ServerEvent) => void;
type Timer = ReturnType<typeof setTimeout>;

export class NotFoundError extends Error {}

interface RunSub {
  view: SubTaskView;
  tpl: SubtaskTemplate;
  failedOnce: boolean;
  pausedUntil: number;
  confirm: 'none' | 'waiting' | 'done';
  actionIndex: number;
}

interface RunTask {
  detail: TaskDetail;
  tpl: TaskTemplate;
  subs: Map<string, RunSub>;
  timer: ReturnType<typeof setInterval> | null;
  stopped: boolean;
  issues: string[];
}

interface PendingConfirm {
  req: ConfirmationRequest;
  timer: Timer | null;
  resolve: (approved: boolean) => void;
}

export interface SimulatorOptions {
  lang: () => Lang;
  scene?: string | null;
  /** Start the ambient loops (voice cycle, tasks, notifications). Default true. */
  autoplay?: boolean;
  loadUi?: () => Partial<UISettings> | null;
  saveUi?: (ui: UISettings) => void;
}

const CONCURRENCY: Record<AgentId, number> = { research: 2, system: 2, development: 1, creative: 1, planner: 1 };
const LEVEL_HZ = 15;

export const SCENES = ['live', 'idle', 'wake', 'listening', 'processing', 'action', 'speaking', 'error'] as const;

export class Simulator {
  private readonly opts: SimulatorOptions;
  private listeners = new Set<Listener>();
  private timers = new Set<Timer>();
  private intervals = new Set<ReturnType<typeof setInterval>>();
  private voiceTimers = new Set<Timer>();
  private levelTimer: ReturnType<typeof setInterval> | null = null;
  private started = false;

  agents = new Map<AgentId, AgentView>();
  tasks = new Map<string, TaskDetail>();
  private runs = new Map<string, RunTask>();
  chat: ChatMessage[] = [];
  activity: ActivityEntry[] = [];
  audit: AuditEntry[] = [];
  private confirms = new Map<string, PendingConfirm>();
  killSwitch: KillSwitchState = { global: false, agents: {} };
  mode: Mode = 'normal';
  voice: VoiceInfo;
  ui: UISettings;
  settings: SettingsObject;
  facts: MemoryFact[] = [];
  projects: MemoryProject[] = [];
  files: RecentFile[] = [];
  grants: Grant[] = [];
  restrictions: Record<string, { task: string | null; folders: string[] | null }> = {};
  secrets: Record<string, SecretStatus> = {};
  private sys = { cpu: 14, mem: 55, up: 42, down: 380, sent: 812, recv: 6240, uptime: 3 * 3600 + 1240, processes: 284, battery: 86 };
  private voiceBusy = false;
  private voiceCycle = 0;
  private templateCursor = 0;
  scene: string | null = null;

  constructor(opts: SimulatorOptions) {
    this.opts = opts;
    const lang = opts.lang();
    const now = Date.now();
    const storedUi = opts.loadUi?.() ?? null;
    this.ui = {
      theme_color: 'blue',
      orb_size: 1,
      orb_opacity: 0.92,
      follow_mouse: false,
      always_active: true,
      mini_orb: true,
      ...(storedUi ?? {}),
    };
    this.voice = {
      state: 'idle',
      enabled: true,
      muted: false,
      wake_word: 'Hey Jarvis',
      engines: { wake: 'openwakeword', stt: 'faster_whisper', tts: 'edge' },
      devices: { input: 'Microphone (Realtek(R) Audio)', output: 'Speakers (Realtek(R) Audio)' },
      last_transcript: null,
      error: null,
    };
    for (const agent of seedAgents(lang, now)) this.agents.set(agent.id, agent);
    const seeded = seedTasks(lang, now);
    for (const task of seeded) this.tasks.set(task.id, task);
    this.chat = seedChat(lang, now);
    this.activity = seedActivity(lang, now, seeded);
    this.audit = seedAudit(now);
    this.facts = seedFacts(lang, now);
    this.projects = seedProjects(lang, now);
    this.files = seedFiles(now);
    this.grants = seedGrants(now);
    this.secrets = seedSecrets();
    this.settings = seedSettings(this.ui, lang);
    this.scene = opts.scene && opts.scene !== 'live' ? opts.scene : null;
  }

  // ------------------------------------------------------------------ lifecycle

  get lang(): Lang {
    return this.opts.lang();
  }

  private tx(text: Bi): string {
    return pick(text, this.lang);
  }

  start(): void {
    if (this.started) return;
    this.started = true;
    this.every(2000, () => this.emitStats());
    if (this.scene) {
      const scene = this.scene;
      this.after(300, () => this.setScene(scene));
    }
    if (this.opts.autoplay !== false) {
      this.after(3200, () => this.startTemplate(TASK_TEMPLATES[0], 'voice'));
      this.after(9000, () => this.voiceLoop());
      this.after(38000, () => this.notifyLoop());
    }
  }

  stop(): void {
    this.started = false;
    for (const t of this.timers) clearTimeout(t);
    for (const t of this.voiceTimers) clearTimeout(t);
    for (const i of this.intervals) clearInterval(i);
    for (const run of this.runs.values()) if (run.timer) clearInterval(run.timer);
    for (const c of this.confirms.values()) if (c.timer) clearTimeout(c.timer);
    if (this.levelTimer) clearInterval(this.levelTimer);
    this.timers.clear();
    this.voiceTimers.clear();
    this.intervals.clear();
    this.levelTimer = null;
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    const t = setTimeout(() => {
      this.timers.delete(t);
      listener({ type: 'hello', data: { version: '1.0.0-sim' }, ts: isoAt(Date.now()) });
      listener({ type: 'state', data: this.snapshot(), ts: isoAt(Date.now()) });
    }, 60);
    this.timers.add(t);
    return () => {
      this.listeners.delete(listener);
    };
  }

  private after(ms: number, fn: () => void, voice = false): Timer {
    const set = voice ? this.voiceTimers : this.timers;
    const t = setTimeout(() => {
      set.delete(t);
      fn();
    }, ms);
    set.add(t);
    return t;
  }

  private every(ms: number, fn: () => void): ReturnType<typeof setInterval> {
    const i = setInterval(fn, ms);
    this.intervals.add(i);
    return i;
  }

  private emit<T extends ServerEvent['type']>(type: T, data: Extract<ServerEvent, { type: T }>['data']): void {
    const event = { type, data: clone(data), ts: isoAt(Date.now()) } as ServerEvent;
    for (const listener of [...this.listeners]) listener(event);
  }

  // ------------------------------------------------------------------ views

  private summary(task: TaskDetail): TaskSummary {
    const { plan: _p, timeline: _t, result: _r, ...rest } = task;
    return rest;
  }

  systemStats(): SystemStats {
    const s = this.sys;
    const cores = Array.from({ length: 16 }, (_, i) => Math.round(clamp(s.cpu + Math.sin(Date.now() / 900 + i * 1.7) * 14 + rand(-6, 6), 1, 100)));
    return {
      ts: isoAt(Date.now()),
      cpu: { percent: Math.round(s.cpu * 10) / 10, per_core: cores, cores: 16, freq_mhz: Math.round(3200 + s.cpu * 14), name: 'AMD Ryzen 9 7940HS' },
      memory: { percent: Math.round(s.mem * 10) / 10, used_gb: Math.round(32 * s.mem) / 100, total_gb: 32 },
      disks: [
        { mount: 'C:\\', percent: 61, used_gb: 581, total_gb: 953 },
        { mount: 'D:\\', percent: 34, used_gb: 633, total_gb: 1863 },
      ],
      network: { up_kbps: Math.round(s.up), down_kbps: Math.round(s.down), sent_mb: Math.round(s.sent), recv_mb: Math.round(s.recv) },
      battery: { percent: Math.round(s.battery), plugged: true },
      uptime_s: Math.round(s.uptime),
      processes: Math.round(s.processes),
      os: 'Windows 11 Pro 24H2',
      hostname: 'DANA-PC',
    };
  }

  snapshot(): StateSnapshot {
    const all = [...this.tasks.values()];
    const active = all.filter((t) => ACTIVE_TASK_STATUSES.includes(t.status)).map((t) => this.summary(t));
    const recent = all
      .filter((t) => !ACTIVE_TASK_STATUSES.includes(t.status))
      .sort((a, b) => ((a.finished_at ?? a.updated_at) < (b.finished_at ?? b.updated_at) ? 1 : -1))
      .slice(0, 20)
      .map((t) => this.summary(t));
    return clone({
      version: '1.0.0',
      assistant_name: String(this.settings.general?.assistant_name ?? 'JARVIS'),
      user_name: String(this.settings.general?.user_name ?? ''),
      language: (this.settings.general?.language as StateSnapshot['language']) ?? 'auto',
      mode: this.mode,
      voice: this.voice,
      ui: this.ui,
      agents: [...this.agents.values()],
      tasks: { active, recent },
      chat: this.chat.slice(-100),
      activity: this.activity.slice(-200),
      confirmations: [...this.confirms.values()].map((c) => c.req),
      kill_switch: this.killSwitch,
      system: this.systemStats(),
      providers: this.providers(),
      files: { workspace: PATHS.workspace, projects: PATHS.projects },
    });
  }

  // ------------------------------------------------------------------ primitives

  private log(message: Bi | string, level: ActivityEntry['level'] = 'info', extra: { agent?: AgentId | null; task?: string | null; kind?: string } = {}) {
    const now = Date.now();
    const entry: ActivityEntry = {
      id: mockId('act'),
      ts: isoAt(now),
      time: hmsAt(now),
      source: extra.agent ? agentLabel(extra.agent, this.lang) : 'JARVIS',
      message: typeof message === 'string' ? message : this.tx(message),
      level,
      task_id: extra.task ?? null,
      agent_id: extra.agent ?? null,
      kind: extra.kind ?? 'activity',
    };
    this.activity.push(entry);
    if (this.activity.length > 400) this.activity.splice(0, this.activity.length - 400);
    this.emit('activity', entry);
  }

  private say(role: ChatMessage['role'], kind: ChatMessage['kind'], text: string, extra: Partial<ChatMessage> = {}): ChatMessage {
    const now = Date.now();
    const message: ChatMessage = {
      id: mockId('msg'),
      role,
      text,
      ts: isoAt(now),
      time: hmsAt(now),
      source: 'text',
      language: hasHebrew(text) ? 'he' : 'en',
      kind,
      task_id: null,
      intent: null,
      ...extra,
    };
    this.chat.push(message);
    if (this.chat.length > 200) this.chat.splice(0, this.chat.length - 200);
    this.emit('chat.message', message);
    return message;
  }

  /** Which providers have a key, and the one each agent works with (the real router's order). */
  private providers(): ProvidersInfo {
    const ai = (this.settings.ai ?? {}) as Record<string, unknown>;
    const on: Record<ProviderName, boolean> = {
      anthropic: this.secrets.ANTHROPIC_API_KEY?.configured ?? false,
      xai: this.secrets.XAI_API_KEY?.configured ?? false,
      openai: this.secrets.OPENAI_API_KEY?.configured ?? false,
      local: Boolean((ai.local as { enabled?: boolean } | undefined)?.enabled),
    };
    const order: ProviderName[] = ['anthropic', 'xai', 'openai', 'local'];
    const first = (preferred: unknown): ProviderName | null =>
      typeof preferred === 'string' && preferred in on && on[preferred as ProviderName]
        ? (preferred as ProviderName)
        : order.find((p) => on[p]) ?? null;
    const prefs = (ai.agent_providers ?? {}) as Record<string, string>;
    const agents: ProvidersInfo['agents'] = {};
    for (const id of this.agents.keys()) agents[id] = first(prefs[id] ?? 'xai');
    return { active: first(ai.provider), ...on, fallbacks: on.anthropic, agents };
  }

  private agentAI(id: AgentId, info: ProvidersInfo): AgentAI {
    const ai = (this.settings.ai ?? {}) as Record<string, { model?: string } | string | undefined>;
    const prefs = (ai.agent_providers ?? {}) as unknown as Record<string, AgentAI['preferred']>;
    const provider = info.agents?.[id] ?? null;
    const section = provider ? ai[provider] : undefined;
    return { preferred: prefs[id] ?? 'xai', provider, model: typeof section === 'object' ? section.model ?? null : null };
  }

  /** A key or an AI setting changed: tell the UI, as the core does. */
  private refreshProviders() {
    const info = this.providers();
    this.emit('providers', info);
    for (const id of this.agents.keys()) this.setAgent(id, { ai: this.agentAI(id, info) });
  }

  private setAgent(id: AgentId, patch: Partial<AgentView>) {
    const agent = this.agents.get(id);
    if (!agent) return;
    const next = { ...agent, ...patch };
    this.agents.set(id, next);
    this.emit('agent.status', next);
  }

  private recordAudit(agent: AgentId, tool: string, capability: string, action: string, risk: string, decision: string, outcome: string, reason = '', taskId: string | null = null) {
    const entry: AuditEntry = {
      id: mockId('aud'),
      ts: isoAt(Date.now()),
      agent_id: agent,
      task_id: taskId,
      tool,
      capability,
      action,
      risk,
      decision,
      outcome,
      reason,
      error: null,
      duration_ms: outcome === 'ok' ? Math.round(rand(20, 2400)) : null,
    };
    this.audit.unshift(entry);
    if (this.audit.length > 300) this.audit.length = 300;
    this.emit('audit', entry);
  }

  private notify(level: string, title: string, message: string) {
    this.emit('notification', { id: mockId('ntf'), level, title, message });
  }

  private setVoice(state: VoiceState, level?: number) {
    this.voice = { ...this.voice, state };
    this.emit('voice.state', level === undefined ? { state, muted: this.voice.muted } : { state, level, muted: this.voice.muted });
  }

  // ------------------------------------------------------------------ system stats

  private emitStats() {
    const running = [...this.runs.values()].reduce((n, r) => n + [...r.subs.values()].filter((s) => s.view.status === 'running').length, 0);
    const researching = [...this.runs.values()].some((r) => [...r.subs.values()].some((s) => s.view.status === 'running' && s.view.agent === 'research'));
    const s = this.sys;
    const cpuTarget = 8 + running * 13 + (this.voiceBusy ? 9 : 0) + rand(-3, 5);
    s.cpu = clamp(s.cpu + (cpuTarget - s.cpu) * 0.45 + rand(-2.5, 2.5), 2, 97);
    s.mem = clamp(s.mem + (52 + running * 2.4 - s.mem) * 0.2 + rand(-0.4, 0.4), 40, 88);
    s.down = clamp(s.down * 0.6 + (researching ? rand(1800, 5200) : rand(60, 900)) * 0.4, 5, 9000);
    s.up = clamp(s.up * 0.6 + rand(15, researching ? 420 : 140) * 0.4, 2, 3000);
    s.sent += (s.up * 2) / 8 / 1024;
    s.recv += (s.down * 2) / 8 / 1024;
    s.uptime += 2;
    s.processes = clamp(s.processes + Math.round(rand(-2, 2)), 240, 330);
    this.emit('system.stats', this.systemStats());
  }

  // ------------------------------------------------------------------ tasks

  private nextTemplate(): TaskTemplate {
    this.templateCursor = (this.templateCursor + 1) % TASK_TEMPLATES.length;
    return TASK_TEMPLATES[this.templateCursor];
  }

  startTemplate(tpl: TaskTemplate, source: InputSource, opts: { skipPlanning?: boolean; priority?: Priority } = {}): string {
    const now = Date.now();
    const title = this.tx(tpl.title);
    const detail: TaskDetail = {
      id: mockId('task'),
      title,
      goal: this.tx(tpl.goal),
      status: 'planning',
      progress: 0,
      priority: opts.priority ?? tpl.priority,
      mode: this.mode,
      agents: unique(tpl.subtasks.map((s) => s.agent)),
      source,
      created_at: isoAt(now),
      updated_at: isoAt(now),
      started_at: isoAt(now),
      finished_at: null,
      summary: null,
      error: null,
      plan: null,
      timeline: [],
      result: null,
    };
    this.tasks.set(detail.id, detail);
    const run: RunTask = { detail, tpl, subs: new Map(), timer: null, stopped: false, issues: [] };
    this.runs.set(detail.id, run);
    this.emitTask(run);
    this.timeline(run, 'received', L(`המשימה התקבלה: ${title}`, `Task received: ${title}`));
    this.log(L(`JARVIS קיבל משימה: ${title}`, `JARVIS received task: ${title}`), 'info', { task: detail.id, kind: 'task' });
    if (opts.skipPlanning) {
      this.plan(run);
    } else {
      this.setAgent('planner', {
        status: 'thinking',
        current: { task_id: detail.id, subtask_id: null, title: this.tx(L('תכנון המשימה', 'Planning the task')) },
        progress: 0.3,
        last_action: this.tx(L('מנתח את הבקשה', 'Analyzing the request')),
        last_action_at: isoAt(now),
      });
      this.log(L('סוכן תכנון שובץ', 'Planner Agent assigned'), 'info', { agent: 'planner', task: detail.id });
      this.after(2600, () => this.plan(run));
    }
    return detail.id;
  }

  private timeline(run: RunTask, kind: string, message: Bi | string, extra: { agent?: AgentId; subtask?: string; level?: string } = {}) {
    const now = Date.now();
    const entry = {
      ts: isoAt(now),
      time: hmsAt(now),
      kind,
      message: typeof message === 'string' ? message : this.tx(message),
      agent_id: extra.agent ?? null,
      subtask_id: extra.subtask ?? null,
      level: extra.level ?? 'info',
    };
    run.detail.timeline.push(entry);
    this.emit('task.timeline', { task_id: run.detail.id, entry });
  }

  private emitTask(run: RunTask) {
    run.detail.updated_at = isoAt(Date.now());
    this.emit('task.updated', this.summary(run.detail));
  }

  private emitSub(run: RunTask, sub: RunSub) {
    this.emit('task.subtask', { task_id: run.detail.id, subtask: sub.view });
  }

  private plan(run: RunTask) {
    if (run.stopped) return;
    const { detail, tpl } = run;
    const subtasks: SubTaskView[] = tpl.subtasks.map((st) => ({
      id: `${detail.id}.${st.key}`,
      agent: st.agent,
      title: this.tx(st.title),
      instruction: this.tx(st.instruction),
      status: st.depends.length ? 'pending' : 'queued',
      progress: 0,
      attempts: 0,
      depends_on: st.depends.map((d) => `${detail.id}.${d}`),
      optional: !!st.optional,
      started_at: null,
      finished_at: null,
      summary: null,
      error: null,
      files_created: [],
    }));
    detail.plan = { strategy: this.tx(tpl.strategy), subtasks };
    tpl.subtasks.forEach((st, i) => run.subs.set(subtasks[i].id, { view: subtasks[i], tpl: st, failedOnce: false, pausedUntil: 0, confirm: 'none', actionIndex: 0 }));
    detail.status = 'running';
    for (const sub of run.subs.values()) this.emitSub(run, sub);
    this.emitTask(run);
    this.timeline(run, 'plan', L(`נבנתה תוכנית של ${subtasks.length} תתי-משימות`, `Plan ready: ${subtasks.length} subtasks`), { agent: 'planner' });
    this.log(L(`סוכן תכנון בנה תוכנית של ${subtasks.length} תתי-משימות`, `Planner Agent created a plan with ${subtasks.length} subtasks`), 'info', { agent: 'planner', task: detail.id });
    const planner = this.agents.get('planner');
    if (planner?.current?.task_id === detail.id && planner.current.subtask_id === null) {
      this.setAgent('planner', { status: 'idle', current: null, progress: 0, last_action: this.tx(L('נבנתה תוכנית', 'Plan created')), last_action_at: isoAt(Date.now()) });
    }
    const lines = subtasks.map((s, i) => `${i + 1}. ${s.title} — ${agentLabel(s.agent, this.lang)}`).join('\n');
    this.say('jarvis', 'plan', `${this.tx(L('התוכנית:', 'The plan:'))}\n${lines}`, { task_id: detail.id });
    run.timer = setInterval(() => this.tick(run), 700);
  }

  private agentLoad(agent: AgentId): number {
    let n = 0;
    for (const run of this.runs.values()) for (const s of run.subs.values()) if (s.view.agent === agent && (s.view.status === 'running' || s.view.status === 'waiting')) n += 1;
    return n;
  }

  private tick(run: RunTask) {
    if (run.stopped || this.killSwitch.global) return;
    const subs = [...run.subs.values()];
    const done = (id: string) => {
      const s = run.subs.get(id);
      return !s || s.view.status === 'completed' || s.view.status === 'skipped';
    };
    for (const sub of subs) {
      const st = sub.view.status;
      if ((st === 'pending' || st === 'queued') && sub.view.depends_on.every(done)) {
        const agent = this.agents.get(sub.view.agent);
        const available = agent && agent.enabled && !agent.killed && !this.killSwitch.agents[sub.view.agent];
        if (available && this.agentLoad(sub.view.agent) < CONCURRENCY[sub.view.agent]) this.startSub(run, sub);
        else if (st === 'pending') {
          sub.view.status = 'queued';
          this.emitSub(run, sub);
        }
      }
    }
    for (const sub of subs) if (sub.view.status === 'running') this.advance(run, sub);
    const total = subs.length || 1;
    const progress = subs.reduce((sum, s) => sum + (s.view.status === 'completed' || s.view.status === 'skipped' ? 1 : s.view.progress), 0) / total;
    if (subs.every((s) => s.view.status === 'completed' || s.view.status === 'skipped')) {
      this.finish(run);
      return;
    }
    if (Math.abs(progress - run.detail.progress) > 0.004) {
      run.detail.progress = progress;
      this.emitTask(run);
    }
  }

  private startSub(run: RunTask, sub: RunSub) {
    const now = Date.now();
    sub.view.status = 'running';
    sub.view.attempts += 1;
    sub.view.started_at = sub.view.started_at ?? isoAt(now);
    sub.view.error = null;
    const agent = sub.view.agent;
    const firstAction = sub.tpl.actions[this.lang][0] ?? sub.view.title;
    this.setAgent(agent, {
      status: 'working',
      current: { task_id: run.detail.id, subtask_id: sub.view.id, title: sub.view.title },
      progress: sub.view.progress,
      last_action: firstAction,
      last_action_at: isoAt(now),
      queue: Math.max(0, (this.agents.get(agent)?.queue ?? 1) - 1),
    });
    this.emitSub(run, sub);
    this.timeline(run, 'subtask.started', L(`${agentLabel(agent, 'he')} התחיל: ${pick(sub.tpl.title, 'he')}`, `${agentLabel(agent, 'en')} started: ${pick(sub.tpl.title, 'en')}`), { agent, subtask: sub.view.id });
    this.log(L(`${agentLabel(agent, 'he')} התחיל: ${pick(sub.tpl.title, 'he')}`, `${agentLabel(agent, 'en')} started: ${pick(sub.tpl.title, 'en')}`), 'info', { agent, task: run.detail.id });
    if (sub.tpl.tool) this.recordAudit(agent, sub.tpl.tool.name, sub.tpl.tool.capability, sub.view.title, sub.tpl.tool.risk, 'allowed', 'ok', '', run.detail.id);
  }

  private advance(run: RunTask, sub: RunSub) {
    const now = Date.now();
    if (sub.pausedUntil > now) return;
    const agent = sub.view.agent;
    const step = (0.7 / sub.tpl.seconds) * rand(0.7, 1.3);
    sub.view.progress = Math.min(1, sub.view.progress + step);

    if (sub.tpl.failOnce && !sub.failedOnce && sub.view.progress > 0.45) {
      sub.failedOnce = true;
      const message = this.tx(sub.tpl.failOnce);
      sub.view.error = message;
      sub.view.progress = 0.12;
      sub.pausedUntil = now + 2200;
      const a = this.agents.get(agent)!;
      this.setAgent(agent, { status: 'error', errors: [{ at: isoAt(now), message }, ...a.errors].slice(0, 10), last_action: message, last_action_at: isoAt(now) });
      this.emitSub(run, sub);
      this.timeline(run, 'subtask.retry', message, { agent, subtask: sub.view.id, level: 'warning' });
      this.log(message, 'warning', { agent, task: run.detail.id });
      this.after(2200, () => {
        if (run.stopped || sub.view.status !== 'running') return;
        sub.view.attempts += 1;
        sub.view.error = null;
        this.setAgent(agent, { status: 'working', last_action: this.tx(L('ניסיון חוזר', 'Retrying')), last_action_at: isoAt(Date.now()) });
        this.emitSub(run, sub);
      });
      return;
    }

    if (sub.tpl.confirm && sub.confirm === 'none' && sub.view.progress > 0.35) {
      sub.confirm = 'waiting';
      sub.view.status = 'waiting';
      this.setAgent(agent, { status: 'waiting', last_action: this.tx(L('ממתין לאישור', 'Waiting for approval')), last_action_at: isoAt(now) });
      this.emitSub(run, sub);
      run.detail.status = 'waiting';
      this.emitTask(run);
      this.requestConfirmation(agent, run.detail.id, sub.tpl.confirm, (approved) => {
        sub.confirm = 'done';
        if (run.stopped) return;
        sub.view.status = 'running';
        run.detail.status = 'running';
        if (!approved) run.issues.push(this.tx(sub.tpl.confirm!.denied));
        this.setAgent(agent, { status: 'working', last_action: approved ? this.tx(L('האישור התקבל', 'Approval received')) : this.tx(sub.tpl.confirm!.denied), last_action_at: isoAt(Date.now()) });
        this.emitSub(run, sub);
        this.emitTask(run);
      });
      return;
    }

    const actions = sub.tpl.actions[this.lang];
    const index = Math.min(actions.length - 1, Math.floor(sub.view.progress * actions.length));
    if (actions.length && index > sub.actionIndex) {
      sub.actionIndex = index;
      this.setAgent(agent, { last_action: actions[index], last_action_at: isoAt(now) });
      this.timeline(run, 'action', actions[index], { agent, subtask: sub.view.id });
      this.log(`${agentLabel(agent, this.lang)}: ${actions[index]}`, 'info', { agent, task: run.detail.id });
    }

    if (sub.view.progress >= 1) {
      this.completeSub(run, sub);
      return;
    }
    const a = this.agents.get(agent)!;
    if (a.current?.subtask_id === sub.view.id) this.setAgent(agent, { progress: sub.view.progress, status: 'working' });
    this.emitSub(run, sub);
  }

  private completeSub(run: RunTask, sub: RunSub) {
    const now = Date.now();
    const agent = sub.view.agent;
    sub.view.status = 'completed';
    sub.view.progress = 1;
    sub.view.finished_at = isoAt(now);
    sub.view.summary = this.tx(sub.tpl.summary);
    sub.view.files_created = sub.tpl.files ?? [];
    this.emitSub(run, sub);
    const a = this.agents.get(agent)!;
    this.setAgent(agent, {
      status: 'completed',
      progress: 1,
      last_action: sub.view.summary,
      last_action_at: isoAt(now),
      stats: { ...a.stats, completed: a.stats.completed + 1 },
    });
    this.timeline(run, 'subtask.completed', L(`${agentLabel(agent, 'he')} סיים: ${pick(sub.tpl.title, 'he')}`, `${agentLabel(agent, 'en')} finished: ${pick(sub.tpl.title, 'en')}`), { agent, subtask: sub.view.id, level: 'success' });
    this.log(L(`${agentLabel(agent, 'he')} סיים: ${pick(sub.tpl.title, 'he')}`, `${agentLabel(agent, 'en')} finished: ${pick(sub.tpl.title, 'en')}`), 'success', { agent, task: run.detail.id });
    for (const path of sub.view.files_created) {
      const name = path.split('\\').pop() ?? path;
      this.files.unshift({ path, name, size: Math.round(rand(1200, 48000)), task_id: run.detail.id, agent_id: agent, created_at: isoAt(now) });
    }
    this.after(1500, () => {
      const cur = this.agents.get(agent);
      if (cur && cur.current?.subtask_id === sub.view.id) this.setAgent(agent, { status: 'idle', current: null, progress: 0 });
    });
  }

  private finish(run: RunTask) {
    if (run.timer) clearInterval(run.timer);
    run.timer = null;
    const { detail, tpl } = run;
    detail.status = 'verifying';
    detail.progress = 0.99;
    this.emitTask(run);
    this.timeline(run, 'verify', L('אימות התוצאות', 'Verifying results'));
    this.log(L('JARVIS מאמת את התוצאות', 'JARVIS is verifying the results'), 'info', { task: detail.id });
    this.after(1600, () => {
      if (run.stopped) return;
      const now = Date.now();
      const files = [...run.subs.values()].flatMap((s) => s.view.files_created);
      detail.status = 'completed';
      detail.progress = 1;
      detail.finished_at = isoAt(now);
      detail.summary = this.tx(tpl.result.summary);
      detail.result = {
        summary: detail.summary,
        details: tpl.result.details ? this.tx(tpl.result.details) : null,
        files_created: files,
        actions_taken: tpl.result.actions[this.lang],
        issues: [...tpl.result.issues[this.lang], ...run.issues],
        next_step: this.tx(tpl.result.next),
      };
      this.emitTask(run);
      this.timeline(run, 'completed', L('המשימה הושלמה', 'Task completed'), { level: 'success' });
      this.log(L(`המשימה הושלמה: ${detail.title}`, `Task completed: ${detail.title}`), 'success', { task: detail.id, kind: 'task' });
      const fileLines = files.length ? `\n\n${files.map((f) => `- \`${f.split('\\').pop()}\``).join('\n')}` : '';
      this.say('jarvis', 'result', `${detail.summary}${fileLines}`, { task_id: detail.id });
      this.notify('success', this.tx(L('המשימה הושלמה', 'Task completed')), detail.title);
      this.runs.delete(detail.id);
      if (this.opts.autoplay !== false) this.after(rand(30000, 42000), () => this.autoTask());
    });
  }

  private autoTask() {
    const busy = [...this.tasks.values()].some((t) => ACTIVE_TASK_STATUSES.includes(t.status));
    if (busy || this.killSwitch.global) {
      this.after(20000, () => this.autoTask());
      return;
    }
    this.startTemplate(this.nextTemplate(), 'voice');
  }

  private stopRun(taskId: string, status: 'cancelled' | 'failed', reason: Bi) {
    const run = this.runs.get(taskId);
    const detail = this.tasks.get(taskId);
    if (!detail) return;
    const now = Date.now();
    if (run) {
      run.stopped = true;
      if (run.timer) clearInterval(run.timer);
      for (const sub of run.subs.values()) {
        if (sub.view.status === 'running' || sub.view.status === 'waiting' || sub.view.status === 'queued' || sub.view.status === 'pending') {
          sub.view.status = 'cancelled';
          sub.view.finished_at = isoAt(now);
          this.emitSub(run, sub);
          const agent = this.agents.get(sub.view.agent);
          if (agent?.current?.task_id === taskId) this.setAgent(sub.view.agent, { status: 'idle', current: null, progress: 0 });
        }
      }
      for (const [id, c] of this.confirms) if (c.req.task_id === taskId) this.closeConfirmation(id, 'cancelled');
      this.runs.delete(taskId);
    }
    for (const agent of this.agents.values()) if (agent.current?.task_id === taskId) this.setAgent(agent.id, { status: 'idle', current: null, progress: 0 });
    detail.status = status;
    detail.finished_at = isoAt(now);
    detail.error = this.tx(reason);
    detail.summary = this.tx(reason);
    detail.updated_at = isoAt(now);
    this.emit('task.updated', this.summary(detail));
    const entry = { ts: isoAt(now), time: hmsAt(now), kind: status, message: this.tx(reason), agent_id: null, subtask_id: null, level: 'warning' };
    detail.timeline.push(entry);
    this.emit('task.timeline', { task_id: taskId, entry });
    this.log(L(`המשימה בוטלה: ${detail.title}`, `Task cancelled: ${detail.title}`), 'warning', { task: taskId, kind: 'task' });
  }

  /** Resume an interrupted task, or retry the failed subtasks of a failed one. */
  private revive(detail: TaskDetail, mode: 'resume' | 'retry') {
    const subs = detail.plan?.subtasks ?? [];
    const tpl: TaskTemplate = {
      key: 'revived',
      title: L(detail.title, detail.title),
      goal: L(detail.goal, detail.goal),
      strategy: L(detail.plan?.strategy ?? '', detail.plan?.strategy ?? ''),
      priority: detail.priority,
      subtasks: subs.map((s) => ({
        key: s.id.split('.').pop() ?? s.id,
        agent: s.agent,
        title: L(s.title, s.title),
        instruction: L(s.instruction, s.instruction),
        depends: s.depends_on.map((d) => d.split('.').pop() ?? d),
        seconds: 7,
        actions: LL([s.title], [s.title]),
        summary: L(s.summary ?? s.title, s.summary ?? s.title),
        files: s.files_created,
      })),
      result: {
        summary: L(`המשימה „${detail.title}” הושלמה.`, `“${detail.title}” is complete.`),
        details: null,
        actions: LL(subs.map((s) => s.title), subs.map((s) => s.title)),
        issues: LL([], []),
        next: L('אין צעדים נוספים.', 'No further steps.'),
      },
    };
    const run: RunTask = { detail, tpl, subs: new Map(), timer: null, stopped: false, issues: [] };
    subs.forEach((view, i) => {
      if (view.status !== 'completed') {
        view.status = 'pending';
        view.error = null;
        view.finished_at = null;
        if (mode === 'retry') view.progress = 0;
      }
      run.subs.set(view.id, { view, tpl: tpl.subtasks[i], failedOnce: true, pausedUntil: 0, confirm: 'done', actionIndex: 0 });
    });
    detail.status = 'running';
    detail.finished_at = null;
    detail.error = null;
    this.runs.set(detail.id, run);
    this.emitTask(run);
    for (const sub of run.subs.values()) this.emitSub(run, sub);
    this.timeline(run, mode, mode === 'resume' ? L('המשימה ממשיכה מהנקודה שבה נעצרה', 'Resuming from where it stopped') : L('ניסיון חוזר לתתי-המשימות שנכשלו', 'Retrying the failed subtasks'), { level: 'info' });
    this.log(mode === 'resume' ? L(`ממשיך משימה: ${detail.title}`, `Resuming task: ${detail.title}`) : L(`ניסיון חוזר: ${detail.title}`, `Retrying: ${detail.title}`), 'info', { task: detail.id, kind: 'task' });
    run.timer = setInterval(() => this.tick(run), 700);
  }

  // ------------------------------------------------------------------ confirmations

  private requestConfirmation(agent: AgentId, taskId: string | null, spec: ConfirmSpec, onDone: (approved: boolean) => void, ttl = 45) {
    const now = Date.now();
    const req: ConfirmationRequest = {
      id: mockId('cfm'),
      agent_id: agent,
      task_id: taskId,
      tool: spec.tool,
      summary: this.tx(spec.summary),
      reasons: spec.reasons[this.lang],
      risk: spec.risk,
      details: spec.details,
      created_at: isoAt(now),
      expires_at: isoAt(now + ttl * 1000),
      status: 'pending',
    };
    const pending: PendingConfirm = {
      req,
      timer: null,
      resolve: (approved) => {
        this.recordAudit(agent, spec.tool, spec.capability, req.summary, spec.risk, approved ? 'confirmed' : 'rejected', approved ? 'ok' : 'skipped', req.reasons.join('; '), taskId);
        onDone(approved);
      },
    };
    pending.timer = setTimeout(() => {
      if (!this.confirms.has(req.id)) return;
      this.closeConfirmation(req.id, 'expired');
      this.log(L('לא התקבלה תשובה — הבקשה נדחתה אוטומטית', 'No answer — the request was denied automatically'), 'warning', { agent, task: taskId });
      pending.resolve(false);
    }, ttl * 1000);
    this.confirms.set(req.id, pending);
    this.emit('confirm.request', req);
    this.log(L(`נדרש אישור: ${req.summary}`, `Approval needed: ${req.summary}`), 'warning', { agent, task: taskId, kind: 'confirm' });
  }

  private closeConfirmation(id: string, status: ConfirmationRequest['status']) {
    const pending = this.confirms.get(id);
    if (!pending) return null;
    if (pending.timer) clearTimeout(pending.timer);
    this.confirms.delete(id);
    const req = { ...pending.req, status };
    this.emit('confirm.response', req);
    return pending;
  }

  answerConfirmation(id: string, approved: boolean, remember: boolean): { ok: boolean } {
    const pending = this.closeConfirmation(id, approved ? 'approved' : 'denied');
    if (!pending) throw new NotFoundError('Confirmation not found or already answered');
    const req = pending.req;
    this.log(
      approved ? L(`אושר: ${req.summary}`, `Approved: ${req.summary}`) : L(`נדחה: ${req.summary}`, `Denied: ${req.summary}`),
      approved ? 'success' : 'warning',
      { agent: req.agent_id, task: req.task_id, kind: 'confirm' },
    );
    if (approved && remember && req.task_id) {
      const grant: Grant = {
        id: mockId('grant'),
        agent_id: req.agent_id,
        capability: String((req.details.capability as string) ?? 'shell.execute'),
        roots: [],
        task_id: req.task_id,
        expires_at: null,
        max_uses: null,
        uses: 0,
        source: 'user',
        reason: `Remembered approval: ${req.summary}`,
        created_at: isoAt(Date.now()),
      };
      this.grants.push(grant);
    }
    pending.resolve(approved);
    return { ok: true };
  }

  // ------------------------------------------------------------------ voice

  private startLevels(state: 'listening' | 'speaking' | 'wake') {
    this.stopLevels();
    const t0 = performance.now();
    this.levelTimer = setInterval(() => {
      const t = (performance.now() - t0) / 1000;
      const speaking = state === 'speaking';
      const base = speaking ? 0.22 : 0.16;
      const env = Math.abs(Math.sin(t * (speaking ? 8.1 : 6.3))) * (0.55 + 0.45 * Math.sin(t * 1.9 + 0.6));
      const level = clamp(base + env * (speaking ? 0.52 : state === 'wake' ? 0.3 : 0.58) + rand(-0.06, 0.1), 0.02, 1);
      this.emit('voice.state', { state, level: Math.round(level * 100) / 100, muted: this.voice.muted });
    }, 1000 / LEVEL_HZ);
  }

  private stopLevels() {
    if (this.levelTimer) clearInterval(this.levelTimer);
    this.levelTimer = null;
  }

  cancelVoice(silent = false) {
    for (const t of this.voiceTimers) clearTimeout(t);
    this.voiceTimers.clear();
    this.stopLevels();
    this.voiceBusy = false;
    if (!silent && this.voice.state !== 'disabled') this.setVoice('idle');
  }

  private voiceLoop() {
    const blocked = this.scene || this.voiceBusy || this.killSwitch.global || !this.voice.enabled || this.voice.muted || this.voice.state === 'disabled';
    if (!blocked) {
      const script = VOICE_SCRIPTS[this.voiceCycle % VOICE_SCRIPTS.length];
      this.voiceCycle += 1;
      this.runVoiceScript(script, 'wake');
    }
    this.after(rand(24000, 32000), () => this.voiceLoop());
  }

  private pipe(step: PipelineEvent['step'], status: PipelineEvent['status']) {
    this.emit('voice.pipeline', { step, status, label: this.tx(STEP_LABELS[step]) });
  }

  /** Wake → listening (partial transcripts) → processing → action → speaking → idle. */
  runVoiceScript(script: VoiceScript, start: 'wake' | 'listening' | 'processing') {
    this.cancelVoice(true);
    this.voiceBusy = true;
    const lang = this.lang;
    const words = pick(script.transcript, lang).split(' ');
    let t = 0;
    const at = (ms: number, fn: () => void) => this.after(ms, fn, true);
    if (start === 'wake') {
      this.setVoice('wake');
      t += 1150;
    }
    if (start !== 'processing') {
      at(t, () => {
        this.setVoice('listening', 0.2);
        this.startLevels('listening');
      });
      words.forEach((_, i) => at(t + 450 + i * 360, () => this.emit('voice.transcript', { text: words.slice(0, i + 1).join(' '), final: false, language: lang })));
      t += 450 + words.length * 360 + 450;
    }
    this.continueVoiceScript(script, t);
  }

  private continueVoiceScript(script: VoiceScript, t0: number) {
    const lang = this.lang;
    const text = pick(script.transcript, lang);
    let t = t0;
    const at = (ms: number, fn: () => void) => this.after(ms, fn, true);
    at(t, () => {
      this.stopLevels();
      this.emit('voice.transcript', { text, final: true, language: lang });
      this.voice.last_transcript = text;
      this.setVoice('processing');
      this.pipe('transcribing', 'active');
      this.say('user', 'answer', text, { source: 'voice', intent: script.intent });
      this.log(L(`פקודה קולית: „${pick(script.transcript, 'he')}”`, `Voice command: “${pick(script.transcript, 'en')}”`), 'info', { kind: 'voice' });
    });
    const steps: PipelineEvent['step'][] = ['transcribing', 'understanding', 'routing', 'selecting_tool', 'preparing_action'];
    steps.forEach((step, i) => {
      at(t + 280 + i * 330, () => {
        this.pipe(step, 'done');
        if (i + 1 < steps.length) this.pipe(steps[i + 1], 'active');
      });
    });
    t += 280 + steps.length * 330;
    let reply = pick(script.reply, lang);
    if (script.intent === 'system_status') reply = reply.replace('{cpu}', String(Math.round(this.sys.cpu))).replace('{mem}', String(Math.round(this.sys.mem)));
    if (script.action) {
      const actionId = mockId('act');
      const title = pick(script.action.title, lang);
      const base: ActionEvent = { id: actionId, title, icon: script.action.icon, progress: 0, status: 'running' };
      at(t, () => {
        this.pipe('executing', 'active');
        this.emit('action', base);
        this.setAgent('system', { status: 'working', current: null, progress: 0.2, last_action: title, last_action_at: isoAt(Date.now()) });
      });
      for (let i = 1; i <= 6; i += 1) at(t + i * 230, () => this.emit('action', { ...base, progress: Math.min(script.fails ? 0.55 : 1, i / 6) }));
      t += 6 * 230 + 200;
      if (script.fails) {
        const fails = script.fails;
        at(t, () => {
          this.emit('action', { ...base, title: pick(fails.title, lang), detail: pick(fails.detail, lang), progress: 0.55, status: 'error' });
          this.pipe('executing', 'error');
          this.voice.error = pick(fails.title, lang);
          this.setVoice('error');
          const sys = this.agents.get('system')!;
          this.setAgent('system', { status: 'error', progress: 0, errors: [{ at: isoAt(Date.now()), message: pick(fails.title, lang) }, ...sys.errors].slice(0, 10), last_action: pick(fails.title, lang), last_action_at: isoAt(Date.now()) });
          this.recordAudit('system', 'process.open', 'process.launch', 'Open Google Chrome', 'low', 'allowed', 'error', 'chrome.exe exited with code 1');
          this.log(L('שגיאה: לא הצלחתי לפתוח את כרום', 'Error: could not open Chrome'), 'error', { agent: 'system', kind: 'voice' });
          this.say('jarvis', 'error', `${pick(fails.title, lang)} ${pick(fails.detail, lang)}`, { source: 'voice' });
          this.emit('speech', { text: pick(fails.title, lang), language: lang });
        });
        at(t + 3600, () => {
          this.voice.error = null;
          this.setVoice('idle');
          this.setAgent('system', { status: 'idle' });
          this.voiceBusy = false;
        });
        return;
      }
      at(t, () => {
        this.emit('action', { ...base, progress: 1, status: 'done' });
        this.pipe('executing', 'done');
        this.pipe('done', 'done');
        const sys = this.agents.get('system')!;
        this.setAgent('system', { status: 'idle', progress: 0, last_action: title.replace(/…$/, ''), last_action_at: isoAt(Date.now()), stats: { ...sys.stats, completed: sys.stats.completed + 1 } });
        this.recordAudit('system', script.action!.icon === 'youtube' ? 'browser.open_url' : 'process.open', 'process.launch', title.replace(/…$/, ''), 'low', 'allowed', 'ok');
        this.log(L(`בוצע: ${pick(script.action!.title, 'he').replace(/…$/, '')}`, `Done: ${pick(script.action!.title, 'en').replace(/…$/, '')}`), 'success', { agent: 'system', kind: 'action' });
      });
      t += 450;
    } else {
      at(t, () => {
        this.pipe('executing', 'active');
      });
      at(t + 500, () => {
        this.pipe('executing', 'done');
        this.pipe('done', 'done');
      });
      t += 700;
    }
    at(t, () => {
      this.say('jarvis', 'answer', reply, { source: 'voice', intent: script.intent });
      if (this.settings.voice?.speak_responses !== false) {
        this.emit('speech', { text: reply, language: lang });
        this.setVoice('speaking', 0.3);
        this.startLevels('speaking');
      }
    });
    t += Math.max(1400, 500 + reply.length * 55);
    at(t, () => {
      this.stopLevels();
      this.setVoice('idle');
      this.voiceBusy = false;
    });
  }

  /** Freeze the voice overlay on one scene (for screenshots). `live`/null resumes the loop. */
  setScene(name: string | null) {
    this.cancelVoice(true);
    const lang = this.lang;
    const scene = name && name !== 'live' ? name : null;
    this.scene = scene;
    const script = VOICE_SCRIPTS[0];
    const transcript = pick(script.transcript, lang);
    switch (scene) {
      case null:
        this.setVoice('idle');
        return;
      case 'idle':
        this.setVoice('idle');
        return;
      case 'wake':
        this.setVoice('idle');
        this.setVoice('wake');
        this.startLevels('wake');
        return;
      case 'listening':
        this.setVoice('wake');
        this.setVoice('listening', 0.3);
        this.emit('voice.transcript', { text: transcript, final: false, language: lang });
        this.startLevels('listening');
        return;
      case 'processing':
        this.setVoice('wake');
        this.setVoice('listening');
        this.emit('voice.transcript', { text: transcript, final: true, language: lang });
        this.setVoice('processing');
        this.pipe('transcribing', 'done');
        this.pipe('understanding', 'done');
        this.pipe('routing', 'done');
        this.pipe('selecting_tool', 'active');
        return;
      case 'action':
        this.setVoice('wake');
        this.setVoice('listening');
        this.emit('voice.transcript', { text: transcript, final: true, language: lang });
        this.setVoice('processing');
        for (const step of ['transcribing', 'understanding', 'routing', 'selecting_tool', 'preparing_action'] as const) this.pipe(step, 'done');
        this.pipe('executing', 'active');
        this.emit('action', { id: 'scene-action', title: pick(script.action!.title, lang), icon: 'chrome', progress: 0.62, status: 'running' });
        return;
      case 'speaking':
        this.setVoice('wake');
        this.setVoice('processing');
        this.emit('speech', { text: pick(script.reply, lang), language: lang });
        this.setVoice('speaking', 0.4);
        this.startLevels('speaking');
        return;
      case 'error': {
        const fails = VOICE_SCRIPTS[3].fails!;
        this.setVoice('wake');
        this.setVoice('processing');
        this.emit('action', { id: 'scene-error', title: pick(fails.title, lang), detail: pick(fails.detail, lang), icon: 'chrome', progress: 0.55, status: 'error' });
        this.setVoice('error');
        return;
      }
      default:
        this.setVoice('idle');
    }
  }

  /** Ad-hoc events for demos and tests (window.jarvisMock.trigger). */
  trigger(name: string) {
    switch (name) {
      case 'confirm':
        this.requestConfirmation(
          'system',
          null,
          {
            tool: 'fs.delete',
            capability: 'fs.delete',
            risk: 'high',
            summary: L('מחיקת 37 קבצים זמניים מתיקיית ההורדות', 'Delete 37 temporary files in Downloads'),
            reasons: LL(['מחיקה של קבצים רבים', 'מחוץ לסביבת העבודה'], ['Deletes many files', 'Outside the workspace']),
            details: { path: 'C:\\Users\\Dana\\Downloads\\Temp', files: 37, recycle_bin: true, capability: 'fs.delete' },
            denied: L('המחיקה נדחתה', 'Deletion denied'),
          },
          () => undefined,
          60,
        );
        return;
      case 'notification': {
        const n = NOTIFICATIONS[Math.floor(Math.random() * NOTIFICATIONS.length)];
        this.notify(n.level, this.tx(n.title), this.tx(n.message));
        return;
      }
      case 'task':
        this.startTemplate(this.nextTemplate(), 'text');
        return;
      case 'error':
        this.runVoiceScript(VOICE_SCRIPTS[3], 'listening');
        return;
      case 'voice':
        this.runVoiceScript(VOICE_SCRIPTS[this.voiceCycle++ % VOICE_SCRIPTS.length], 'wake');
        return;
      case 'kill':
        this.engageKill(undefined, 'demo');
        return;
      default:
        return;
    }
  }

  private notifyLoop() {
    if (!this.scene) this.trigger('notification');
    this.after(rand(55000, 80000), () => this.notifyLoop());
  }

  // ------------------------------------------------------------------ commands

  command(text: string, source: 'text' | 'voice' = 'text'): CommandResponse {
    const clean = text.trim();
    const lang: Lang = hasHebrew(clean) ? 'he' : this.lang;
    const user = this.say('user', 'answer', clean, { source, language: hasHebrew(clean) ? 'he' : 'en' });
    const T = (b: Bi) => pick(b, lang);
    const reply = (kind: ChatMessage['kind'], msg: string, delay = 650, extra: Partial<ChatMessage> = {}) => this.after(delay, () => this.say('jarvis', kind, msg, extra));
    const respond = (intent: string, complexity: string, agents: AgentId[], task_id?: string): CommandResponse => ({
      accepted: true,
      message: clone(user),
      intent: { name: intent, complexity, agents },
      ...(task_id ? { task_id } : {}),
    });

    if (/(עצור הכו?ל|stop (everything|all)|kill switch)/i.test(clean)) {
      this.engageKill(undefined, T(L('פקודת משתמש', 'user command')));
      reply('answer', T(L('עצרתי את כל הסוכנים. אפשר לשחרר מהמסך הראשי.', 'All agents are stopped. You can release from the main screen.')), 300);
      return respond('kill_switch', 'simple', []);
    }
    const modeMatch = clean.match(/(?:מצב|mode)\s*(שיחה|רגיל|משימה|אוטונומי|דיבאג|chat|normal|task|autonomous|debug)/i);
    if (modeMatch) {
      const map: Record<string, Mode> = { שיחה: 'chat', רגיל: 'normal', משימה: 'task', אוטונומי: 'autonomous', דיבאג: 'debug' };
      const mode = (map[modeMatch[1]] ?? modeMatch[1].toLowerCase()) as Mode;
      this.setMode(mode);
      reply('info', T(L(`עברתי למצב ${modeMatch[1]}.`, `Switched to ${mode} mode.`)), 300);
      return respond('set_mode', 'simple', []);
    }
    if (/(שגיאה|simulate error|error demo)/i.test(clean)) {
      this.runVoiceScript(VOICE_SCRIPTS[3], 'processing');
      return respond('open_app', 'simple', ['system']);
    }
    if (this.mode === 'chat' && /(פתח|תפתח|הפעל|open|launch|בנה|צור|build|create)/i.test(clean)) {
      reply('info', T(L('במצב שיחה אני לא מבצע פעולות. אפשר לעבור למצב רגיל.', 'In chat mode I don’t take actions. Switch to normal mode to run it.')));
      return respond('chat', 'simple', []);
    }
    const open = clean.match(/^(?:תפתח|פתח|הפעל|open|launch|start)\s+(?:לי\s+)?(?:את\s+)?(.+)$/i);
    if (open) {
      const target = open[1].replace(/[?.!]+$/, '');
      const app = APP_NAMES.find((a) => a.match.test(target));
      const name = app?.name ?? target;
      const script: VoiceScript = {
        transcript: L(clean, clean),
        intent: 'open_app',
        action: { title: L(`פותח את ${name}…`, `Opening ${name}…`), icon: app?.icon ?? 'app' },
        reply: L(`פתחתי את ${name}.`, `${name} is open.`),
      };
      this.runTextScript(script);
      return respond('open_app', 'simple', ['system']);
    }
    if (/(מצב המערכת|מצב מערכת|system status|how is the (pc|computer|system))/i.test(clean)) {
      const stats = this.systemStats();
      reply('answer', T(L(
        `המעבד ב-${Math.round(stats.cpu.percent)}%, הזיכרון ב-${Math.round(stats.memory.percent)}% (${stats.memory.used_gb.toFixed(1)} מתוך ${stats.memory.total_gb}GB), ובדיסק C נשארו ${stats.disks[0].total_gb - stats.disks[0].used_gb}GB פנויים.`,
        `CPU is at ${Math.round(stats.cpu.percent)}%, memory at ${Math.round(stats.memory.percent)}% (${stats.memory.used_gb.toFixed(1)} of ${stats.memory.total_gb} GB), and drive C has ${stats.disks[0].total_gb - stats.disks[0].used_gb} GB free.`,
      )));
      return respond('system_status', 'simple', ['system']);
    }
    if (/(בנה|צור|תבנה|תכין|הכן|חקור|תחקור|תכנן|ארגן|כתוב|השווה|build|create|make|research|prepare|plan|write|organize|compare)/i.test(clean)) {
      const known = TASK_TEMPLATES.find((tpl) => (tpl.key === 'landing' && /(נחיתה|landing)/i.test(clean)) || (tpl.key === 'headphones' && /(אוזניות|headphones)/i.test(clean)) || (tpl.key === 'organizer' && /(הורדות|downloads)/i.test(clean)));
      const creative = /(מצגת|עיצוב|לוגו|תמונה|presentation|slides|design|logo|image|poster)/i.test(clean);
      const tpl = known ?? GENERIC_TEMPLATE(clean.length > 64 ? `${clean.slice(0, 61)}…` : clean, creative);
      reply('ack', T(L('קיבלתי. אני מחלק את המשימה בין הסוכנים.', 'On it. I’m splitting the task between the agents.')), 350);
      const taskId = this.startTemplate(tpl, source);
      return respond('complex_task', 'complex', unique(tpl.subtasks.map((s) => s.agent)), taskId);
    }
    if (/(מה השעה|what time)/i.test(clean)) {
      reply('answer', T(L(`השעה ${hmsAt(Date.now()).slice(0, 5)}.`, `It’s ${hmsAt(Date.now()).slice(0, 5)}.`)));
      return respond('time', 'simple', []);
    }
    if (/^(שלום|היי|הי|בוקר טוב|ערב טוב|hello|hi|hey|good (morning|evening))\b/i.test(clean)) {
      const name = String(this.settings.general?.user_name ?? '');
      reply('answer', T(L(`שלום${name ? ` ${name}` : ''}! כל המערכות פועלות. במה אוכל לעזור?`, `Hello${name ? ` ${name}` : ''}! All systems are online. How can I help?`)));
      return respond('greeting', 'simple', []);
    }
    const replies = GENERIC_REPLIES[lang];
    reply('answer', replies[Math.floor(Math.random() * replies.length)], 900);
    return respond('chat', 'simple', []);
  }

  /** Text fast path: no wake/listening, but the orb shows steps, the action card and speech. */
  private runTextScript(script: VoiceScript) {
    this.cancelVoice(true);
    this.voiceBusy = true;
    this.setVoice('processing');
    this.pipe('understanding', 'active');
    this.continueVoiceSteps(script, 250);
  }

  private continueVoiceSteps(script: VoiceScript, t0: number) {
    // Reuse the voice script after the transcript stage (the user message is already in chat).
    const lang = this.lang;
    let t = t0;
    const at = (ms: number, fn: () => void) => this.after(ms, fn, true);
    const steps: PipelineEvent['step'][] = ['understanding', 'routing', 'selecting_tool', 'preparing_action'];
    steps.forEach((step, i) => at(t + i * 260, () => {
      this.pipe(step, 'done');
      if (i + 1 < steps.length) this.pipe(steps[i + 1], 'active');
    }));
    t += steps.length * 260;
    const action = script.action!;
    const title = pick(action.title, lang);
    const base: ActionEvent = { id: mockId('act'), title, icon: action.icon, progress: 0, status: 'running' };
    at(t, () => {
      this.pipe('executing', 'active');
      this.emit('action', base);
    });
    for (let i = 1; i <= 5; i += 1) at(t + i * 220, () => this.emit('action', { ...base, progress: i / 5 }));
    t += 5 * 220 + 150;
    const reply = pick(script.reply, lang);
    at(t, () => {
      this.emit('action', { ...base, progress: 1, status: 'done' });
      this.pipe('executing', 'done');
      this.pipe('done', 'done');
      this.recordAudit('system', 'process.open', 'process.launch', title.replace(/…$/, ''), 'low', 'allowed', 'ok');
      this.log(L(`בוצע: ${title.replace(/…$/, '')}`, `Done: ${title.replace(/…$/, '')}`), 'success', { agent: 'system', kind: 'action' });
      this.say('jarvis', 'answer', reply, { intent: script.intent });
      if (this.settings.voice?.speak_responses !== false && this.voice.enabled) {
        this.emit('speech', { text: reply, language: lang });
        this.setVoice('speaking', 0.3);
        this.startLevels('speaking');
      }
    });
    t += Math.max(1300, 400 + reply.length * 50);
    at(t, () => {
      this.stopLevels();
      this.setVoice(this.voice.enabled ? 'idle' : 'disabled');
      this.voiceBusy = false;
    });
  }

  // ------------------------------------------------------------------ REST handlers

  setMode(mode: Mode): { mode: Mode } {
    if (!MODES.includes(mode)) throw new Error(`Unknown mode ${mode}`);
    this.mode = mode;
    this.emit('mode', { mode });
    this.log(L(`מצב העבודה שונה ל-${mode.toUpperCase()}`, `Mode changed to ${mode.toUpperCase()}`), 'info', { kind: 'mode' });
    return { mode };
  }

  engageKill(agentId: AgentId | undefined, reason: string | undefined): KillSwitchState {
    if (agentId) {
      this.killSwitch = { ...this.killSwitch, agents: { ...this.killSwitch.agents, [agentId]: reason || 'user' } };
      this.pauseAgentWork(agentId);
      this.setAgent(agentId, { killed: true, status: 'offline', current: null, progress: 0 });
      this.log(L(`עצירת חירום הופעלה עבור ${agentLabel(agentId, 'he')}`, `Kill switch engaged for ${agentLabel(agentId, 'en')}`), 'error', { agent: agentId, kind: 'killswitch' });
    } else {
      this.killSwitch = { ...this.killSwitch, global: true };
      for (const run of [...this.runs.values()]) this.stopRun(run.detail.id, 'cancelled', L('נעצר בעצירת חירום', 'Stopped by the kill switch'));
      for (const id of [...this.confirms.keys()]) this.closeConfirmation(id, 'cancelled');
      this.cancelVoice();
      for (const agent of this.agents.values()) this.setAgent(agent.id, { status: 'idle', current: null, progress: 0 });
      this.log(L('עצירת חירום גלובלית — כל הסוכנים נעצרו', 'GLOBAL KILL SWITCH — all agents stopped'), 'error', { kind: 'killswitch' });
      this.notify('warning', this.tx(L('עצירת חירום פעילה', 'Kill switch engaged')), this.tx(L('כל הסוכנים נעצרו והכלים חסומים.', 'All agents are stopped and tools are blocked.')));
    }
    this.emit('killswitch', this.killSwitch);
    return clone(this.killSwitch);
  }

  releaseKill(agentId?: AgentId): KillSwitchState {
    if (agentId) {
      const agents = { ...this.killSwitch.agents };
      delete agents[agentId];
      this.killSwitch = { ...this.killSwitch, agents };
      this.setAgent(agentId, { killed: false, status: this.agents.get(agentId)?.enabled ? 'idle' : 'offline' });
      this.log(L(`עצירת החירום שוחררה עבור ${agentLabel(agentId, 'he')}`, `Kill switch released for ${agentLabel(agentId, 'en')}`), 'success', { agent: agentId, kind: 'killswitch' });
    } else {
      this.killSwitch = { ...this.killSwitch, global: false };
      this.log(L('עצירת החירום שוחררה — המערכת חזרה לפעולה', 'Kill switch released — back online'), 'success', { kind: 'killswitch' });
      if (this.opts.autoplay !== false) this.after(25000, () => this.autoTask());
    }
    this.emit('killswitch', this.killSwitch);
    return clone(this.killSwitch);
  }

  private pauseAgentWork(agentId: AgentId) {
    for (const run of this.runs.values()) {
      for (const sub of run.subs.values()) {
        if (sub.view.agent === agentId && (sub.view.status === 'running' || sub.view.status === 'waiting')) {
          sub.view.status = 'queued';
          this.emitSub(run, sub);
        }
      }
    }
  }

  agentAction(id: AgentId, action: AgentAction): AgentView {
    const agent = this.agents.get(id);
    if (!agent) throw new NotFoundError(`Unknown agent ${id}`);
    const name = (lang: Lang) => agentLabel(id, lang);
    switch (action) {
      case 'start':
        this.setAgent(id, { enabled: true, status: agent.killed ? 'offline' : 'idle' });
        this.log(L(`${name('he')} הופעל`, `${name('en')} started`), 'success', { agent: id, kind: 'agent' });
        break;
      case 'stop':
        this.pauseAgentWork(id);
        this.setAgent(id, { enabled: false, status: 'offline', current: null, progress: 0 });
        this.log(L(`${name('he')} נעצר`, `${name('en')} stopped`), 'warning', { agent: id, kind: 'agent' });
        break;
      case 'restart':
        this.pauseAgentWork(id);
        this.setAgent(id, { status: 'offline', current: null, progress: 0 });
        this.log(L(`${name('he')} מופעל מחדש`, `${name('en')} restarting`), 'info', { agent: id, kind: 'agent' });
        this.after(1200, () => this.setAgent(id, { enabled: true, status: this.agents.get(id)?.killed ? 'offline' : 'idle', errors: [] }));
        break;
      case 'kill':
        this.engageKill(id, 'user');
        break;
      case 'release':
        this.releaseKill(id);
        break;
    }
    return clone(this.agents.get(id)!);
  }

  assignTask(id: AgentId, instruction: string, priority?: Priority): { task_id: string } {
    if (!this.agents.has(id)) throw new NotFoundError(`Unknown agent ${id}`);
    const title = instruction.length > 64 ? `${instruction.slice(0, 61)}…` : instruction;
    const tpl: TaskTemplate = {
      key: 'assigned',
      title: L(title, title),
      goal: L(instruction, instruction),
      strategy: L(`הקצאה ישירה ל${agentLabel(id, 'he')}`, `Direct assignment to the ${agentLabel(id, 'en')}`),
      priority: priority ?? 'normal',
      subtasks: [
        {
          key: 'work',
          agent: id,
          title: L(title, title),
          instruction: L(instruction, instruction),
          depends: [],
          seconds: 10,
          actions: LL(['מתחיל לעבוד', 'מעבד', 'מסכם'], ['Getting started', 'Working on it', 'Wrapping up']),
          summary: L('בוצע.', 'Done.'),
        },
      ],
      result: { summary: L(`בוצע: ${title}`, `Done: ${title}`), details: null, actions: LL([title], [title]), issues: LL([], []), next: L('—', '—') },
    };
    return { task_id: this.startTemplate(tpl, 'api', { skipPlanning: true, priority }) };
  }

  cancelTask(id: string): TaskSummary {
    const task = this.tasks.get(id);
    if (!task) throw new NotFoundError('Task not found');
    if (ACTIVE_TASK_STATUSES.includes(task.status)) {
      this.stopRun(id, 'cancelled', L('המשימה בוטלה על ידי המשתמש', 'Task cancelled by the user'));
      this.say('jarvis', 'info', this.tx(L(`ביטלתי את המשימה „${task.title}”.`, `I cancelled “${task.title}”.`)), { task_id: id });
    }
    return this.summary(clone(task));
  }

  resumeTask(id: string): TaskSummary {
    const task = this.tasks.get(id);
    if (!task) throw new NotFoundError('Task not found');
    if (task.status === 'interrupted' || task.status === 'cancelled') this.revive(task, 'resume');
    return this.summary(clone(task));
  }

  retryTask(id: string): TaskSummary {
    const task = this.tasks.get(id);
    if (!task) throw new NotFoundError('Task not found');
    if (task.status === 'failed') this.revive(task, 'retry');
    return this.summary(clone(task));
  }

  getTasks(status?: string, limit = 50): TaskSummary[] {
    return [...this.tasks.values()]
      .filter((t) => !status || t.status === status)
      .sort((a, b) => (a.created_at < b.created_at ? 1 : -1))
      .slice(0, limit)
      .map((t) => this.summary(clone(t)));
  }

  getTask(id: string): TaskDetail {
    const task = this.tasks.get(id);
    if (!task) throw new NotFoundError('Task not found');
    return clone(task);
  }

  voiceAction(action: VoiceAction): VoiceInfo {
    switch (action) {
      case 'start':
        this.voice = { ...this.voice, enabled: true };
        this.setVoice('idle');
        this.log(L('הקול הופעל', 'Voice turned on'), 'info', { kind: 'voice' });
        break;
      case 'stop':
        this.cancelVoice(true);
        this.voice = { ...this.voice, enabled: false };
        this.setVoice('disabled');
        this.log(L('הקול כובה', 'Voice turned off'), 'info', { kind: 'voice' });
        break;
      case 'mute':
      case 'unmute':
        this.voice = { ...this.voice, muted: action === 'mute' };
        this.emit('voice.state', { state: this.voice.state, muted: this.voice.muted });
        break;
      case 'ptt_start': {
        if (this.scene) this.scene = null;
        this.cancelVoice(true);
        this.voiceBusy = true;
        this.setVoice('listening', 0.25);
        this.startLevels('listening');
        const script = VOICE_SCRIPTS[this.voiceCycle % VOICE_SCRIPTS.length];
        const words = pick(script.transcript, this.lang).split(' ');
        words.forEach((_, i) => this.after(500 + i * 380, () => this.emit('voice.transcript', { text: words.slice(0, i + 1).join(' '), final: false, language: this.lang }), true));
        break;
      }
      case 'ptt_stop': {
        if (this.voice.state !== 'listening') break;
        const script = VOICE_SCRIPTS[this.voiceCycle % VOICE_SCRIPTS.length];
        this.voiceCycle += 1;
        for (const t of this.voiceTimers) clearTimeout(t);
        this.voiceTimers.clear();
        this.continueVoiceScript(script, 120);
        break;
      }
      case 'cancel':
        this.cancelVoice();
        break;
    }
    return clone(this.voice);
  }

  speak(text: string) {
    const lang: Lang = hasHebrew(text) ? 'he' : 'en';
    this.cancelVoice(true);
    this.voiceBusy = true;
    this.emit('speech', { text, language: lang });
    this.setVoice('speaking', 0.3);
    this.startLevels('speaking');
    this.after(Math.max(1400, 500 + text.length * 55), () => {
      this.stopLevels();
      this.setVoice(this.voice.enabled ? 'idle' : 'disabled');
      this.voiceBusy = false;
    }, true);
    return { ok: true };
  }

  patchSettings(patch: DeepPartialSettings): SettingsObject {
    const merged = deepMerge(this.settings as Record<string, unknown>, patch as Record<string, unknown>) as SettingsObject;
    this.settings = merged;
    if (patch.ui) {
      this.ui = { ...this.ui, ...(patch.ui as Partial<UISettings>) };
      this.opts.saveUi?.(this.ui);
      this.emit('settings', { ui: this.ui });
    }
    if (patch.voice && typeof patch.voice.enabled === 'boolean' && patch.voice.enabled !== this.voice.enabled) this.voiceAction(patch.voice.enabled ? 'start' : 'stop');
    if (patch.ai) this.refreshProviders();
    return clone(this.settings);
  }

  putSecret(name: string, value: string) {
    if (!/^[A-Z][A-Z0-9_]{1,63}$/.test(name)) throw new Error('Invalid secret name');
    if (!value.trim()) throw new Error('Empty value');
    const known = this.secrets[name];
    this.secrets[name] = { label: known?.label ?? name, configured: true, source: 'keyring' };
    this.log(L(`המפתח ${name} נשמר`, `Secret ${name} saved`), 'success', { kind: 'secrets' });
    this.refreshProviders();
    return { configured: true, source: 'keyring' };
  }

  deleteSecret(name: string) {
    const known = this.secrets[name];
    if (known) this.secrets[name] = { ...known, configured: false, source: null };
    this.log(L(`המפתח ${name} הוסר`, `Secret ${name} removed`), 'warning', { kind: 'secrets' });
    this.refreshProviders();
    return { configured: false };
  }

  permissions() {
    const agents: Record<string, unknown> = {};
    for (const agent of this.agents.values()) {
      const r = this.restrictions[agent.id];
      agents[agent.id] = {
        capabilities: [...agent.permissions].sort(),
        grants: this.grants.filter((g) => g.agent_id === agent.id || g.agent_id === '*'),
        restricted_to_task: r?.task ?? null,
        folder_scope: r?.folders ?? null,
        risk_ceiling: RISK_CEILINGS[agent.id],
      };
    }
    return clone({ agents, capabilities: CAPABILITIES });
  }

  grant(req: GrantRequest): Grant {
    if (!this.agents.has(req.agent_id)) throw new NotFoundError('Unknown agent');
    if (!CAPABILITIES.includes(req.capability)) throw new Error(`Unknown capability ${req.capability}`);
    const now = Date.now();
    const grant: Grant = {
      id: mockId('grant'),
      agent_id: req.agent_id,
      capability: req.capability,
      roots: req.roots ?? [],
      task_id: req.task_id ?? null,
      expires_at: req.ttl_s ? isoAt(now + req.ttl_s * 1000) : null,
      max_uses: req.max_uses ?? null,
      uses: 0,
      source: 'user',
      reason: req.reason ?? '',
      created_at: isoAt(now),
    };
    this.grants.push(grant);
    this.log(L(`הוענקה הרשאה ${req.capability} ל${agentLabel(req.agent_id, 'he')}`, `Granted ${req.capability} to the ${agentLabel(req.agent_id, 'en')}`), 'info', { agent: req.agent_id, kind: 'permissions' });
    return clone(grant);
  }

  revokeGrant(id: string) {
    const grant = this.grants.find((g) => g.id === id);
    if (!grant) throw new NotFoundError('Grant not found');
    this.grants = this.grants.filter((g) => g.id !== id);
    this.log(L(`בוטלה הרשאה ${grant.capability}`, `Revoked ${grant.capability}`), 'warning', { agent: grant.agent_id as AgentId, kind: 'permissions' });
    return { ok: true };
  }

  resetPermissions(id: AgentId) {
    this.grants = this.grants.filter((g) => g.agent_id !== id);
    delete this.restrictions[id];
    const profile = AGENT_PROFILES.find((p) => p.id === id);
    if (profile) this.setAgent(id, { permissions: profile.permissions });
    this.log(L(`ההרשאות של ${agentLabel(id, 'he')} אופסו`, `Permissions of the ${agentLabel(id, 'en')} were reset`), 'info', { agent: id, kind: 'permissions' });
    return { ok: true };
  }

  restrict(id: AgentId, body: { task_id?: string | null; folders?: string[] | null }) {
    this.restrictions[id] = { task: body.task_id ?? null, folders: body.folders ?? null };
    return { ok: true };
  }

  searchMemory(q: string, limit: number): MemorySearchHit[] {
    const tokens = q.toLowerCase().split(/\s+/).filter((t) => t.length > 1);
    if (!tokens.length) return [];
    const score = (text: string) => {
      const low = text.toLowerCase();
      const hits = tokens.filter((t) => low.includes(t)).length;
      return hits ? Math.round((hits / tokens.length) * (0.72 + Math.min(0.25, 8 / (text.length + 8))) * 100) / 100 : 0;
    };
    const out: MemorySearchHit[] = [];
    for (const f of this.facts) out.push({ layer: 'long_term', id: f.id, text: f.text, score: score(f.text), ts: f.created_at });
    for (const p of this.projects) out.push({ layer: 'project', id: p.id, text: `${p.name} — ${p.description}`, score: score(`${p.name} ${p.description}`), ts: p.updated_at, ref: p.path });
    for (const t of this.tasks.values()) out.push({ layer: 'task', id: t.id, text: `${t.title}: ${t.summary ?? t.goal}`, score: score(`${t.title} ${t.goal} ${t.summary ?? ''}`), ts: t.created_at, ref: t.id });
    for (const m of this.chat) out.push({ layer: 'short_term', id: m.id, text: m.text, score: score(m.text) * 0.9, ts: m.ts });
    return out.filter((h) => h.score > 0).sort((a, b) => b.score - a.score).slice(0, limit);
  }

  addFact(text: string, category?: string): MemoryFact {
    const fact: MemoryFact = { id: mockId('fact'), text, category: category || 'general', created_at: isoAt(Date.now()) };
    this.facts.unshift(fact);
    this.log(L(`נשמרה עובדה בזיכרון: ${text}`, `Saved to memory: ${text}`), 'info', { kind: 'memory' });
    return clone(fact);
  }

  deleteFact(id: string) {
    const before = this.facts.length;
    this.facts = this.facts.filter((f) => f.id !== id);
    if (this.facts.length === before) throw new NotFoundError('Fact not found');
    return { ok: true };
  }

  openFile(path: string, reveal: boolean) {
    this.log(reveal ? L(`הוצג בתיקייה: ${path}`, `Revealed in folder: ${path}`) : L(`נפתח: ${path}`, `Opened: ${path}`), 'info', { agent: 'system', kind: 'files' });
    this.recordAudit('system', reveal ? 'fs.reveal' : 'fs.open', 'process.launch', path, 'safe', 'allowed', 'ok');
    return { ok: true };
  }

  clearChat() {
    this.chat = [];
    return { ok: true };
  }

  agentDetail(id: AgentId) {
    const agent = this.agents.get(id);
    if (!agent) throw new NotFoundError(`Unknown agent ${id}`);
    return clone({ ...agent, activity: this.activity.filter((a) => a.agent_id === id).slice(-40) });
  }
}

const STEP_LABELS: Record<PipelineEvent['step'], Bi> = {
  transcribing: L('מתמלל…', 'Transcribing…'),
  understanding: L('מבין את הבקשה…', 'Understanding…'),
  routing: L('מנתב…', 'Routing…'),
  selecting_tool: L('בוחר כלי…', 'Selecting tool…'),
  preparing_action: L('מכין פעולה…', 'Preparing action…'),
  executing: L('מבצע…', 'Executing…'),
  done: L('הושלם', 'Done'),
};

function deepMerge(base: Record<string, unknown>, patch: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = { ...base };
  for (const [key, value] of Object.entries(patch)) {
    const prev = out[key];
    out[key] =
      value && typeof value === 'object' && !Array.isArray(value) && prev && typeof prev === 'object' && !Array.isArray(prev)
        ? deepMerge(prev as Record<string, unknown>, value as Record<string, unknown>)
        : value;
  }
  return out;
}
