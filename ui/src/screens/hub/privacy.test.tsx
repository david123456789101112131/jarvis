import { act, cleanup, fireEvent, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { PrivacyCleared, PrivacyInventory, PrivacyItem, PrivacyKey } from '../../api/privacy';
import type { VoiceInfo } from '../../api/types';
import { createStore } from '../../state/store';
import { renderWith } from '../../test/render';
import { PrivacyScreen } from './PrivacyScreen';

afterEach(cleanup);

const KEYS: PrivacyKey[] = ['chat', 'commands', 'tasks', 'memory', 'projects', 'files', 'jobs', 'code', 'media', 'speed', 'logs', 'temp', 'approvals'];

function inventory(counts: Partial<Record<PrivacyKey, number>> = {}, extra: Partial<Record<PrivacyKey, Partial<PrivacyItem>>> = {}, voice: Partial<PrivacyInventory['senses']['microphone']> = {}): PrivacyInventory {
  return {
    items: KEYS.map((key) => ({ key, count: counts[key] ?? 0, bytes: null, where: [`C:\\JARVIS\\data\\${key}`], can_clear: true, busy: false, ...extra[key] })),
    database: { where: 'C:\\JARVIS\\data\\memory.db', bytes: 2_097_152 },
    kept: [
      { key: 'keys', count: 1, names: ['xAI (Grok) API key'], where: ['keyring'] },
      { key: 'settings', where: ['C:\\JARVIS\\data\\config.toml'] },
      { key: 'work', where: ['C:\\Users\\User\\JARVIS\\workspace', 'C:\\Users\\User\\JARVIS\\projects'] },
      { key: 'models', where: ['C:\\JARVIS\\cache\\models'] },
    ],
    senses: {
      microphone: { enabled: true, state: 'idle', muted: false, wake_word: 'Hey Jarvis', always_listening: true, ctrl_to_talk: true, stt: 'faster_whisper', stt_online: false, tts: 'edge', tts_online: true, ...voice },
      screen: { computer_enabled: true, model: 'local', remote_host: null },
      cloud: { only_when_told: true, ai: [{ name: 'Claude (Anthropic)', configured: false }, { name: 'Grok (xAI)', configured: true }, { name: 'OpenAI', configured: false }] },
    },
  };
}

type Handler = (body: unknown) => unknown;

function core(first: PrivacyInventory, routes: Record<string, Handler> = {}) {
  return async (method: string, path: string, body?: unknown) => {
    const route = `${method} ${path}`;
    if (routes[route]) return routes[route](body);
    if (route === 'GET /api/privacy') return first;
    throw new Error(`unexpected ${route}`);
  };
}

const button = (el: HTMLElement) => el as HTMLButtonElement;

/** A ConfirmButton: the first click arms it, the second does it. */
function twice(el: HTMLElement) {
  fireEvent.click(el);
  fireEvent.click(el);
}

function voiceOn(store: ReturnType<typeof createStore>, muted = false) {
  act(() => store.dispatch({ type: 'event', at: Date.now(), event: { type: 'voice.state', data: { state: 'idle', muted } } }));
}

describe('the privacy screen', () => {
  it('in the simulation says the real core is needed, and asks it nothing', () => {
    const { getByTestId, request } = renderWith(<PrivacyScreen />, { mock: true });
    expect(getByTestId('privacy-simulation').textContent).toContain('the simulation cannot run it');
    expect(request).not.toHaveBeenCalled();
  });

  it('lists every kind of saved data with how much and where, and says what stays', async () => {
    const view = renderWith(<PrivacyScreen />, {
      request: core(inventory({ chat: 12, tasks: 3, jobs: 2, code: 1 }, { tasks: { kept: 1 }, jobs: { running: 1, bytes: 4096 }, code: { busy: true } })),
    });
    await view.findByTestId('privacy-items');
    expect(view.request).toHaveBeenCalledWith('GET', '/api/privacy');
    expect(view.getAllByTestId(/^privacy-item-/)).toHaveLength(KEYS.length);
    expect(view.getByTestId('privacy-count-chat').textContent).toBe('Messages: 12');
    expect(view.getByTestId('privacy-item-tasks').textContent).toContain('In progress or interrupted, staying: 1');
    expect(view.getByTestId('privacy-item-jobs').textContent).toContain('Running, staying: 1');
    expect(view.getByTestId('privacy-item-jobs').textContent).toContain('4');
    expect(view.getByTestId('privacy-item-chat').textContent).toContain('C:\\JARVIS\\data\\chat');
    expect(view.getByTestId('privacy-count-memory').textContent).toBe('Nothing saved');
    expect(button(view.getByTestId('privacy-clear-memory')).disabled).toBe(true);
    // busy: said, and not deletable until that work is done
    expect(view.getByTestId('privacy-item-code').textContent).toContain('Busy right now');
    expect(button(view.getByTestId('privacy-clear-code')).disabled).toBe(true);
    expect(view.getByTestId('privacy-database').textContent).toContain('2');
    // kept on purpose: the keys by name and where (never a value), the user's work, the models
    expect(view.getByTestId('privacy-kept-keys').textContent).toContain('xAI (Grok) API key · Windows Credential Manager');
    expect(view.getByTestId('privacy-kept-work').textContent).toContain('C:\\Users\\User\\JARVIS\\workspace');
    expect(view.getByTestId('privacy-kept-work').textContent).toContain('deletes nothing here except');
  });

  it('deletes one kind only after a second click, and shows the new counts', async () => {
    const clear = vi.fn((): PrivacyCleared => ({ results: { chat: { ok: true, count: 12 } }, inventory: inventory({ tasks: 3 }) }));
    const store = createStore();
    act(() => store.dispatch({ type: 'event', at: Date.now(), event: { type: 'chat.message', data: { id: 'm1', role: 'user', text: 'hi', ts: new Date().toISOString(), time: '03:00', source: 'text', language: 'en', kind: 'answer' } } }));
    const view = renderWith(<PrivacyScreen />, { request: core(inventory({ chat: 12, tasks: 3 }), { 'POST /api/privacy/clear': clear }), store });
    const del = await view.findByTestId('privacy-clear-chat');
    fireEvent.click(del);
    expect(del.textContent).toBe('Delete for good?');
    expect(clear).not.toHaveBeenCalled();
    fireEvent.click(del);
    await waitFor(() => expect(clear).toHaveBeenCalledWith({ items: ['chat'] }));
    await waitFor(() => expect(view.getByTestId('privacy-count-chat').textContent).toBe('Nothing saved'));
    expect(store.getState().chat).toEqual([]);
    expect(store.getState().toasts.at(-1)?.title).toBe('Deleted: The conversation');
  });

  it('"delete everything" sends every kind that has something and is not busy, and says what stayed', async () => {
    const clear = vi.fn((body: unknown): PrivacyCleared => {
      const items = (body as { items: PrivacyKey[] }).items;
      return { results: Object.fromEntries(items.map((k) => [k, k === 'media' ? { ok: false, reason: 'busy' } : { ok: true, count: 1 }])), inventory: inventory({ media: 2 }) };
    });
    const view = renderWith(<PrivacyScreen />, {
      request: core(inventory({ chat: 2, memory: 4, media: 2, logs: 3, code: 1 }, { code: { busy: true } }), { 'POST /api/privacy/clear': clear }),
    });
    twice(await view.findByTestId('privacy-clear-all'));
    await waitFor(() => expect(clear).toHaveBeenCalledWith({ items: ['chat', 'memory', 'media', 'logs'] }));
    await waitFor(() => expect(view.store.getState().toasts.some((t) => t.title.startsWith('Not deleted: Image and video previews (still in use'))).toBe(true));
    expect(view.store.getState().toasts.some((t) => t.title === 'Deleted: The conversation, What JARVIS remembers about you, Logs')).toBe(true);
  });

  it('says exactly what the microphone, the screen and the internet get, and mutes on a click', async () => {
    const voice = vi.fn(async () => ({}) as VoiceInfo);
    const store = createStore();
    voiceOn(store);
    const view = renderWith(<PrivacyScreen />, { request: core(inventory()), api: { voice }, store });
    await view.findByTestId('privacy-senses');
    expect(view.getByTestId('privacy-mic-state').textContent).toBe('Open only to hear “Hey Jarvis”: the sound is checked on this computer, then dropped; nothing is saved or sent.');
    expect(view.getByTestId('privacy-mic').textContent).toContain('A command is recorded only after the wake word or tap Ctrl or the microphone button');
    expect(view.getByTestId('privacy-stt').textContent).toBe('Speech recognition: on this computer.');
    expect(view.getByTestId('privacy-tts').textContent).toContain('goes to Microsoft’s online voice');
    expect(view.getByTestId('privacy-screen-model').textContent).toBe('They go to the computer-control model on this computer.');
    expect(view.getByTestId('privacy-cloud-keys').textContent).toBe('With a key here: Grok (xAI)');
    fireEvent.click(view.getByTestId('privacy-mute'));
    await waitFor(() => expect(voice).toHaveBeenCalledWith('mute'));
  });

  it('with voice off, says the microphone is never opened; a remote model server is named', async () => {
    const first = inventory();
    first.senses.screen = { computer_enabled: true, model: 'remote', remote_host: 'gpu-box.local' };
    first.senses.cloud.ai = first.senses.cloud.ai.map((a) => ({ ...a, configured: false }));
    const view = renderWith(<PrivacyScreen />, { request: core(first) });
    await view.findByTestId('privacy-senses');
    expect(view.getByTestId('privacy-mic-state').textContent).toBe('Voice is off: JARVIS never opens the microphone.');
    expect(view.queryByTestId('privacy-mute')).toBeNull();
    expect(view.getByTestId('privacy-screen-model').textContent).toBe('They go to the model server at gpu-box.local, set in the settings.');
    expect(view.getByTestId('privacy-cloud-keys').textContent).toBe('No cloud AI key is saved, so nothing goes to one.');
  });

  it('speaks Hebrew', async () => {
    const view = renderWith(<PrivacyScreen />, { request: core(inventory({ chat: 5 })), lang: 'he' });
    await view.findByTestId('privacy-items');
    expect(view.getByText('פרטיות ונתונים')).toBeTruthy();
    expect(view.getByTestId('privacy-count-chat').textContent).toBe('הודעות: 5');
    expect(view.getByTestId('privacy-clear-chat').textContent?.trim()).toBe('מחיקה');
  });
});
