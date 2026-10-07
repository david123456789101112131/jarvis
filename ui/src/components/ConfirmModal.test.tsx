import { cleanup, fireEvent, render, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { JarvisApi } from '../api/client';
import type { BootConfig } from '../api/config';
import type { Transport } from '../api/transport';
import type { ConfirmationRequest } from '../api/types';
import { I18nProvider } from '../i18n';
import { ServicesProvider } from '../state/context';
import { createStore } from '../state/store';
import { ConfirmModal, DetailValue } from './ConfirmModal';

const show = (value: unknown) => render(<I18nProvider initial="he"><DetailValue value={value} /></I18nProvider>).container;

describe('approval details', () => {
  it('lists every path left to right', () => {
    const box = show(['C:\\Users\\USER\\Desktop\\ישן', 'C:\\data\\a.txt']);
    expect([...box.querySelectorAll('li bdi')].map((b) => [b.getAttribute('dir'), b.textContent])).toEqual([
      ['ltr', 'C:\\Users\\USER\\Desktop\\ישן'],
      ['ltr', 'C:\\data\\a.txt'],
    ]);
  });

  it('says yes or no in words and shows the whole input', () => {
    expect(show(true).textContent).toBe('כן');
    const long = { command: 'x'.repeat(500) };
    expect(show(long).querySelector('pre')?.textContent).toContain('x'.repeat(500));
  });
});

function request(extra: Partial<ConfirmationRequest> = {}): ConfirmationRequest {
  return {
    id: 'conf_1', agent_id: 'system', task_id: 'task_1', tool: 'run_command', summary: 'Run: npm run build',
    reasons: ['a command JARVIS cannot confirm is read-only'], risk: 'high', details: { command: 'npm run build' },
    created_at: new Date().toISOString(), expires_at: null, status: 'pending', ...extra,
  };
}

function dialog(req: ConfirmationRequest) {
  const answerConfirmation = vi.fn().mockResolvedValue({ ok: true });
  const services = {
    store: createStore(),
    transport: { api: { answerConfirmation } as unknown as JarvisApi } as unknown as Transport,
    boot: {} as BootConfig,
  };
  const view = render(
    <ServicesProvider services={services}>
      <I18nProvider initial="en">
        <ConfirmModal request={req} waiting={0} onLater={() => undefined} />
      </I18nProvider>
    </ServicesProvider>,
  );
  return { ...view, answerConfirmation };
}

describe('the approval dialog', () => {
  afterEach(cleanup);

  it('offers to remember an approval for the task, for the program it names', async () => {
    const { getByTestId, queryByTestId, container, answerConfirmation } = dialog(request({ scope: 'npm' }));
    expect(queryByTestId('confirm-irreversible')).toBeNull();
    expect(container.querySelector('.check bdi')?.textContent).toBe('npm');
    fireEvent.click(getByTestId('confirm-remember'));
    fireEvent.click(getByTestId('confirm-approve'));
    await waitFor(() => expect(answerConfirmation).toHaveBeenCalledWith('conf_1', true, true));
  });

  it('never offers to remember what cannot be undone, and says why it asks', async () => {
    const { getByTestId, queryByTestId, answerConfirmation } = dialog(request({ irreversible: true, tool: 'delete_path' }));
    expect(getByTestId('confirm-irreversible').textContent).toContain("can't be undone");
    expect(queryByTestId('confirm-remember')).toBeNull();
    fireEvent.click(getByTestId('confirm-approve'));
    await waitFor(() => expect(answerConfirmation).toHaveBeenCalledWith('conf_1', true, false));
  });

  it('never offers to remember tools that always ask, critical actions or actions outside a task', () => {
    for (const extra of [{ always_ask: true }, { risk: 'critical' as const }, { task_id: null }]) {
      const { queryByTestId, unmount } = dialog(request(extra));
      expect(queryByTestId('confirm-remember')).toBeNull();
      unmount();
    }
  });
});
