import type {
  ActionEvent,
  ActivityEntry,
  AgentView,
  AuditEntry,
  ChatMessage,
  ConfirmationRequest,
  KillSwitchState,
  Mode,
  PipelineStep,
  ProvidersInfo,
  ServerEvent,
  SubTaskView,
  SystemStats,
  TaskDetail,
  TaskSummary,
  UISettings,
  VoiceInfo,
} from '../api/types';
import type { Job } from '../api/work';
import type { ConnectionInfo } from '../api/ws';

export interface PipelineStepState {
  step: PipelineStep;
  status: 'pending' | 'active' | 'done' | 'error';
  label: string | null;
}

export interface OverlayState {
  /** When the current voice state started (ms epoch). */
  stateSince: number;
  transcript: { text: string; final: boolean; language: string; at: number } | null;
  pipeline: { steps: PipelineStepState[]; startedAt: number; updatedAt: number; finished: boolean } | null;
  action: (ActionEvent & { at: number; startedAt: number }) | null;
  speech: { text: string; language: string; at: number } | null;
  error: { message: string | null; detail: string | null; at: number } | null;
}

export type ToastLevel = 'info' | 'success' | 'warning' | 'error';

export interface Toast {
  id: string;
  level: ToastLevel;
  title: string;
  message: string;
  at: number;
  source: 'server' | 'local';
}

export interface SystemSample {
  t: number;
  cpu: number;
  mem: number;
  disk: number;
  up: number;
  down: number;
}

export interface PendingChat {
  clientId: string;
  text: string;
  at: number;
  failed: boolean;
}

/** A screen the core asked the UI to show ("show me the agents"). `seq` makes repeats distinct. */
export interface NavigationRequest {
  screen: string;
  param: string | null;
  seq: number;
}

export interface AppState {
  connection: ConnectionInfo;
  /** First `state` snapshot received. */
  hydrated: boolean;
  serverVersion: string | null;
  version: string;
  assistantName: string;
  userName: string;
  language: 'auto' | 'he' | 'en';
  mode: Mode;
  voice: VoiceInfo;
  voiceLevel: number;
  ui: UISettings;
  agents: AgentView[];
  tasks: { active: TaskSummary[]; recent: TaskSummary[] };
  taskDetails: Record<string, TaskDetail>;
  /** Subtasks seen on the event stream for tasks whose detail is not loaded yet. */
  liveSubtasks: Record<string, SubTaskView[]>;
  chat: ChatMessage[];
  pendingChat: PendingChat[];
  activity: ActivityEntry[];
  audit: AuditEntry[];
  auditLoaded: boolean;
  confirmations: ConfirmationRequest[];
  killSwitch: KillSwitchState;
  system: SystemStats | null;
  systemHistory: SystemSample[];
  providers: ProvidersInfo;
  files: { workspace: string; projects: string };
  overlay: OverlayState;
  toasts: Toast[];
  navigation: NavigationRequest | null;
  /** The work screens' actions, newest first (live over `job` events). */
  jobs: Job[];
}

export type Action =
  | { type: 'event'; event: ServerEvent; at: number }
  | { type: 'connection'; info: ConnectionInfo }
  | { type: 'task.detail'; detail: TaskDetail }
  | { type: 'audit.loaded'; entries: AuditEntry[] }
  | { type: 'chat.pending'; pending: PendingChat }
  | { type: 'chat.pendingDone'; clientId: string; failed: boolean }
  | { type: 'chat.cleared' }
  | { type: 'toast.push'; toast: Omit<Toast, 'at'> & { at?: number } }
  | { type: 'toast.dismiss'; id: string }
  | { type: 'ui.local'; patch: Partial<UISettings> }
  | { type: 'confirmation.resolved'; id: string }
  | { type: 'overlay.dismiss'; at: number };
