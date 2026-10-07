import { describe, expect, it } from 'vitest';
import type { ChatMessage, ServerEvent, TaskSummary } from '../api/types';
import { createInitialState, reducer } from './reducer';
import { HOLD, selectOrbState, planLevels } from './selectors';
import type { AppState } from './types';

const event = (state: AppState, e: unknown, at = 1000): AppState => reducer(state, { type: 'event', event: e as ServerEvent, at });

function task(id: string, status: TaskSummary['status'], updated = '2026-10-07T10:00:00Z'): TaskSummary {
  return {
    id, title: id, goal: id, status, progress: 0, priority: 'normal', mode: 'normal', agents: [], source: 'text',
    created_at: '2026-10-07T09:00:00Z', updated_at: updated, started_at: null, finished_at: null, summary: null, error: null,
  };
}

function chat(id: string, role: ChatMessage['role'], text: string, ts: string): ChatMessage {
  return { id, role, text, ts, time: ts.slice(11, 19), source: 'text', language: 'he', kind: 'answer' };
}

describe('reducer', () => {
  it('moves a task from the active list to the recent list when it finishes', () => {
    let s = event(createInitialState(), { type: 'task.updated', data: task('t1', 'running') });
    expect(s.tasks.active.map((t) => t.id)).toEqual(['t1']);
    s = event(s, { type: 'task.updated', data: task('t1', 'completed', '2026-10-07T10:01:00Z') });
    expect(s.tasks.active).toEqual([]);
    expect(s.tasks.recent.map((t) => [t.id, t.status])).toEqual([['t1', 'completed']]);
  });

  it('keeps the chat in time order and drops the pending copy of a sent message', () => {
    let s = reducer(createInitialState(), { type: 'chat.pending', pending: { clientId: 'c1', text: 'שלום ', at: 1, failed: false } } as never);
    s = event(s, { type: 'chat.message', data: chat('m2', 'jarvis', 'שלום!', '2026-10-07T10:00:02Z') });
    s = event(s, { type: 'chat.message', data: chat('m1', 'user', 'שלום', '2026-10-07T10:00:01Z') });
    expect(s.chat.map((m) => m.id)).toEqual(['m1', 'm2']);
    expect(s.pendingChat).toEqual([]);
    s = event(s, { type: 'chat.message', data: { ...chat('m2', 'jarvis', 'עודכן', '2026-10-07T10:00:02Z') } });
    expect(s.chat.map((m) => m.text)).toEqual(['שלום', 'עודכן']);
  });

  it('clamps the microphone level and ignores garbage', () => {
    let s = event(createInitialState(), { type: 'voice.level', data: { level: 7 } });
    expect(s.voiceLevel).toBe(1);
    s = event(s, { type: 'voice.level', data: { level: 'loud' } });
    expect(s.voiceLevel).toBe(0);
  });

  it('records a voice error so the orb can flash, and counts navigation requests', () => {
    let s = event(createInitialState(), { type: 'voice.state', data: { state: 'error', error: 'no microphone' } }, 5000);
    expect(s.voice.error).toBe('no microphone');
    expect(s.overlay.error?.at).toBe(5000);
    s = event(s, { type: 'ui.navigate', data: { screen: 'tasks', task_id: 't9' } });
    s = event(s, { type: 'ui.navigate', data: { screen: 'tasks', task_id: 't9' } });
    expect(s.navigation).toEqual({ screen: 'tasks', param: 't9', seq: 2 });
  });

  it('removes a confirmation once it is answered anywhere', () => {
    const request = { id: 'c1', status: 'pending', created_at: '2026-10-07T10:00:00Z' };
    let s = event(createInitialState(), { type: 'confirm.request', data: request });
    expect(s.confirmations).toHaveLength(1);
    s = event(s, { type: 'confirm.response', data: { ...request, status: 'approved' } });
    expect(s.confirmations).toEqual([]);
  });

  it('keeps the orb size and opacity within range', () => {
    const s = event(createInitialState(), { type: 'settings', data: { ui: { orb_size: 9, orb_opacity: -1 } } });
    expect(s.ui.orb_size).toBe(2);
    expect(s.ui.orb_opacity).toBe(0.1);
  });
});

describe('selectOrbState', () => {
  it('stays alive when only the voice service is off', () => {
    const s = createInitialState();
    expect(selectOrbState({ ...s, voice: { ...s.voice, state: 'disabled' } }, 0)).toBe('idle');
    const busy = event(s, { type: 'task.updated', data: task('t1', 'running') });
    expect(selectOrbState({ ...busy, voice: { ...busy.voice, state: 'disabled' } }, 0)).toBe('busy');
  });

  it('dims only for the global kill switch', () => {
    const s = event(createInitialState(), { type: 'killswitch', data: { global: true, agents: {} } });
    expect(selectOrbState(s, 0)).toBe('disabled');
  });

  it('flashes red on an error, then calms down while the problem lasts', () => {
    const s = event(createInitialState(), { type: 'voice.state', data: { state: 'error' } }, 1000);
    expect(selectOrbState(s, 1000 + 100)).toBe('error');
    expect(selectOrbState(s, 1000 + HOLD.orbError + 1)).toBe('idle');
  });

  it('shows the live voice states', () => {
    const s = event(createInitialState(), { type: 'voice.state', data: { state: 'listening' } });
    expect(selectOrbState(s, 2000)).toBe('listening');
  });
});

describe('planLevels', () => {
  it('groups steps by dependency depth and survives cycles', () => {
    const step = (id: string, depends_on: string[]) => ({ id, depends_on }) as never;
    const levels = planLevels([step('a', []), step('b', ['a']), step('c', ['a']), step('d', ['b', 'c']), step('x', ['y']), step('y', ['x'])]);
    expect(levels.map((l) => l.map((s: { id: string }) => s.id))).toEqual([['a'], ['b', 'c', 'y'], ['d', 'x']]);
  });
});
