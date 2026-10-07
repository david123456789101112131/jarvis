import type { ActionEvent, ActivityEntry, AgentId, ConfirmationRequest, SubTaskView, TaskSummary } from '../api/types';
import type { AppState, PipelineStepState } from './types';

export const HOLD = {
  speech: 4500,
  actionDone: 2600,
  actionError: 6000,
  error: 6000,
  stepsAfterFinish: 1800,
  stepsStale: 8000,
  orbError: 2600,
} as const;

export type BelowSlot =
  | { kind: 'wake' }
  | { kind: 'transcript'; text: string; final: boolean; language: string }
  | { kind: 'action'; action: ActionEvent & { at: number; startedAt: number } }
  | { kind: 'speech'; text: string; language: string }
  | { kind: 'error'; message: string | null; detail: string | null }
  | { kind: 'idle-wave' };

export interface CoreOverlay {
  below: BelowSlot | null;
  steps: PipelineStepState[] | null;
  /** Show the waveform band across the orb (wake / listening / speaking). */
  band: boolean;
  /** Earliest time at which this result may change on its own (for re-render scheduling). */
  nextChangeAt: number | null;
}

/** What the orb panel shows right now. Pure: same state and `now` give the same answer. */
export function selectCoreOverlay(state: AppState, now: number): CoreOverlay {
  const { overlay, voice, ui } = state;
  const vs = voice.state;
  const deadlines: number[] = [];
  const within = (at: number, hold: number) => {
    const end = at + hold;
    if (now < end) {
      deadlines.push(end);
      return true;
    }
    return false;
  };

  let below: BelowSlot | null = null;
  const errorFresh = overlay.error ? within(overlay.error.at, HOLD.error) : false;
  if (vs === 'error' || errorFresh) {
    below = { kind: 'error', message: overlay.error?.message ?? null, detail: overlay.error?.detail ?? voice.error ?? null };
  } else if (overlay.speech && (vs === 'speaking' || (vs !== 'wake' && vs !== 'listening' && within(overlay.speech.at, HOLD.speech)))) {
    below = { kind: 'speech', text: overlay.speech.text, language: overlay.speech.language };
  } else if (
    overlay.action &&
    (overlay.action.status === 'running' ||
      within(overlay.action.at, overlay.action.status === 'done' ? HOLD.actionDone : HOLD.actionError))
  ) {
    below = { kind: 'action', action: overlay.action };
  } else if (vs === 'listening' || (vs === 'processing' && overlay.transcript)) {
    const t = overlay.transcript;
    below = { kind: 'transcript', text: t?.text ?? '', final: t?.final ?? false, language: t?.language ?? '' };
  } else if (vs === 'wake') {
    below = { kind: 'wake' };
  } else if (ui.always_active && vs === 'idle' && voice.enabled && !voice.muted) {
    below = { kind: 'idle-wave' };
  }

  let steps: PipelineStepState[] | null = null;
  const p = overlay.pipeline;
  if (p) {
    const visible =
      vs === 'processing'
        ? within(p.updatedAt, HOLD.stepsStale * 4)
        : p.finished
          ? within(p.updatedAt, HOLD.stepsAfterFinish)
          : within(p.updatedAt, HOLD.stepsStale);
    if (visible) steps = p.steps;
  }

  return {
    below,
    steps,
    band: vs === 'wake' || vs === 'listening' || vs === 'speaking',
    nextChangeAt: deadlines.length ? Math.min(...deadlines) : null,
  };
}

export type OrbVisualState = 'idle' | 'wake' | 'listening' | 'processing' | 'speaking' | 'error' | 'disabled' | 'busy';

export function selectOrbState(state: AppState, now: number): OrbVisualState {
  const vs = state.voice.state;
  // Errors flash red (entering the voice "error" state records `overlay.error`), then the orb calms
  // down even if the problem persists (e.g. microphone access denied); the voice chip keeps showing it.
  if (state.overlay.error && now - state.overlay.error.at < HOLD.orbError) return 'error';
  if (state.killSwitch.global) return 'disabled';
  const busy = state.tasks.active.some((t) => t.status === 'running' || t.status === 'planning' || t.status === 'verifying');
  switch (vs) {
    case 'wake':
    case 'listening':
    case 'processing':
    case 'speaking':
      return vs;
    // Voice off is not JARVIS off: text commands still work, so the orb stays alive.
    // The dim "disabled" look is reserved for the global kill switch.
    case 'disabled':
    default:
      return busy ? 'busy' : 'idle';
  }
}

function newestFirst(a: TaskSummary, b: TaskSummary): number {
  return a.created_at < b.created_at ? 1 : a.created_at > b.created_at ? -1 : 0;
}

function finishedFirst(a: TaskSummary, b: TaskSummary): number {
  const ka = a.finished_at ?? a.updated_at;
  const kb = b.finished_at ?? b.updated_at;
  return ka < kb ? 1 : ka > kb ? -1 : 0;
}

export function sortActiveTasks(tasks: readonly TaskSummary[]): TaskSummary[] {
  return tasks.slice().sort(newestFirst);
}

export function sortRecentTasks(tasks: readonly TaskSummary[]): TaskSummary[] {
  return tasks.slice().sort(finishedFirst);
}

const EMPTY_SUBTASKS: SubTaskView[] = [];

export function selectSubtasks(state: AppState, taskId: string): SubTaskView[] {
  return state.taskDetails[taskId]?.plan?.subtasks ?? state.liveSubtasks[taskId] ?? EMPTY_SUBTASKS;
}

export function filterActivity(entries: readonly ActivityEntry[], agent: AgentId | 'jarvis' | 'all'): ActivityEntry[] {
  if (agent === 'all') return entries.slice();
  if (agent === 'jarvis') return entries.filter((e) => !e.agent_id);
  return entries.filter((e) => e.agent_id === agent);
}

/** Oldest pending confirmation first (that is the one that expires first). */
export function selectPendingConfirmations(state: AppState): ConfirmationRequest[] {
  return state.confirmations
    .filter((c) => c.status === 'pending')
    .slice()
    .sort((a, b) => (a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : 0));
}

/** Merge a fetched history page with the live lists (live copies win when newer). */
export function mergeTaskLists(fetched: readonly TaskSummary[], live: readonly TaskSummary[]): TaskSummary[] {
  const map = new Map<string, TaskSummary>();
  for (const t of fetched) map.set(t.id, t);
  for (const t of live) {
    const known = map.get(t.id);
    if (!known || known.updated_at <= t.updated_at) map.set(t.id, t);
  }
  return [...map.values()].sort(newestFirst);
}

/** Group subtasks into dependency levels (columns of the plan DAG). */
export function planLevels(subtasks: readonly SubTaskView[]): SubTaskView[][] {
  const byId = new Map(subtasks.map((s) => [s.id, s]));
  const depth = new Map<string, number>();
  const visiting = new Set<string>();
  const levelOf = (s: SubTaskView): number => {
    const known = depth.get(s.id);
    if (known !== undefined) return known;
    if (visiting.has(s.id)) return 0; // cycle guard
    visiting.add(s.id);
    let level = 0;
    for (const dep of s.depends_on) {
      const d = byId.get(dep);
      if (d) level = Math.max(level, levelOf(d) + 1);
    }
    visiting.delete(s.id);
    depth.set(s.id, level);
    return level;
  };
  const levels: SubTaskView[][] = [];
  for (const s of subtasks) {
    const level = levelOf(s);
    (levels[level] ??= []).push(s);
  }
  return levels.filter(Boolean);
}
