import { act, cleanup, fireEvent, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AppsListing, AppsStatus, ScreenLook, StepProposal } from '../../api/apps';
import type { Job } from '../../api/work';
import { createInitialState } from '../../state/reducer';
import { createStore, type Store } from '../../state/store';
import { renderWith } from '../../test/render';
import { AppsScreen } from './AppsScreen';

afterEach(cleanup);

// ------------------------------------------------------------------ what the core answers
function status(extra: Partial<AppsStatus> = {}, available: Record<string, boolean> = {}): AppsStatus {
  const on: Record<string, boolean> = { launch: true, windows: true, uia: true, vision: true, connectors: false, ...available };
  return {
    ways: Object.entries(on).map(([key, ok]) => ({
      key,
      label: `Way ${key}`,
      detail: key === 'vision' ? 'one screenshot on your click, analysed by the Holo model on this computer; nothing is sent' : `About ${key}`,
      available: ok,
      reason: ok ? null : extra.computer_enabled === false && key !== 'launch' && key !== 'windows' && key !== 'connectors' ? 'computer control is turned off in the settings' : `${key} needs Windows`,
      needs: ok || key === 'connectors' ? [] : extra.computer_enabled === false ? ['Turn computer control on (the button here, or the setting computer.enabled)'] : [`Run JARVIS on a Windows computer (${key})`],
    })),
    stop: [
      { key: 'stop_button', label: 'The Stop button', detail: 'on every action running here' },
      { key: 'esc_twice', label: 'Esc twice', detail: 'stops a look or a step midway' },
      { key: 'kill_switch', label: 'The kill switch', detail: 'stops everything at once' },
    ],
    limits: ['sign-in screens, password fields and Windows Security: JARVIS never types or clicks there'],
    note: 'nothing happens in the background: every look is one screenshot, on your click',
    computer_enabled: true,
    busy: false,
    task: null,
    ...extra,
  };
}

function listing(launch = true): AppsListing {
  const can = launch ? ['open'] : [];
  const why = launch ? null : 'launch needs Windows';
  return {
    installed: [
      { name: 'Notepad', kind: 'exe', source: 'startmenu', id: 'notepad', can, why_not: why },
      { name: 'Google Chrome', kind: 'lnk', source: 'startmenu', id: 'chrome', can, why_not: why },
    ],
    total: 3,
    matched: 2,
    windows: [
      { program: 'notepad.exe', title: 'Untitled - Notepad', pid: 42, windows: 1, window: 'Untitled - Notepad', can: launch ? ['look', 'suggest'] : [], note: launch ? null : 'uia needs Windows' },
      { program: 'KeePass.exe', title: 'Passwords - KeePass', pid: 43, windows: 1, window: 'Passwords - KeePass', can: [], note: 'this is a password manager: JARVIS does not look into it or touch it' },
    ],
    errors: [],
  };
}

function job(extra: Partial<Job> = {}): Job {
  return {
    id: 'job_look', kind: 'apps.look', area: 'apps', title: 'What JARVIS sees on the screen', plan: ['Ask for your approval to see the screen'], real: true,
    status: 'running', progress: null, message: 'Waiting for your approval', result: null, error: null, sends: [], cost: null,
    created_at: new Date().toISOString(), finished_at: null, ms: null, can_retry: false, retry_of: null,
    params: { task_id: 'apps_1', window: '' }, lang: 'en', keep_result: false,
    steps: [
      { key: 'approve', label: 'Your approval', status: 'running', detail: '', ms: null },
      { key: 'look', label: 'The look at the screen', status: 'pending', detail: '', ms: null },
    ],
    ...extra,
  };
}

const SEEN: ScreenLook = {
  purpose: 'look',
  facts: { window: 'Untitled - Notepad', title: 'Untitled - Notepad', program: 'notepad.exe', element: 'Text editor', control: 'Edit', password: false, own: false, sensitive: null },
  blocked: null, analysed: 'model', screen: { width: 1920, height: 1080 }, description: 'Notepad with an empty document', model_error: null,
};

const SEND: StepProposal = {
  kind: 'action', understood: 'The Send button sends the message', model_element: 'Send', action: { action: 'click', x: 960, y: 540, element: 'Send' },
  words: "Click 'Send' in Chat - WhatsApp", window: 'Chat - WhatsApp', program: 'WhatsApp.exe', element: 'Send', control: 'Button',
  risk: 'high', reasons: ['it sends a message'], refused: null, irreversible: true, rule: 'send', asks_again: true,
  preview: "What will happen: Click 'Send' in Chat - WhatsApp, which it sends a message. It cannot be undone once it happens.", can_do: true,
};

