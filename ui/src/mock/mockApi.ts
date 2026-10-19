import { ApiError, type JarvisApi } from '../api/client';
import type {
  AgentAction,
  AgentId,
  DeepPartialSettings,
  FileListing,
  GrantRequest,
  Lang,
  Mode,
  PermissionsView,
  Priority,
  ProcessInfo,
  QuickTool,
  TaskStatus,
  VoiceAction,
  WindowAction,
} from '../api/types';
import { PATHS } from './content';
import { NotFoundError, type Simulator } from './simulator';
import { clone, isoAt, rand } from './util';

const QUICK_TOOLS: (QuickTool & { command_he: string })[] = [
  { id: 'chrome', label: 'Chrome', label_he: 'כרום', icon: 'chrome', command: 'open Chrome', command_he: 'פתח את כרום' },
  { id: 'youtube', label: 'YouTube', label_he: 'יוטיוב', icon: 'youtube', command: 'open YouTube', command_he: 'פתח את יוטיוב' },
  { id: 'calculator', label: 'Calculator', label_he: 'מחשבון', icon: 'calculator', command: 'open Calculator', command_he: 'פתח את המחשבון' },
  { id: 'settings', label: 'Settings', label_he: 'הגדרות', icon: 'settings', command: 'open Settings', command_he: 'פתח את ההגדרות' },
  { id: 'files', label: 'Files', label_he: 'קבצים', icon: 'folder', command: 'open File Explorer', command_he: 'פתח את סייר הקבצים' },
];

/** JarvisApi backed by the simulator, with a little latency so loading states are real. */
export class MockApi implements JarvisApi {
  private readonly sim: Simulator;

  constructor(sim: Simulator) {
    this.sim = sim;
  }

  private async call<T>(fn: () => T, min = 60, max = 220): Promise<T> {
    await new Promise((resolve) => setTimeout(resolve, rand(min, max)));
    try {
      return clone(fn());
    } catch (err) {
      if (err instanceof ApiError) throw err;
      if (err instanceof NotFoundError) throw new ApiError(404, 'not_found', err.message);
      if (err instanceof Error) throw new ApiError(400, 'bad_request', err.message);
      throw err;
    }
  }

