import { act, cleanup, fireEvent, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ServerEvent, TaskSummary } from '../../api/types';
import { createInitialState, reducer } from '../../state/reducer';
import { HOLD, selectFocus } from '../../state/selectors';
import type { AppState } from '../../state/types';
import { createStore } from '../../state/store';
import { renderWith } from '../../test/render';
import { FOCUS_LINGER_MS, FOCUS_SETTLE_MS, focusGeometry, VoiceFocus } from './VoiceFocus';

const event = (state: AppState, e: unknown, at = 1000): AppState => reducer(state, { type: 'event', event: e as ServerEvent, at });
const voiceState = (state: string) => ({ type: 'voice.state', data: { state } });
const action = (status: string, source: string | null = 'voice') => ({ type: 'action', data: { id: 'a1', title: 'פותח את מחשבון…', icon: 'app', status, source } });

function task(status: TaskSummary['status'], source: TaskSummary['source'] = 'voice'): TaskSummary {
  return {
    id: 't1', title: 'סדר את התיקייה', goal: 'x', status, progress: 0.4, priority: 'normal', mode: 'normal', agents: ['system'], source,
    created_at: '2026-10-25T04:00:00Z', updated_at: '2026-10-25T04:00:01Z', started_at: null, finished_at: null, summary: null, error: null,
  };
}

describe('selectFocus', () => {
  it('comes forward while JARVIS listens, thinks and speaks, and stays out of the way otherwise', () => {
    let s = createInitialState();
    expect(selectFocus(s, 1000).mode).toBe('off');
    for (const state of ['wake', 'listening', 'processing', 'speaking']) {
      s = event(s, voiceState(state));
      expect(selectFocus(s, 1000).mode).toBe('talk');
    }
    s = event(s, voiceState('idle'));
    expect(selectFocus(s, 1000).mode).toBe('off');
  });

  it('turns small to tell what a spoken command is doing, and only a spoken one', () => {
    let s = event(createInitialState(), voiceState('processing'));
    s = event(s, action('running'));
    expect(selectFocus(s, 1000)).toMatchObject({ mode: 'act', action: { title: 'פותח את מחשבון…' } });
    s = event(s, voiceState('speaking'));                 // speaking wins: big again
    expect(selectFocus(s, 1000).mode).toBe('talk');
    s = event(event(s, voiceState('idle')), action('done'), 2000);
    expect(selectFocus(s, 2000)).toMatchObject({ mode: 'act', nextChangeAt: 2000 + HOLD.actionDone });
    expect(selectFocus(s, 2000 + HOLD.actionDone).mode).toBe('off');
    s = event(s, { type: 'speech', data: { text: 'פתחתי.', language: 'he' } }, 2100);
    expect(selectFocus(s, 2100).mode).toBe('off');           // said aloud: nothing left to show

    const typed = event(createInitialState(), action('running', 'text'));
    expect(selectFocus(typed, 1000).mode).toBe('off');
    const old = event(createInitialState(), action('running', null));
    expect(selectFocus(old, 1000).mode).toBe('off');
  });

  it('follows a spoken task to its end; a typed task, the kill switch or the setting keep it away', () => {
    let s = event(createInitialState(), { type: 'task.updated', data: task('running') });
    expect(selectFocus(s, 1000)).toMatchObject({ mode: 'act', task: { id: 't1' } });
    s = event(s, { type: 'task.updated', data: { ...task('completed'), updated_at: '2026-10-25T04:00:09Z' } });
    expect(selectFocus(s, 1000).mode).toBe('off');
    expect(selectFocus(event(createInitialState(), { type: 'task.updated', data: task('running', 'text') }), 1000).mode).toBe('off');

    const talking = event(createInitialState(), voiceState('speaking'));
    expect(selectFocus({ ...talking, killSwitch: { global: true, agents: {} } }, 1000).mode).toBe('off');
    expect(selectFocus(event(talking, { type: 'settings', data: { ui: { focus_orb: false } } }), 1000).mode).toBe('off');
  });
});

describe('focusGeometry', () => {
  it('fits the big orb and its caption in the smallest window, and puts the small one in the card', () => {
    const g = focusGeometry(1100, 700, false);
    expect(g.d).toBeGreaterThan(250);
    expect(g.captionTop + 120).toBeLessThan(700);
    expect(g.cy - g.d / 2).toBeGreaterThan(56);           // below the top bar
    expect(g.small.scale * g.d).toBeCloseTo(54);
    const rtl = focusGeometry(1100, 700, true);
    expect(rtl.small.dx).toBeGreaterThan(0);              // Hebrew: the orb on the right of the card
    expect(g.small.dx).toBeLessThan(0);
  });
});

