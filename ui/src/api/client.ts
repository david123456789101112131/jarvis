import type {
  AgentAction,
  AgentDetail,
  AgentId,
  AgentView,
  AuditEntry,
  ChatMessage,
  CommandResponse,
  ConfirmationRequest,
  DeepPartialSettings,
  FileListing,
  Grant,
  GrantRequest,
  KillSwitchState,
  MemoryFact,
  MemoryProject,
  MemorySearchHit,
  Mode,
  PermissionsView,
  Priority,
  ProcessInfo,
  QuickTool,
  RecentFile,
  SecretStatus,
  SettingsObject,
  StateSnapshot,
  SystemStats,
  TaskDetail,
  TaskStatus,
  TaskSummary,
  VoiceAction,
  VoiceInfo,
  ActivityEntry,
  Lang,
  WindowAction,
} from './types';

/** Error returned by the core: `{"error": {"code", "message"}}` with a 4xx/5xx status. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

/** Typed REST surface of the core (docs/API.md). Implemented by HttpApi and by the mock. */
export interface JarvisApi {
  health(): Promise<{ ok: boolean; version: string }>;
  getState(): Promise<StateSnapshot>;
  command(text: string, source?: 'text' | 'voice'): Promise<CommandResponse>;
  getChat(limit?: number): Promise<ChatMessage[]>;
  clearChat(): Promise<{ ok: boolean }>;
  getAgents(): Promise<AgentView[]>;
  getAgent(id: AgentId): Promise<AgentDetail>;
  agentAction(id: AgentId, action: AgentAction): Promise<AgentView>;
  assignTask(id: AgentId, instruction: string, priority?: Priority): Promise<{ task_id: string }>;
  getTasks(params?: { status?: TaskStatus | ''; limit?: number }): Promise<TaskSummary[]>;
  getTask(id: string): Promise<TaskDetail>;
  cancelTask(id: string): Promise<TaskSummary>;
  resumeTask(id: string): Promise<TaskSummary>;
  retryTask(id: string): Promise<TaskSummary>;
  getSystem(): Promise<SystemStats>;
  getProcesses(limit?: number): Promise<ProcessInfo[]>;
  getActivity(params?: { limit?: number; task_id?: string; agent_id?: string }): Promise<ActivityEntry[]>;
  getAudit(limit?: number): Promise<AuditEntry[]>;
  getPermissions(): Promise<PermissionsView>;
  grantPermission(body: GrantRequest): Promise<Grant>;
  revokeGrant(grantId: string): Promise<{ ok: boolean }>;
  resetAgentPermissions(id: AgentId): Promise<{ ok: boolean }>;
  restrictAgent(id: AgentId, body: { task_id?: string | null; folders?: string[] | null }): Promise<{ ok: boolean }>;
  getConfirmations(): Promise<ConfirmationRequest[]>;
  answerConfirmation(id: string, approved: boolean, remember?: boolean): Promise<{ ok: boolean }>;
  killSwitch(engage: boolean, agentId?: AgentId, reason?: string): Promise<KillSwitchState>;
  setMode(mode: Mode): Promise<{ mode: Mode }>;
  getFacts(q?: string): Promise<MemoryFact[]>;
  addFact(text: string, category?: string): Promise<MemoryFact>;
  deleteFact(id: string): Promise<{ ok: boolean }>;
  searchMemory(q: string, limit?: number): Promise<MemorySearchHit[]>;
  getProjects(): Promise<MemoryProject[]>;
  getRecentFiles(limit?: number): Promise<RecentFile[]>;
  listFiles(path?: string): Promise<FileListing>;
  /** Opens a file with its app; executables and scripts are only shown in Explorer (`revealed`). */
  openFile(path: string, reveal?: boolean): Promise<{ ok: boolean; revealed?: boolean }>;
  getSettings(): Promise<SettingsObject>;
  patchSettings(patch: DeepPartialSettings): Promise<SettingsObject>;
  getSecrets(): Promise<Record<string, SecretStatus>>;
  putSecret(name: string, value: string): Promise<{ configured: boolean; source: string | null }>;
  deleteSecret(name: string): Promise<{ configured: boolean }>;
  voice(action: VoiceAction): Promise<VoiceInfo>;
  speak(text: string): Promise<{ ok: boolean }>;
  getQuickTools(lang?: Lang): Promise<QuickTool[]>;
  windowAction(action: WindowAction): Promise<{ ok: boolean }>;
}