  health() {
    return this.call(() => ({ ok: true, version: '1.0.0-sim' }));
  }
  getState() {
    return this.call(() => this.sim.snapshot());
  }
  command(text: string, source: 'text' | 'voice' = 'text') {
    return this.call(() => this.sim.command(text, source), 90, 260);
  }
  getChat(limit = 100) {
    return this.call(() => this.sim.chat.slice(-limit));
  }
  clearChat() {
    return this.call(() => this.sim.clearChat());
  }
  getAgents() {
    return this.call(() => [...this.sim.agents.values()]);
  }
  getAgent(id: AgentId) {
    return this.call(() => this.sim.agentDetail(id));
  }
  agentAction(id: AgentId, action: AgentAction) {
    return this.call(() => this.sim.agentAction(id, action));
  }
  assignTask(id: AgentId, instruction: string, priority?: Priority) {
    return this.call(() => this.sim.assignTask(id, instruction, priority));
  }
  getTasks(params?: { status?: TaskStatus | ''; limit?: number }) {
    return this.call(() => this.sim.getTasks(params?.status || undefined, params?.limit ?? 50));
  }
  getTask(id: string) {
    return this.call(() => this.sim.getTask(id), 120, 320);
  }
  cancelTask(id: string) {
    return this.call(() => this.sim.cancelTask(id));
  }
  resumeTask(id: string) {
    return this.call(() => this.sim.resumeTask(id));
  }
  retryTask(id: string) {
    return this.call(() => this.sim.retryTask(id));
  }
  getSystem() {
    return this.call(() => this.sim.systemStats());
  }
  getProcesses(limit = 15): Promise<ProcessInfo[]> {
    const names = ['chrome.exe', 'Code.exe', 'python.exe', 'msedgewebview2.exe', 'explorer.exe', 'Spotify.exe', 'Teams.exe', 'svchost.exe'];
    return this.call(() =>
      names.slice(0, limit).map((name, i) => ({ name, pid: 1200 + i * 317, cpu: Math.round(rand(0, 18) * 10) / 10, memory_mb: Math.round(rand(40, 900)) })),
    );
  }
  getActivity(params?: { limit?: number; task_id?: string; agent_id?: string }) {
    return this.call(() =>
      this.sim.activity
        .filter((a) => (!params?.task_id || a.task_id === params.task_id) && (!params?.agent_id || a.agent_id === params.agent_id))
        .slice(-(params?.limit ?? 200)),
    );
  }
  getAudit(limit = 200) {
    return this.call(() => this.sim.audit.slice(0, limit));
  }
  getPermissions() {
    return this.call(() => this.sim.permissions() as PermissionsView);
  }
  grantPermission(body: GrantRequest) {
    return this.call(() => this.sim.grant(body));
  }
  revokeGrant(grantId: string) {
    return this.call(() => this.sim.revokeGrant(grantId));
  }
  resetAgentPermissions(id: AgentId) {
    return this.call(() => this.sim.resetPermissions(id));
  }
  restrictAgent(id: AgentId, body: { task_id?: string | null; folders?: string[] | null }) {
    return this.call(() => this.sim.restrict(id, body));
  }
  getConfirmations() {
    return this.call(() => this.sim.snapshot().confirmations);
  }
  answerConfirmation(id: string, approved: boolean, remember = false) {
    return this.call(() => this.sim.answerConfirmation(id, approved, remember));
  }
  killSwitch(engage: boolean, agentId?: AgentId, reason?: string) {
    return this.call(() => (engage ? this.sim.engageKill(agentId, reason) : this.sim.releaseKill(agentId)));
  }
  setMode(mode: Mode) {
    return this.call(() => this.sim.setMode(mode));
  }
  getFacts(q?: string) {
    return this.call(() => (q ? this.sim.facts.filter((f) => f.text.toLowerCase().includes(q.toLowerCase()) || f.category.includes(q)) : this.sim.facts));
  }
  addFact(text: string, category?: string) {
    return this.call(() => this.sim.addFact(text, category));
  }
  deleteFact(id: string) {
    return this.call(() => this.sim.deleteFact(id));
  }
  searchMemory(q: string, limit = 20) {
    return this.call(() => this.sim.searchMemory(q, limit), 150, 380);
  }
  getProjects() {
    return this.call(() => this.sim.projects);
  }
  getRecentFiles(limit = 30) {
    return this.call(() => this.sim.files.slice(0, limit));
  }
  listFiles(path?: string): Promise<FileListing> {
    const root = path || PATHS.workspace;
    return this.call(() => ({
      path: root,
      entries: this.sim.files
        .filter((f) => f.path.startsWith(root))
        .map((f) => ({ name: f.name, path: f.path, is_dir: false, size: f.size ?? 0, modified: f.created_at || isoAt(Date.now()) })),
    }));
  }
  openFile(path: string, reveal = false) {
    return this.call(() => this.sim.openFile(path, reveal));
  }
  getSettings() {
    return this.call(() => this.sim.settings);
  }
  patchSettings(patch: DeepPartialSettings) {
    return this.call(() => this.sim.patchSettings(patch));
  }
  getSecrets() {
    return this.call(() => this.sim.secrets);
  }
  putSecret(name: string, value: string) {
    return this.call(() => this.sim.putSecret(name, value), 200, 420);
  }
  deleteSecret(name: string) {
    return this.call(() => this.sim.deleteSecret(name));
  }
  voice(action: VoiceAction) {
    return this.call(() => this.sim.voiceAction(action), 30, 90);
  }
  speak(text: string) {
    return this.call(() => this.sim.speak(text));
  }
  getQuickTools(lang?: Lang) {
    const he = (lang ?? this.sim.lang) === 'he';
    return this.call(() => QUICK_TOOLS.map(({ command_he, ...tool }) => ({ ...tool, command: he ? command_he : tool.command })));
  }
  windowAction(_action: WindowAction): Promise<{ ok: boolean }> {
    // The simulator has no desktop window, like `jarvis --no-window`.
    return this.call(() => {
      throw new ApiError(409, 'conflict', 'JARVIS runs without a desktop window.');
    });
  }
  /** The work screens need the real core: the simulation answers every other route with "not here". */
  request<T>(_method: string, path: string): Promise<T> {
    return this.call<T>(() => {
      throw new ApiError(501, 'simulation', `${path} is not available in the simulation`);
    });
  }
}
