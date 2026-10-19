import { act, cleanup, fireEvent, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { ApiError } from '../../api/client';
import type { Job } from '../../api/work';
import { isHubTab, parseHash, railOf, rememberHubTab, lastHubTab } from '../../lib/router';
import { createInitialState, reducer } from '../../state/reducer';
import { renderWith } from '../../test/render';
import { JobCard } from './JobCard';
import { PathPicker } from './PathPicker';

export function job(extra: Partial<Job> = {}): Job {
  return {
    id: 'job_1', kind: 'dev.inspect', area: 'code', title: 'Inspect the project', plan: ['Read the files', 'Find the tests'],
    real: true, status: 'running', progress: null, message: 'Reading', result: null, error: null, sends: [], cost: null,
    created_at: new Date().toISOString(), finished_at: null, ms: null, can_retry: false, retry_of: null, params: {}, lang: 'en',
    keep_result: true,
    steps: [
      { key: 'scan', label: 'Scan files', status: 'done', detail: '', ms: 340 },
      { key: 'tests', label: 'Find tests', status: 'running', detail: '', ms: null },
    ],
    ...extra,
  };
}

describe('the areas and the hub', () => {
  it('routes the work areas and keeps the old screens as hub tabs', () => {
    expect(parseHash('#/code').screen).toBe('code');
    expect(parseHash('#/images').screen).toBe('images');
    expect(parseHash('#/agents/research')).toEqual({ screen: 'agents', param: 'research' });
    expect(parseHash('#/nope').screen).toBe('command');
    expect(railOf('video')).toBe('video');
    expect(railOf('security')).toBe('hub');
    expect(isHubTab('privacy') && isHubTab('jobs') && !isHubTab('apps')).toBe(true);
    rememberHubTab('memory');
    expect(lastHubTab()).toBe('memory');
    rememberHubTab('code'); // not a hub tab: ignored
    expect(lastHubTab()).toBe('memory');
  });
});

describe('the jobs in the store', () => {
  it('takes the snapshot and then each job event, newest first', () => {
    let state = reducer(createInitialState(), { type: 'event', at: 1, event: { type: 'state', data: { ...snapshot(), jobs: [job({ id: 'job_old', status: 'done' })] } } });
    expect(state.jobs.map((j) => j.id)).toEqual(['job_old']);
    state = reducer(state, { type: 'event', at: 2, event: { type: 'job', data: job({ id: 'job_new' }) } });
    state = reducer(state, { type: 'event', at: 3, event: { type: 'job', data: job({ id: 'job_old', status: 'failed' }) } });
    expect(state.jobs.map((j) => [j.id, j.status])).toEqual([
      ['job_new', 'running'],
      ['job_old', 'failed'],
    ]);
  });
});

describe('the job card', () => {
  afterEach(cleanup);

  it('says what it understood, what it does, that it is real and what leaves the computer', () => {
    const { getByTestId } = renderWith(<JobCard job={job({ sends: ['the request → Grok (xAI)'], cost: '≈ $0.01' })} />);
    expect(getByTestId('job-card').textContent).toContain('Inspect the project');
    expect(getByTestId('job-plan').textContent).toContain('Find the tests');
    expect(getByTestId('job-real').textContent).toBe('Real action');
    expect(getByTestId('job-sends').textContent).toContain('Grok (xAI)');
    expect(getByTestId('job-sends').textContent).toContain('$0.01');
    expect(getByTestId('job-step-scan').textContent).toContain('0.34');
  });

  it('marks a demonstration and that nothing leaves the computer', () => {
    const { getByTestId } = renderWith(<JobCard job={job({ real: false })} />);
    expect(getByTestId('job-real').textContent).toBe('Demonstration');
    expect(getByTestId('job-sends').textContent).toBe('Nothing leaves the computer');
  });

  it('stops a running job and retries a stopped one', async () => {
    const { getByTestId, request, rerender } = renderWith(<JobCard job={job()} />);
    fireEvent.click(getByTestId('job-stop'));
    await waitFor(() => expect(request).toHaveBeenCalledWith('POST', '/api/jobs/job_1/cancel', {}));
    rerender(<JobCard job={job({ status: 'cancelled', can_retry: true, message: 'Stopped as you asked', ms: 1200 })} />);
    expect(() => getByTestId('job-stop')).toThrow();
    expect(getByTestId('job-note').textContent).toContain('Stopped as you asked');
    fireEvent.click(getByTestId('job-retry'));
    await waitFor(() => expect(request).toHaveBeenCalledWith('POST', '/api/jobs/job_1/retry', {}));
  });

  it('shows a failure in words', () => {
    const { getByTestId } = renderWith(<JobCard job={job({ status: 'failed', error: 'No connection to the image service', ms: 50 })} />);
    expect(getByTestId('job-error').textContent).toContain('No connection to the image service');
    expect(getByTestId('job-status').textContent).toBe('Failed');
  });
});

describe('the path picker', () => {
  afterEach(cleanup);

  it('uses the window dialog and reports what the user picked', async () => {
    let picked: string | null = null;
    const { getByTestId, request } = renderWith(<PathPicker kind="folder" value={null} onChange={(p) => (picked = p)} />, {
      request: async () => ({ paths: ['C:\\Users\\me\\Projects\\app'] }),
    });
    fireEvent.click(getByTestId('pick-folder-open'));
    await waitFor(() => expect(picked).toBe('C:\\Users\\me\\Projects\\app'));
    expect(request.mock.calls[0][0]).toBe('POST');
    expect(request.mock.calls[0][1]).toBe('/api/dialog');
    expect((request.mock.calls[0][2] as { kind: string }).kind).toBe('folder');
  });

  it('a cancelled dialog changes nothing', async () => {
    let calls = 0;
    const { getByTestId } = renderWith(<PathPicker kind="open" value="C:\\a.png" onChange={() => (calls += 1)} />, { request: async () => ({ paths: null }) });
    await act(async () => {
      fireEvent.click(getByTestId('pick-open-open'));
    });
    expect(calls).toBe(0);
  });

  it('without a window it asks for a typed path', async () => {
    let picked: string | null = null;
    const { getByTestId, findByTestId } = renderWith(<PathPicker kind="save" value={null} onChange={(p) => (picked = p)} />, {
      request: async () => {
        throw new ApiError(409, 'conflict', 'no window');
      },
    });
    fireEvent.click(getByTestId('pick-save-open'));
    const input = await findByTestId('pick-save-typed');
    fireEvent.change(input, { target: { value: '  D:\\out\\a.png ' } });
    fireEvent.click(getByTestId('pick-save-use'));
    expect(picked).toBe('D:\\out\\a.png');
  });
});

function snapshot() {
  return {
    version: '1.0.0', assistant_name: 'JARVIS', user_name: '', language: 'he' as const, mode: 'normal' as const,
    voice: createInitialState().voice, ui: createInitialState().ui, agents: [], tasks: { active: [], recent: [] }, chat: [],
    activity: [], confirmations: [], kill_switch: { global: false, agents: {} }, system: null,
    providers: createInitialState().providers, files: { workspace: '', projects: '' },
  };
}