type Query = Record<string, string | number | undefined | null>;

function qs(params?: Query): string {
  if (!params) return '';
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : '';
}

const enc = encodeURIComponent;

export interface HttpApiOptions {
  baseUrl: string; // e.g. "http://127.0.0.1:8765" or "" for same origin
  token: string | null;
  timeoutMs?: number;
  fetchImpl?: typeof fetch;
}

/** REST client: JSON bodies, `X-JARVIS-Token` header, typed errors and a request timeout. */
export class HttpApi implements JarvisApi {
  private readonly baseUrl: string;
  private readonly token: string | null;
  private readonly timeoutMs: number;
  private readonly fetchImpl: typeof fetch;

  constructor(options: HttpApiOptions) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, '');
    this.token = options.token;
    this.timeoutMs = options.timeoutMs ?? 20000;
    this.fetchImpl = options.fetchImpl ?? ((...args) => fetch(...args));
  }

  async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const headers: Record<string, string> = { Accept: 'application/json' };
    if (this.token) headers['X-JARVIS-Token'] = this.token;
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
    const timer = controller ? setTimeout(() => controller.abort(), this.timeoutMs) : null;
    let response: Response;
    try {
      response = await this.fetchImpl(`${this.baseUrl}${path}`, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller?.signal,
        credentials: 'same-origin',
        cache: 'no-store',
      });
    } catch (err) {
      const aborted = err instanceof DOMException && err.name === 'AbortError';
      throw new ApiError(0, aborted ? 'timeout' : 'network', aborted ? 'Request timed out' : 'Core unreachable');
    } finally {
      if (timer) clearTimeout(timer);
    }
    const text = await response.text();
    let data: unknown = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        data = null;
      }
    }
    if (!response.ok) {
      const error = (data as { error?: { code?: string; message?: string }; detail?: unknown } | null) ?? null;
      const code = error?.error?.code ?? `http_${response.status}`;
      const message =
        error?.error?.message ??
        (typeof error?.detail === 'string' ? error.detail : `${response.status} ${response.statusText}`.trim());
      throw new ApiError(response.status, code, message);
    }
    return data as T;
  }

  private get<T>(path: string, params?: Query) {
    return this.request<T>('GET', `${path}${qs(params)}`);
  }
  private post<T>(path: string, body: unknown = {}) {
    return this.request<T>('POST', path, body);
  }

  health() {
    return this.get<{ ok: boolean; version: string }>('/api/health');
  }
  getState() {
    return this.get<StateSnapshot>('/api/state');
  }
  command(text: string, source: 'text' | 'voice' = 'text') {
    return this.post<CommandResponse>('/api/command', { text, source });
  }
  getChat(limit = 100) {
    return this.get<ChatMessage[]>('/api/chat', { limit });
  }
  clearChat() {
    return this.request<{ ok: boolean }>('DELETE', '/api/chat');
  }
  getAgents() {
    return this.get<AgentView[]>('/api/agents');
  }
  getAgent(id: AgentId) {
    return this.get<AgentDetail>(`/api/agents/${enc(id)}`);
  }
  agentAction(id: AgentId, action: AgentAction) {
    return this.post<AgentView>(`/api/agents/${enc(id)}/action`, { action });
  }
  assignTask(id: AgentId, instruction: string, priority?: Priority) {
    return this.post<{ task_id: string }>(`/api/agents/${enc(id)}/task`, priority ? { instruction, priority } : { instruction });
  }
  getTasks(params?: { status?: TaskStatus | ''; limit?: number }) {
    return this.get<TaskSummary[]>('/api/tasks', { status: params?.status, limit: params?.limit ?? 50 });
  }
  getTask(id: string) {
    return this.get<TaskDetail>(`/api/tasks/${enc(id)}`);
  }
  cancelTask(id: string) {
    return this.post<TaskSummary>(`/api/tasks/${enc(id)}/cancel`);
  }
  resumeTask(id: string) {
    return this.post<TaskSummary>(`/api/tasks/${enc(id)}/resume`);
  }
  retryTask(id: string) {
    return this.post<TaskSummary>(`/api/tasks/${enc(id)}/retry`);
  }
  getSystem() {
    return this.get<SystemStats>('/api/system');
  }
  getProcesses(limit = 15) {
    return this.get<ProcessInfo[]>('/api/system/processes', { limit });
  }
  getActivity(params?: { limit?: number; task_id?: string; agent_id?: string }) {
    return this.get<ActivityEntry[]>('/api/activity', { limit: params?.limit ?? 200, task_id: params?.task_id, agent_id: params?.agent_id });
  }
  getAudit(limit = 200) {
    return this.get<AuditEntry[]>('/api/audit', { limit });
  }
  getPermissions() {
    return this.get<PermissionsView>('/api/permissions');
  }
  grantPermission(body: GrantRequest) {
    return this.post<Grant>('/api/permissions/grant', body);
  }
  revokeGrant(grantId: string) {
    return this.request<{ ok: boolean }>('DELETE', `/api/permissions/grants/${enc(grantId)}`);
  }
  resetAgentPermissions(id: AgentId) {
    return this.post<{ ok: boolean }>(`/api/permissions/agents/${enc(id)}/reset`);
  }
  restrictAgent(id: AgentId, body: { task_id?: string | null; folders?: string[] | null }) {
    return this.post<{ ok: boolean }>(`/api/permissions/agents/${enc(id)}/restrict`, body);
  }
  getConfirmations() {
    return this.get<ConfirmationRequest[]>('/api/confirmations');
  }
  answerConfirmation(id: string, approved: boolean, remember = false) {
    return this.post<{ ok: boolean }>(`/api/confirmations/${enc(id)}`, { approved, remember });
  }
  killSwitch(engage: boolean, agentId?: AgentId, reason?: string) {
    const body: Record<string, unknown> = { engage };
    if (agentId) body.agent_id = agentId;
    if (reason) body.reason = reason;
    return this.post<KillSwitchState>('/api/killswitch', body);
  }
  setMode(mode: Mode) {
    return this.post<{ mode: Mode }>('/api/mode', { mode });
  }
  getFacts(q?: string) {
    return this.get<MemoryFact[]>('/api/memory/facts', { q });
  }
  addFact(text: string, category?: string) {
    return this.post<MemoryFact>('/api/memory/facts', category ? { text, category } : { text });
  }
  deleteFact(id: string) {
    return this.request<{ ok: boolean }>('DELETE', `/api/memory/facts/${enc(id)}`);
  }
  searchMemory(q: string, limit = 20) {
    return this.get<MemorySearchHit[]>('/api/memory/search', { q, limit });
  }
  getProjects() {
    return this.get<MemoryProject[]>('/api/memory/projects');
  }
  getRecentFiles(limit = 30) {
    return this.get<RecentFile[]>('/api/files/recent', { limit });
  }
  listFiles(path?: string) {
    return this.get<FileListing>('/api/files/list', { path });
  }
  openFile(path: string, reveal = false) {
    return this.post<{ ok: boolean; revealed?: boolean }>('/api/files/open', { path, reveal });
  }
  getSettings() {
    return this.get<SettingsObject>('/api/settings');
  }
  patchSettings(patch: DeepPartialSettings) {
    return this.request<SettingsObject>('PATCH', '/api/settings', patch);
  }
  getSecrets() {
    return this.get<Record<string, SecretStatus>>('/api/secrets');
  }
  putSecret(name: string, value: string) {
    return this.request<{ configured: boolean; source: string | null }>('PUT', `/api/secrets/${enc(name)}`, { value });
  }
  deleteSecret(name: string) {
    return this.request<{ configured: boolean }>('DELETE', `/api/secrets/${enc(name)}`);
  }
  voice(action: VoiceAction) {
    return this.post<VoiceInfo>(`/api/voice/${enc(action)}`);
  }
  speak(text: string) {
    return this.post<{ ok: boolean }>('/api/voice/speak', { text });
  }
  getQuickTools(lang?: Lang) {
    return this.get<QuickTool[]>('/api/quick-tools', { lang });
  }
  windowAction(action: WindowAction) {
    return this.post<{ ok: boolean }>(`/api/window/${enc(action)}`);
  }
}