describe('VoiceFocus', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null); // no WebGL here: the CSS orb
  });
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  const mount = () => {
    const store = createStore();
    renderWith(<VoiceFocus />, { store, lang: 'he' });
    const send = (e: unknown) => act(() => store.dispatch({ type: 'event', event: e as ServerEvent, at: Date.now() }));
    const view = () => screen.getByTestId('voice-focus');
    return { store, send, view };
  };

  it('grows for the conversation, shrinks while acting, and goes back after a moment', () => {
    const { send, view } = mount();
    expect(view().dataset.stage).toBe('hidden');
    send(voiceState('wake'));
    expect(view().dataset.mode).toBe('talk');
    expect(view().dataset.stage).toBe('shown');
    expect(document.documentElement.classList.contains('focus-orb-on')).toBe(false); // no command-center orb here
    send({ type: 'voice.transcript', data: { text: 'תפתח מחשבון', final: true, language: 'he' } });
    send(voiceState('processing'));
    expect(screen.getByTestId('focus-caption').textContent).toContain('תפתח מחשבון');

    send(action('running'));
    expect(view().dataset.mode).toBe('talk');              // a quick action does not send the orb on a trip
    act(() => vi.advanceTimersByTime(FOCUS_SETTLE_MS + 10));
    expect(view().dataset.mode).toBe('act');
    expect(screen.getByTestId('focus-narration').textContent).toContain('פותח את מחשבון');

    send(action('done'));                                  // as the core does it: done, then the result is said
    send(voiceState('speaking'));
    send({ type: 'speech', data: { text: 'פתחתי את המחשבון.', language: 'he' } });
    act(() => vi.advanceTimersByTime(FOCUS_SETTLE_MS + 10));
    expect(view().dataset.mode).toBe('talk');
    expect(screen.getByTestId('focus-caption').textContent).toContain('פתחתי את המחשבון');

    send(voiceState('idle'));
    act(() => vi.advanceTimersByTime(FOCUS_LINGER_MS + 50));  // the result was said: no small card on the way out
    expect(view().dataset.mode).toBe('off');
    expect(view().dataset.stage).toBe('leaving');
    act(() => vi.advanceTimersByTime(1000));               // no transitions in the test DOM: put away anyway
    expect(view().dataset.stage).toBe('hidden');
  });

  it('Esc puts it away until JARVIS is called again', () => {
    const { send, view } = mount();
    send(voiceState('wake'));
    send(voiceState('listening'));
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(view().dataset.mode).toBe('off');
    send(voiceState('speaking'));
    expect(view().dataset.mode).toBe('off');
    send(voiceState('idle'));
    send(voiceState('wake'));
    expect(view().dataset.mode).toBe('talk');
  });

  it('a spoken task is narrated with its steps', () => {
    const { send, view } = mount();
    send({ type: 'task.updated', data: task('running') });
    send({ type: 'task.subtask', data: { task_id: 't1', subtask: { id: 's1', agent: 'system', title: 'סורק קבצים', instruction: '', status: 'completed', progress: 1, attempts: 1, depends_on: [], optional: false, started_at: null, finished_at: null, summary: null, error: null, files_created: [] } } });
    send({ type: 'task.subtask', data: { task_id: 't1', subtask: { id: 's2', agent: 'system', title: 'מעביר לתיקיות', instruction: '', status: 'running', progress: 0.3, attempts: 1, depends_on: ['s1'], optional: false, started_at: null, finished_at: null, summary: null, error: null, files_created: [] } } });
    expect(view().dataset.mode).toBe('act');
    const card = screen.getByTestId('focus-task');
    expect(card.textContent).toContain('עובד על זה');
    expect(card.textContent).toContain('סדר את התיקייה');
    expect(card.textContent).toContain('מעביר לתיקיות');
    expect(card.querySelectorAll('.focus-task__step')).toHaveLength(2);
  });
});

describe('VoiceFocus in its own window (JARVIS in the background)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
  });
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('asks for its window first, leaves the acting to the mini orb, and gives the window back', async () => {
    const store = createStore();
    const windowAction = vi.fn(async (_action: string) => ({ ok: true }));
    renderWith(<VoiceFocus standalone />, { store, lang: 'he', api: { windowAction } });
    const send = (e: unknown) => act(() => store.dispatch({ type: 'event', event: e as ServerEvent, at: Date.now() }));
    const view = () => screen.getByTestId('voice-focus');
    expect(screen.queryByTestId('focus-narration')).toBeNull();

    send(voiceState('wake'));
    expect(windowAction).toHaveBeenCalledWith('focus_show');
    expect(view().dataset.mode).toBe('off');                // not before the window is on screen
    await act(async () => undefined);
    expect(view().dataset.mode).toBe('talk');

    send(voiceState('processing'));
    send(action('running'));
    act(() => vi.advanceTimersByTime(FOCUS_SETTLE_MS + 10));
    expect(view().dataset.mode).toBe('talk');               // a quick action does not send it away
    act(() => vi.advanceTimersByTime(FOCUS_LINGER_MS));
    expect(view().dataset.mode).toBe('off');                // acting: the mini orb in the corner tells it
    act(() => vi.advanceTimersByTime(1000));
    expect(windowAction).toHaveBeenLastCalledWith('focus_hide');

    send(voiceState('speaking'));                           // the answer: back to the middle
    await act(async () => undefined);
    expect(windowAction).toHaveBeenLastCalledWith('focus_show');
    expect(view().dataset.mode).toBe('talk');
    fireEvent.click(view());                                // a click puts it away
    act(() => vi.advanceTimersByTime(1000));
    expect(windowAction).toHaveBeenLastCalledWith('focus_hide');
    expect(windowAction.mock.calls.map((c) => c[0])).toEqual(['focus_show', 'focus_hide', 'focus_show', 'focus_hide']);
  });
});
