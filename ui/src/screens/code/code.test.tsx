import { act, cleanup, fireEvent, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { ApiError } from '../../api/client';
import { diffLines, estimateUsd, type DevChange, type DevEngine, type DevReport, type DevStatus } from '../../api/dev';
import type { Job } from '../../api/work';
import type { Store } from '../../state/store';
import { renderWith } from '../../test/render';
import { CodeScreen } from './CodeScreen';

const GROK: DevEngine = { provider: 'xai', name: 'Grok', model: 'grok-4.7', local: false, price: [2, 6], preferred: true };

function status(extra: Partial<DevStatus> = {}): DevStatus {
  return {
    available: true, engines: [GROK], needs: [], prices_checked: '2026-10-19', mode: 'normal', sessions: [],
    tools: { python: true, node: true, git: true }, limits: { files: 24, chars: 160000 }, ...extra,
  };
}

const REPORT = {
  root: 'C:\\proj\\shop', name: 'shop', files: 6, truncated: false, scan_ms: 12,
  languages: [{ name: 'Python', files: 4 }], formats: [], manifests: ['pyproject.toml'], frameworks: ['FastAPI', 'pytest'],
  python: { present: true, dependencies: ['fastapi'], dev_dependencies: ['pytest'], venv: true, venv_python: '3.12.10', pytest_installed: true },
  node: { present: false, dependencies: [], dev_dependencies: [], manager: '', installed: null },
  commands: { test: 'pytest', run: ['python main.py'], build: [], check: [] },
  tools: { python: true, git: true }, style: { indent: '4 spaces', line_endings: 'CRLF', bom: false, max_line: null },
  readme: null, layout: [], tests: { dirs: ['tests'], files: 1, examples: [] }, secret_files: ['.env'],
  missing: [{ key: 'no_repo', he: 'לא במאגר Git', en: 'Not a Git repository' }],
} as unknown as DevReport;

const SESSION = { id: 'dev_1', root: 'C:\\proj\\shop', name: 'shop', opened_at: '2026-10-19T00:00:00Z', writable: false, report: null, last_change: null, last_tests: null };

function job(extra: Partial<Job>): Job {
  return {
    id: 'job_x', kind: 'dev.inspect', area: 'code', title: 'Look at the project shop', plan: ['Read the file names'], real: true,
    status: 'running', steps: [], progress: null, message: '', result: null, error: null, sends: [], cost: null,
    created_at: new Date().toISOString(), finished_at: null, ms: null, can_retry: false, retry_of: null,
    params: { session: 'dev_1' }, lang: 'en', keep_result: false, ...extra,
  };
}

function change(extra: Partial<DevChange> = {}): DevChange {
  return {
    id: 'chg_1', session_id: 'dev_1', root: 'C:\\proj\\shop', request: 'skip negatives', lang: 'en', created_at: new Date().toISOString(),
    understood: 'Skip negative numbers in the total', plan: ['Filter the items', 'Add a test'], assumptions: ['A negative is a refund'],
    questions: ['Should zero count?'], summary: 'total skips negatives', risks: [], tests: { added: true, command: 'pytest', notes: 'a test for -1' },
    status: 'proposed', provider: 'xai', model: 'grok-4.7', effort: 'low', usage: {}, cost_usd: 0.012, sent: ['shop/cart.py'], masked: 1,
    parent_id: null, applied_at: null, undone_at: null, digest: 'abc', undo_digest: null, can_apply: true, writable: false,
    files: [
      { path: 'shop/cart.py', action: 'modify', why: 'the filter', include: true, edited: false, status: 'proposed', added: 1, removed: 1,
        diff: '--- a/shop/cart.py\n+++ b/shop/cart.py\n@@ -1,2 +1,2 @@\n def total(self):\n-    return sum(self.items)\n+    return sum(i for i in self.items if i >= 0)\n',
        content: 'def total(self):\n    return sum(i for i in self.items if i >= 0)\n', editable: true, masked: false, blocked: false,
        problems: [], warnings: [{ code: 'import', text: 'imports requests, which the project does not declare' }], eol: 'CRLF', new_file: false },
    ],
    ...extra,
  };
}

type Call = { method: string; path: string; body: unknown };

/** A stand-in core: answers the code screen's routes and records what was asked. */
function core(over: Record<string, (body: unknown) => unknown> = {}) {
  const calls: Call[] = [];
  const routes: Record<string, (body: unknown) => unknown> = {
    'GET /api/dev/status': () => status(),
    'POST /api/dialog': () => ({ paths: ['C:\\proj\\shop'] }),
    'POST /api/dev/open': () => ({ session: SESSION, job: job({ id: 'job_open' }) }),
    'GET /api/dev/sessions/dev_1/changes': () => [],
    'POST /api/dev/sessions/dev_1/inspect': () => job({ id: 'job_files', kind: 'dev.files', title: 'Find the relevant files in shop' }),
    'POST /api/dev/sessions/dev_1/propose': () => job({ id: 'job_plan', kind: 'dev.propose', title: 'Plan a change: skip negatives', sends: ['the request → Grok (api.x.ai)'] }),
    'GET /api/dev/changes/chg_1': () => change(),
    'POST /api/dev/sessions/dev_1/write': () => ({ ...SESSION, writable: true }),
    'POST /api/dev/changes/chg_1/apply': () => job({ id: 'job_apply', kind: 'dev.apply', params: { change: 'chg_1' } }),
    'POST /api/dev/sessions/dev_1/tests': () => job({ id: 'job_tests', kind: 'dev.tests', title: 'Run the tests of shop' }),
    'POST /api/dev/sessions/dev_1/fix': () => job({ id: 'job_fix', kind: 'dev.fix' }),
    ...over,
  };
  const request = async (method: string, path: string, body?: unknown) => {
    calls.push({ method, path, body });
    const route = routes[`${method} ${path}`];
    if (!route) throw new ApiError(404, 'not_found', `${method} ${path}`);
    return route(body);
  };
  return { request, calls, sent: (method: string, path: string) => calls.filter((c) => c.method === method && c.path === path) };
}

function finish(store: Store, done: Job) {
  act(() => store.dispatch({ type: 'event', at: Date.now(), event: { type: 'job', data: done } }));
}

async function openShop(fake: ReturnType<typeof core>) {
  const view = renderWith(<CodeScreen />, { request: fake.request });
  await waitFor(() => expect(fake.sent('GET', '/api/dev/status')).toHaveLength(1));
  fireEvent.click(view.getByTestId('code-pick-open'));
  await waitFor(() => expect(fake.sent('POST', '/api/dev/open')).toHaveLength(1));
  expect(fake.sent('POST', '/api/dev/open')[0].body).toEqual({ path: 'C:\\proj\\shop', lang: 'en' });
  finish(view.store, job({ id: 'job_open', status: 'done', result: { summary: '6 files', report: REPORT, ranked: [], listing: ['shop/cart.py', 'main.py'], listing_total: 2, git: null } }));
  await waitFor(() => expect(view.getByTestId('code-report')).toBeTruthy());
  return view;
}

describe('the code screen', () => {
  afterEach(cleanup);

  it('says it needs the real core in the simulation', async () => {
    const view = renderWith(<CodeScreen />, { request: async () => { throw new ApiError(501, 'simulation', 'no'); } });
    await waitFor(() => expect(view.getByTestId('code-unavailable').textContent).toContain('needs the real JARVIS core'));
  });

  it('opens the folder the user picks and shows what it found, without AI', async () => {
    const fake = core();
    const view = await openShop(fake);
    expect(view.getByTestId('code-test-command').textContent).toBe('pytest');
    expect(view.getByTestId('code-secret-files').textContent).toContain('.env');
    expect(view.getByTestId('code-missing').textContent).toContain('Not a Git repository');
    expect(view.getByTestId('code-flow-project').getAttribute('data-state')).toBe('done');
    expect(view.getByTestId('code-allow').textContent).toContain('Nothing is written until you press Apply');
  });

  it('finds the files, shows what will be sent and its price, and plans only on the click', async () => {
    const fake = core();
    const view = await openShop(fake);
    fireEvent.change(view.getByTestId('code-request-text'), { target: { value: 'skip negative numbers in the total' } });
    fireEvent.click(view.getByTestId('code-find'));
    await waitFor(() => expect(fake.sent('POST', '/api/dev/sessions/dev_1/inspect')).toHaveLength(1));
    finish(view.store, job({ id: 'job_files', kind: 'dev.files', status: 'done', result: {
      summary: 'Found 2', report: REPORT, listing: ['shop/cart.py', 'tests/test_cart.py', 'main.py'], listing_total: 3, git: null,
      ranked: [{ path: 'shop/cart.py', score: 30, size: 4000, reasons: ['the name matches cart'], lines: [] },
               { path: 'tests/test_cart.py', score: 10, size: 900, reasons: ['the tests of cart.py'], lines: [] }] } }));
    await waitFor(() => expect(view.getByTestId('code-file-shop/cart.py')).toBeTruthy());
    expect(view.getByTestId('code-will-send').textContent).toContain('The text of 2 files');
    expect(view.getByTestId('code-will-send').textContent).toContain('To Grok (grok-4.7), only after you approve.');
    expect(view.getByTestId('code-price').textContent).toMatch(/≈ \$0\.0\d\d–\$0\.\d+/);

    fireEvent.click(view.getByTestId('code-file-tests/test_cart.py'));          // leave the test file out
    expect(view.getByTestId('code-will-send').textContent).toContain('The text of 1 files');
    fireEvent.change(view.getByTestId('code-add-file'), { target: { value: 'nope.py' } });
    fireEvent.click(view.getByTestId('code-add-file-go'));
    expect(view.getByRole('alert').textContent).toContain('not in the project');
    expect(fake.sent('POST', '/api/dev/sessions/dev_1/propose')).toHaveLength(0);

    fireEvent.click(view.getByTestId('code-plan'));
    await waitFor(() => expect(fake.sent('POST', '/api/dev/sessions/dev_1/propose')).toHaveLength(1));
    expect(fake.sent('POST', '/api/dev/sessions/dev_1/propose')[0].body).toMatchObject({
      request: 'skip negative numbers in the total', files: ['shop/cart.py'], mode: 'auto', engine: 'xai', lang: 'en' });
    await waitFor(() => expect(view.getAllByTestId('job-sends')[0].textContent).toContain('Grok (api.x.ai)'));
  });

  it('reviews a change, needs "allow changes" to apply it, and applies on the click', async () => {
    const fake = core();
    const view = await openShop(fake);
    fireEvent.change(view.getByTestId('code-request-text'), { target: { value: 'skip negatives' } });
    fireEvent.click(view.getByTestId('code-plan'));
    await waitFor(() => expect(fake.sent('POST', '/api/dev/sessions/dev_1/propose')).toHaveLength(1));
    finish(view.store, job({ id: 'job_plan', kind: 'dev.propose', status: 'done', result: { summary: 'ready', change_id: 'chg_1' } }));
    await waitFor(() => expect(view.getByTestId('code-change')).toBeTruthy());

    expect(view.getByTestId('code-understood').textContent).toContain('Skip negative numbers');
    expect(view.getByTestId('code-questions').textContent).toContain('Should zero count?');
    expect(view.getByTestId('code-assumptions').textContent).toContain('refund');
    expect(view.getByTestId('code-change-file-shop/cart.py-warnings').textContent).toContain('does not declare');
    const diff = view.getByTestId('code-change-file-shop/cart.py-diff');
    expect(diff.querySelector('.diff__line--add')?.textContent).toContain('if i >= 0');
    expect(diff.querySelector('.diff__line--del')?.textContent).toContain('return sum(self.items)');
    expect(view.getByTestId('code-change').textContent).toContain('$0.012');

    const applyButton = view.getByTestId('code-apply') as HTMLButtonElement;
    expect(applyButton.disabled).toBe(true);
    expect(view.getByTestId('code-apply-why').textContent).toContain('Allow changes in this project');
    fireEvent.click(view.getByTestId('code-allow-toggle'));
    await waitFor(() => expect((view.getByTestId('code-apply') as HTMLButtonElement).disabled).toBe(false));
    expect(fake.sent('POST', '/api/dev/sessions/dev_1/write')[0].body).toEqual({ allow: true, lang: 'en' });
    expect(fake.sent('POST', '/api/dev/changes/chg_1/apply')).toHaveLength(0);           // nothing written yet
    fireEvent.click(view.getByTestId('code-apply'));
    await waitFor(() => expect(fake.sent('POST', '/api/dev/changes/chg_1/apply')).toHaveLength(1));
  });

  it('edits a proposed file and leaves one out', async () => {
    const edited = change({ files: [{ ...change().files[0], edited: true, content: 'mine\n', diff: '+mine\n' }] });
    const fake = core({ 'PUT /api/dev/changes/chg_1/files': () => edited });
    const view = await openShop(fake);
    fireEvent.change(view.getByTestId('code-request-text'), { target: { value: 'x y' } });
    fireEvent.click(view.getByTestId('code-plan'));
    await waitFor(() => expect(fake.sent('POST', '/api/dev/sessions/dev_1/propose')).toHaveLength(1));
    finish(view.store, job({ id: 'job_plan', kind: 'dev.propose', status: 'done', result: { change_id: 'chg_1' } }));
    await waitFor(() => expect(view.getByTestId('code-change-file-shop/cart.py-edit')).toBeTruthy());
    fireEvent.click(view.getByTestId('code-change-file-shop/cart.py-edit'));
    fireEvent.change(view.getByTestId('code-change-file-shop/cart.py-text'), { target: { value: 'mine\n' } });
    fireEvent.click(view.getByTestId('code-change-file-shop/cart.py-save'));
    await waitFor(() => expect(fake.sent('PUT', '/api/dev/changes/chg_1/files')[0].body).toEqual({ path: 'shop/cart.py', content: 'mine\n', lang: 'en' }));
    await waitFor(() => expect(view.getByTestId('code-change-file-shop/cart.py').textContent).toContain('edited by you'));
    fireEvent.click(view.getByTestId('code-change-file-shop/cart.py-include'));
    await waitFor(() => expect(fake.sent('PUT', '/api/dev/changes/chg_1/files')[1].body).toEqual({ path: 'shop/cart.py', include: false, lang: 'en' }));
  });

  it('runs the tests, shows what passed and failed, and asks for a fix with the failures', async () => {
    const fake = core();
    const view = await openShop(fake);
    fireEvent.click(view.getByTestId('code-run-tests'));
    await waitFor(() => expect(fake.sent('POST', '/api/dev/sessions/dev_1/tests')).toHaveLength(1));
    finish(view.store, job({ id: 'job_tests', kind: 'dev.tests', status: 'done', result: {
      summary: 'Tests (pytest): 1 failed, 3 passed', ok: false, runner: 'pytest', command: 'python.exe -m pytest -q --no-header',
      counts: { failed: 1, passed: 3 }, failures: [{ name: 'tests/test_cart.py::test_total', message: 'assert 4 == 5' }],
      exit_code: 1, duration_s: 0.8, timed_out: false, output: 'FAILED tests/test_cart.py::test_total', output_cut: false, hint: '' } }));
    await waitFor(() => expect(view.getByTestId('code-test-results')).toBeTruthy());
    expect(view.getByTestId('code-tests-verdict').textContent).toContain('The tests failed');
    expect(view.getByTestId('code-count-failed').textContent).toContain('1');
    expect(view.getByTestId('code-count-passed').textContent).toContain('3');
    expect(view.getByTestId('code-failures').textContent).toContain('tests/test_cart.py::test_total');
    expect(view.getByTestId('code-tests-command').textContent).toContain('-m pytest');
    expect(view.getByTestId('code-flow-tests').getAttribute('data-state')).toBe('failed');
    fireEvent.click(view.getByTestId('code-fix'));
    await waitFor(() => expect(fake.sent('POST', '/api/dev/sessions/dev_1/fix')[0].body).toMatchObject({ tests_job_id: 'job_tests', engine: 'xai' }));
  });

  it('without an AI, says what is needed and keeps the rest working', async () => {
    const fake = core({ 'GET /api/dev/status': () => status({ engines: [], needs: [{ he: 'מפתח xAI', en: 'An xAI (Grok) API key in Settings > API keys' }] }) });
    const view = await openShop(fake);
    expect(view.getByTestId('code-no-engine').textContent).toContain('An xAI (Grok) API key');
    expect(view.queryByTestId('code-plan')).toBeNull();
    expect((view.getByTestId('code-run-tests') as HTMLButtonElement).disabled).toBe(false);
  });
});

describe('the code screen helpers', () => {
  it('estimates the price like the core and colours a diff', () => {
    const [low, high] = estimateUsd(GROK, [32000], 100, 'auto') ?? [0, 0];
    expect(low).toBeGreaterThan(0.02);
    expect(high).toBeGreaterThan(low);
    expect(estimateUsd({ ...GROK, local: true }, [1], 1, 'auto')).toBeNull();
    expect(diffLines('--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n c\n').map((l) => l.kind)).toEqual(['meta', 'meta', 'hunk', 'del', 'add', 'ctx']);
  });
});