type Handler = (body: unknown, path: string) => unknown;

/** A stand-in core: answers the apps routes (or what a test overrides) and fails anything else. */
function core(st: AppsStatus = status(), routes: Record<string, Handler> = {}, list: AppsListing = listing()) {
  return async (method: string, path: string, body?: unknown) => {
    const route = `${method} ${path.split('?')[0]}`;
    if (routes[route]) return routes[route](body, path);
    if (route === 'GET /api/apps/status') return st;
    if (route === 'GET /api/apps/list') return list;
    throw new Error(`unexpected ${route}`);
  };
}

function event(store: Store, data: Job) {
  act(() => store.dispatch({ type: 'event', at: Date.now(), event: { type: 'job', data } }));
}

const button = (el: HTMLElement) => el as HTMLButtonElement;

// ------------------------------------------------------------------ the tests
describe('the apps screen', () => {
  it('in the simulation says the real core is needed, and asks it nothing', () => {
    const { getByTestId, request } = renderWith(<AppsScreen />, { mock: true });
    expect(getByTestId('apps-simulation').textContent).toContain('the simulation cannot run it');
    expect(request).not.toHaveBeenCalled();
  });

  it('says which ways exist here, how to stop, and that nothing happens in the background', async () => {
    const view = renderWith(<AppsScreen />, { request: core() });
    await view.findByTestId('apps-ways');
    expect(view.request).toHaveBeenCalledWith('GET', '/api/apps/status?lang=en');
    expect(view.request).toHaveBeenCalledWith('GET', '/api/apps/list?q=&lang=en');
    expect(view.getByTestId('apps-note').textContent).toContain('nothing happens in the background: every look is one screenshot, on your click');
    expect(view.getByTestId('apps-way-uia-state').textContent).toBe('Available');
    expect(view.getByTestId('apps-way-vision-state').textContent).toBe('Available');
    expect(view.getByTestId('apps-way-connectors-state').textContent).toBe('Not available');
    expect(view.getByTestId('apps-stop').textContent).toContain('Esc twice');
    expect(view.getByTestId('apps-stop').textContent).toContain('The kill switch');
    expect(view.getByTestId('apps-limits').textContent).toContain('JARVIS never types or clicks there');
    expect(button(view.getByTestId('apps-look-button')).disabled).toBe(false);
    expect(view.getByTestId('apps-look-how').textContent).toContain('nothing is sent');
    expect(view.getByTestId('apps-look-none').textContent).toContain('Nothing is looked at until you click');

    const suggest = button(view.getByTestId('apps-suggest'));
    expect(suggest.disabled).toBe(true);
    expect(view.getByTestId('apps-suggest-why').textContent).toBe('Write what should be done first');
    fireEvent.change(view.getByTestId('apps-goal'), { target: { value: 'save the file' } });
    expect(suggest.disabled).toBe(false);
    expect(view.queryByTestId('apps-suggest-why')).toBeNull();
  });

  it('without a Windows desktop every screen action says why and what is needed, and nothing runs', async () => {
    const st = status({}, { launch: false, windows: false, uia: false, vision: false });
    const view = renderWith(<AppsScreen />, { request: core(st, {}, listing(false)) });
    const box = await view.findByTestId('apps-screen-unavailable');
    expect(box.textContent).toContain('uia needs Windows');
    expect(box.textContent).toContain('Run JARVIS on a Windows computer (uia)');
    expect(view.getByTestId('apps-way-vision-why').textContent).toBe('vision needs Windows');

    const look = button(view.getByTestId('apps-look-button'));
    expect(look.disabled).toBe(true);
    expect(view.getByTestId('apps-look-button-why').textContent).toBe('uia needs Windows');
    fireEvent.change(view.getByTestId('apps-goal'), { target: { value: 'save the file' } });
    expect(button(view.getByTestId('apps-suggest')).disabled).toBe(true);
    expect(view.getByTestId('apps-suggest-why').textContent).toBe('vision needs Windows');

    await view.findAllByTestId('apps-installed-row');
    expect(view.getByTestId('apps-open-why').textContent).toBe('Opening apps is not available here: launch needs Windows');
    for (const open of view.getAllByTestId('apps-open')) expect(button(open).disabled).toBe(true);
    expect(view.queryAllByTestId('apps-window-look')).toHaveLength(0);
    expect(view.getAllByTestId('apps-window-note')[0].textContent).toContain('uia needs Windows');

    fireEvent.click(look);
    expect(view.request.mock.calls.filter(([m]) => m === 'POST')).toEqual([]);
  });

  it('with computer control off, one button turns it on and every look still asks first', async () => {
    let st = status({ computer_enabled: false }, { uia: false, vision: false });
    const patchSettings = vi.fn(async () => {
      st = status();
      return {};
    });
    const view = renderWith(<AppsScreen />, { request: core(st, { 'GET /api/apps/status': () => st }), api: { patchSettings } });
    const off = await view.findByTestId('apps-off');
    expect(off.textContent).toContain('Computer control is off');
    expect(off.textContent).toContain('Every look and every step still asks for your approval first.');
    expect(view.queryByTestId('apps-screen-unavailable')).toBeNull(); // said once, at the top
    expect(view.getByTestId('apps-look-button-why').textContent).toBe('computer control is turned off in the settings');

    fireEvent.click(view.getByTestId('apps-turn-on'));
    await waitFor(() => expect(patchSettings).toHaveBeenCalledWith({ computer: { enabled: true } }));
    await waitFor(() => expect(view.queryByTestId('apps-off')).toBeNull());
    expect(button(view.getByTestId('apps-look-button')).disabled).toBe(false);
  });

  it('lists and searches the installed apps, finds new installs, and opens one as a live action', async () => {
    const opened = job({ id: 'job_open', kind: 'apps.open', title: 'Open Notepad', params: { name: 'Notepad' }, steps: [{ key: 'open', label: 'Opening Notepad', status: 'running', detail: '', ms: null }] });
    const found: AppsListing = { ...listing(), installed: [listing().installed[0]], matched: 1 };
    const view = renderWith(<AppsScreen />, {
      request: core(status(), { 'GET /api/apps/list': (_b, path) => (path.includes('q=note') ? found : listing()), 'POST /api/apps/open': () => opened }),
    });
    expect((await view.findByTestId('apps-installed-count')).textContent).toBe('2 of 3');
    expect(view.getAllByTestId('apps-installed-row').map((r) => r.textContent)).toEqual(['NotepadOpen', 'Google ChromeOpen']);

    fireEvent.change(view.getByTestId('apps-search'), { target: { value: 'note' } });
    await waitFor(() => expect(view.request).toHaveBeenCalledWith('GET', '/api/apps/list?q=note&lang=en'));
    await waitFor(() => expect(view.getByTestId('apps-installed-count').textContent).toBe('1 of 3'));
    fireEvent.click(view.getByTestId('apps-rescan'));
    await waitFor(() => expect(view.request).toHaveBeenCalledWith('GET', '/api/apps/list?q=note&lang=en&refresh=1'));

    fireEvent.click(view.getAllByTestId('apps-open')[0]);
    await waitFor(() => expect(view.request).toHaveBeenCalledWith('POST', '/api/apps/open', { name: 'Notepad', lang: 'en' }));
    const card = await view.findByTestId('job-card');
    expect(card.dataset.kind).toBe('apps.open');
    expect(card.textContent).toContain('Open Notepad');
  });

  it('looks once on a click, shows what it found, and keeps a correction for the next look', async () => {
    const view = renderWith(<AppsScreen />, {
      request: core(status(), {
        'POST /api/apps/look': () => job(),
        'POST /api/apps/correct': (body) => ({ task: { id: 'apps_1', goal: '', corrections: [(body as { text: string }).text], done: [] } }),
      }),
    });
    fireEvent.click(await view.findByTestId('apps-look-button'));
    await waitFor(() => expect(view.request).toHaveBeenCalledWith('POST', '/api/apps/look', { task_id: null, window: '', lang: 'en' }));
    expect((await view.findByTestId('apps-look-live')).textContent).toContain('Waiting for your approval');
    expect(button(view.getByTestId('apps-look-button')).disabled).toBe(true); // one look at a time
    expect(view.getByTestId('apps-look-button-why').textContent).toContain('The screen is in use');

    event(view.store, job({ status: 'done', progress: 1, message: '', ms: 900, result: { summary: 'Looked at the screen (notepad.exe)', task_id: 'apps_1', look: SEEN } }));
    expect(view.getByTestId('apps-seen-analysed').textContent).toBe('Seen by the local model');
    expect(view.getByTestId('apps-seen-window').textContent).toBe('Untitled - Notepad');
    expect(view.getByTestId('apps-seen-program').textContent).toBe('notepad.exe');
    expect(view.getByTestId('apps-seen-element').textContent).toBe('Text editorEdit');
    expect(view.getByTestId('apps-seen-description').textContent).toBe('Notepad with an empty document');
    expect(view.getByTestId('apps-seen').textContent).toContain('it was not saved anywhere');

    const save = button(view.getByTestId('apps-correct-save'));
    expect(save.disabled).toBe(true);
    expect(view.getByTestId('apps-correct-why').textContent).toBe('Write what JARVIS got wrong first');
    fireEvent.change(view.getByTestId('apps-correct-text'), { target: { value: '  it is the Save dialog ' } });
    expect(save.disabled).toBe(false);
    fireEvent.click(save);
    await waitFor(() => expect(view.request).toHaveBeenCalledWith('POST', '/api/apps/correct', { text: 'it is the Save dialog', task_id: 'apps_1', lang: 'en' }));
    expect((await view.findByTestId('apps-corrections')).textContent).toBe('it is the Save dialog');
    expect((view.getByTestId('apps-correct-text') as HTMLTextAreaElement).value).toBe('');

    fireEvent.click(view.getByTestId('apps-look-button')); // the next look belongs to the same task
    await waitFor(() => expect(view.request).toHaveBeenCalledWith('POST', '/api/apps/look', { task_id: 'apps_1', window: '', lang: 'en' }));
  });

  it('says plainly when a look had only what Windows reports, or JARVIS would not look in', async () => {
    const facts: ScreenLook = { ...SEEN, analysed: 'facts', screen: null, description: null, facts: { ...SEEN.facts, element: 'Password', control: 'Edit', password: true } };
    const blocked: ScreenLook = { ...SEEN, analysed: 'facts', screen: null, description: null, blocked: 'Windows Security is in front, and JARVIS does not look into it' };
    const st = status({ task: { id: 'apps_1', goal: '', corrections: [], done: [] } }, { vision: false }); // the task the core still holds
    const view = renderWith(<AppsScreen />, { request: core(st) });
    await view.findByTestId('apps-task-end'); // the task was taken over
    expect(view.getByTestId('apps-look-how').textContent).toContain('JARVIS takes no screenshot');
    event(view.store, job({ status: 'done', result: { summary: 'Looked', task_id: 'apps_1', look: facts } }));
    expect(view.getByTestId('apps-seen-analysed').textContent).toBe('What Windows reports only (no screenshot)');
    expect(view.getByTestId('apps-seen-password').textContent).toContain('JARVIS never types there');
    expect(view.queryByTestId('apps-seen-description')).toBeNull();
    event(view.store, job({ id: 'job_look2', status: 'done', result: { summary: 'Looked', task_id: 'apps_1', look: blocked } }));
    expect(view.getByTestId('apps-seen-blocked').textContent).toContain('JARVIS does not look into it');

    event(view.store, job({ id: 'job_look3', status: 'cancelled', message: 'Stopped as you asked' }));
    expect(view.getByTestId('apps-look-failed').textContent).toBe('The look did not happen: Stopped as you asked');
  });

  it('looks at a window from the list, or works in it, and leaves sensitive windows alone', async () => {
    const view = renderWith(<AppsScreen />, { request: core(status(), { 'POST /api/apps/look': () => job({ params: { task_id: 'apps_1', window: 'Untitled - Notepad' } }) }) });
    const rows = await view.findAllByTestId('apps-window-row');
    expect(rows).toHaveLength(2);
    expect(rows[1].textContent).toContain('JARVIS does not look into it or touch it');
    expect(rows[1].querySelector('button')).toBeNull();

    fireEvent.click(view.getByTestId('apps-window-choose'));
    expect(view.getByTestId('apps-target').textContent).toContain('Untitled - Notepad');
    expect(view.getByTestId('apps-window-chosen').textContent).toBe('Chosen');
    fireEvent.click(view.getByTestId('apps-target-clear'));
    expect(view.getByTestId('apps-target').textContent).toContain('The window you were working in');

    fireEvent.click(view.getByTestId('apps-window-look'));
    await waitFor(() => expect(view.request).toHaveBeenCalledWith('POST', '/api/apps/look', { task_id: null, window: 'Untitled - Notepad', lang: 'en' }));
    expect(view.getByTestId('apps-target').textContent).toContain('Untitled - Notepad');
  });

  it('shows the suggested step with where it lands, its risk and what it will do; doing it asks first', async () => {
    const suggestion = job({ id: 'job_sug', kind: 'apps.suggest', title: 'Next step: send the message', params: { task_id: 'apps_1', goal: 'send the message', window: '' } });
    const stepJob = job({ id: 'job_step', kind: 'apps.step', title: 'Do the step: Click', params: { suggestion: 'job_sug', task_id: 'apps_1' }, steps: [{ key: 'approve', label: 'Your approval', status: 'running', detail: '', ms: null }, { key: 'act', label: 'The step itself', status: 'pending', detail: '', ms: null }] });
    const view = renderWith(<AppsScreen />, { request: core(status(), { 'POST /api/apps/suggest': () => suggestion, 'POST /api/apps/step': () => stepJob }) });
    fireEvent.change(await view.findByTestId('apps-goal'), { target: { value: '  send the message ' } });
    fireEvent.click(view.getByTestId('apps-suggest'));
    await waitFor(() => expect(view.request).toHaveBeenCalledWith('POST', '/api/apps/suggest', { goal: 'send the message', task_id: null, window: '', lang: 'en' }));
    expect((await view.findByTestId('apps-suggest-live')).textContent).toContain('Waiting for your approval');
    expect(view.queryByTestId('apps-suggestion')).toBeNull();

    event(view.store, { ...suggestion, status: 'done', message: '', result: { summary: 'Suggested one step (nothing was done)', task_id: 'apps_1', look: { ...SEEN, purpose: 'suggest', description: null }, proposal: SEND } });
    const card = view.getByTestId('apps-suggestion');
    expect(view.getByTestId('apps-suggestion-words').textContent).toBe("Click 'Send' in Chat - WhatsApp");
    expect(view.getByTestId('apps-suggestion-where').textContent).toBe('Chat - WhatsApp');
    expect(view.getByTestId('apps-suggestion-risk').textContent).toBe('Risk: High');
    expect(view.getByTestId('apps-suggestion-irreversible').textContent).toBe('Cannot be undone');
    expect(view.getByTestId('apps-suggestion-preview').textContent).toContain('It cannot be undone once it happens.');
    expect(view.getByTestId('apps-suggestion-again').textContent).toContain('ask you once more');
    expect(card.textContent).toContain('JARVIS asks for your approval first');
    expect(card.textContent).toContain('The Send button sends the message');

    fireEvent.click(view.getByTestId('apps-do'));
    await waitFor(() => expect(view.request).toHaveBeenCalledWith('POST', '/api/apps/step', { suggestion: 'job_sug', lang: 'en' }));
    expect((await view.findByTestId('apps-step-state')).textContent).toContain('Waiting for your approval; nothing has been done yet');
    expect(view.queryByTestId('apps-do')).toBeNull(); // a suggestion is done once
    expect(view.queryByTestId('apps-dismiss')).toBeNull();

    event(view.store, { ...stepJob, steps: [{ ...stepJob.steps[0], status: 'done' }, { ...stepJob.steps[1], status: 'running' }] });
    expect(view.getByTestId('apps-step-state').textContent).toContain('Doing the step');
    event(view.store, { ...stepJob, status: 'done', ms: 1500, result: { summary: 'Clicked', words: "Click 'Send' in Chat - WhatsApp" } });
    expect(view.getByTestId('apps-step-state').textContent).toContain('Done. Ask for the next step when you are ready.');
  });

  it('a declined step says nothing was done; a refused step and an answer offer nothing to do', async () => {
    const st = status({ task: { id: 'apps_1', goal: 'pay the bill', corrections: [], done: [] } });
    const view = renderWith(<AppsScreen />, { request: core(st) });
    await view.findByTestId('apps-task-end'); // the task the core kept was taken over, with its goal
    expect((view.getByTestId('apps-goal') as HTMLTextAreaElement).value).toBe('pay the bill');
    const refused: StepProposal = { ...SEND, refused: 'this is a sign-in screen', can_do: false, preview: null };
    event(view.store, job({ id: 'job_sug', kind: 'apps.suggest', status: 'done', params: { task_id: 'apps_1' }, result: { summary: 'Suggested', task_id: 'apps_1', look: SEEN, proposal: refused } }));
    expect(view.getByTestId('apps-suggestion-refused').textContent).toBe('JARVIS will not do this step: this is a sign-in screen');
    expect(view.queryByTestId('apps-do')).toBeNull();
    expect(view.getByTestId('apps-suggestion-nothing').textContent).toBe('There is nothing to do in this step.');

    const answer: StepProposal = { kind: 'answer', understood: '', model_element: '', answer: 'The bill is already paid.', can_do: false };
    event(view.store, job({ id: 'job_sug2', kind: 'apps.suggest', status: 'done', params: { task_id: 'apps_1' }, result: { summary: 'Suggested', task_id: 'apps_1', look: SEEN, proposal: answer } }));
    expect(view.getByTestId('apps-suggestion').dataset.kind).toBe('answer');
    expect(view.getByTestId('apps-suggestion-answer').textContent).toBe('The bill is already paid.');
    fireEvent.click(view.getByTestId('apps-dismiss'));
    expect(view.queryByTestId('apps-suggestion')).toBeNull();

    event(view.store, job({ id: 'job_sug3', kind: 'apps.suggest', status: 'done', params: { task_id: 'apps_1' }, result: { summary: 'Suggested', task_id: 'apps_1', look: SEEN, proposal: SEND } }));
    event(view.store, job({ id: 'job_step', kind: 'apps.step', status: 'cancelled', message: 'Stopped as you asked', params: { suggestion: 'job_sug3', task_id: 'apps_1' } }));
    expect(view.getByTestId('apps-step-state').textContent).toContain('The step was not done: Stopped as you asked');
  });

  it('every disabled action says why: computer control busy, the kill switch', async () => {
    const busy = renderWith(<AppsScreen />, { request: core(status({ busy: true })) });
    expect((await busy.findByTestId('apps-look-button-why')).textContent).toBe('Computer control is running right now (Esc twice stops it)');
    fireEvent.change(busy.getByTestId('apps-goal'), { target: { value: 'save it' } });
    expect(busy.getByTestId('apps-suggest-why').textContent).toBe('Computer control is running right now (Esc twice stops it)');
    busy.unmount();

    const store = createStore({ ...createInitialState(), killSwitch: { global: true, agents: {} } });
    const killed = renderWith(<AppsScreen />, { request: core(), store });
    expect((await killed.findByTestId('apps-look-button-why')).textContent).toBe('The kill switch is engaged; release it first');
    expect(button(killed.getByTestId('apps-look-button')).disabled).toBe(true);
  });

  it('ends a task: its corrections and remembered approval go', async () => {
    const st = status({ task: { id: 'apps_1', goal: 'save it', corrections: ['it is the Save dialog'], done: ["Click 'Save' in Untitled - Notepad"] } });
    const view = renderWith(<AppsScreen />, { request: core(st, { 'DELETE /api/apps/task/apps_1': () => ({ ended: true }) }) });
    expect((await view.findByTestId('apps-corrections')).textContent).toBe('it is the Save dialog');
    expect(view.getByTestId('apps-done').textContent).toBe("Click 'Save' in Untitled - Notepad");
    fireEvent.click(view.getByTestId('apps-task-end'));
    fireEvent.click(view.getByTestId('apps-task-end')); // the second click confirms
    await waitFor(() => expect(view.request).toHaveBeenCalledWith('DELETE', '/api/apps/task/apps_1'));
    await waitFor(() => expect(view.queryByTestId('apps-corrections')).toBeNull());
    expect(view.queryByTestId('apps-done')).toBeNull();
    expect(view.queryByTestId('apps-task-end')).toBeNull();
  });

  it('speaks Hebrew', async () => {
    const view = renderWith(<AppsScreen />, { request: core(), lang: 'he' });
    await view.findByTestId('apps-ways');
    expect(view.request).toHaveBeenCalledWith('GET', '/api/apps/status?lang=he');
    expect(view.getByTestId('apps-task').textContent).toContain('מה לעשות?');
    expect(view.getByTestId('apps-look-button').textContent).toBe('להסתכל על המסך');
    expect(view.getByTestId('apps-suggest-why').textContent).toBe('קודם לכתוב מה לעשות');
  });
});
