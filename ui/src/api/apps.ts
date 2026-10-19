import type { JarvisApi } from './client';
import type { Lang, RiskLevel } from './types';
import type { Job } from './work';

/** One way JARVIS can work with this computer's apps (core: `AppsService.status`), and why it is not available. */
export interface AppsWay {
  key: 'launch' | 'windows' | 'uia' | 'vision' | 'connectors' | string;
  label: string;
  detail: string;
  available: boolean;
  /** Why not, in words; null when available. */
  reason: string | null;
  /** What the user can do about it. */
  needs: string[];
}

export interface AppsStopWay {
  key: 'stop_button' | 'esc_twice' | 'kill_switch' | string;
  label: string;
  detail: string;
}

/** One goal the user works toward on the screen: what JARVIS keeps for it (in memory only). */
export interface AppsTask {
  id: string;
  goal: string;
  corrections: string[];
  done: string[];
}

export interface AppsStatus {
  ways: AppsWay[];
  stop: AppsStopWay[];
  limits: string[];
  /** "Nothing happens in the background…" */
  note: string;
  computer_enabled: boolean;
  /** Computer control is running a task right now. */
  busy: boolean;
  task: AppsTask | null;
}

export interface InstalledApp {
  name: string;
  kind: string;
  source: string;
  id: string | null;
  /** `['open']`, or empty with `why_not`. */
  can: string[];
  why_not: string | null;
}

export interface OpenWindow {
  program: string;
  title: string;
  pid: number | null;
  windows: number;
  /** How to name it to a look or a suggestion (its title, or its program). */
  window: string;
  can: ('look' | 'suggest' | string)[];
  /** Why JARVIS does not look into it, or why nothing can be done with it here. */
  note: string | null;
}

export interface AppsListing {
  installed: InstalledApp[];
  total: number;
  matched: number;
  windows: OpenWindow[];
  errors: string[];
}

/** What Windows itself reports about the window in front and its element. */
export interface ScreenFacts {
  window: string;
  title: string;
  program: string;
  element: string;
  control: string;
  password: boolean;
  own: boolean;
  sensitive: string | null;
}

/** One look at the screen. The screenshot itself never reaches the UI. */
export interface ScreenLook {
  purpose: 'look' | 'suggest';
  facts: ScreenFacts;
  /** Why JARVIS did not look into the window in front. */
  blocked: string | null;
  /** `model`: the local model saw one screenshot; `facts`: only what Windows reports, no screenshot. */
  analysed: 'model' | 'facts';
  screen: { width: number; height: number } | null;
  description: string | null;
  model_error: string | null;
}

/** The one next step the model proposed, as JARVIS judged it. Nothing was done. */
export interface StepProposal {
  kind: 'action' | 'answer' | 'wait' | 'unknown';
  understood: string;
  model_element: string;
  /** answer / wait / unknown: what the model said instead of an action. */
  answer?: string;
  action?: Record<string, unknown>;
  /** The step in words. */
  words?: string;
  window?: string;
  program?: string;
  element?: string;
  control?: string;
  risk?: RiskLevel;
  reasons?: string[];
  refused?: string | null;
  irreversible?: boolean;
  rule?: string | null;
  /** JARVIS's rules ask once more right before the action itself. */
  asks_again?: boolean;
  /** For sending, deleting, paying and the like: what will happen, in one line. */
  preview?: string | null;
  can_do: boolean;
}

export interface LookResult {
  summary: string;
  task_id: string;
  look: ScreenLook;
}

export interface SuggestResult extends LookResult {
  proposal: StepProposal;
}

const enc = encodeURIComponent;

export const apps = {
  status: (api: JarvisApi, lang: Lang) => api.request<AppsStatus>('GET', `/api/apps/status?lang=${lang}`),
  list: (api: JarvisApi, query: string, lang: Lang, refresh = false) =>
    api.request<AppsListing>('GET', `/api/apps/list?q=${enc(query)}&lang=${lang}${refresh ? '&refresh=1' : ''}`),
  open: (api: JarvisApi, name: string, lang: Lang) => api.request<Job>('POST', '/api/apps/open', { name, lang }),
  look: (api: JarvisApi, taskId: string | null, window: string, lang: Lang) =>
    api.request<Job>('POST', '/api/apps/look', { task_id: taskId, window, lang }),
  correct: (api: JarvisApi, text: string, taskId: string | null, lang: Lang) =>
    api.request<{ task: AppsTask }>('POST', '/api/apps/correct', { text, task_id: taskId, lang }),
  suggest: (api: JarvisApi, goal: string, taskId: string | null, window: string, lang: Lang) =>
    api.request<Job>('POST', '/api/apps/suggest', { goal, task_id: taskId, window, lang }),
  /** Do the step a finished suggestion proposed: it asks for approval first. */
  step: (api: JarvisApi, suggestion: string, lang: Lang) => api.request<Job>('POST', '/api/apps/step', { suggestion, lang }),
  /** End a task: its corrections and suggestions go, and so does any approval remembered for it. */
  endTask: (api: JarvisApi, taskId: string) => api.request<{ ended: boolean }>('DELETE', `/api/apps/task/${enc(taskId)}`),
};

/** A finished look's or suggestion's result, when the job still has it (the history keeps only its summary). */
export function lookOf(job: Job | undefined): ScreenLook | null {
  const look = (job?.result as Partial<LookResult> | null | undefined)?.look;
  return look && typeof look === 'object' && 'facts' in look ? look : null;
}

export function proposalOf(job: Job | undefined): StepProposal | null {
  const proposal = (job?.result as Partial<SuggestResult> | null | undefined)?.proposal;
  return proposal && typeof proposal === 'object' && 'kind' in proposal ? proposal : null;
}
