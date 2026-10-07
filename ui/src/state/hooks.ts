import { useCallback, useEffect, useRef, useState } from 'react';
import type { AgentAction, AgentId, Mode } from '../api/types';
import { useI18n } from '../i18n';
import { useApi, useAppState, useDispatch, useRunner, useStore } from './context';

/**
 * Make sure the full TaskDetail (plan + timeline) of these tasks is loaded. After that the
 * `task.subtask` / `task.timeline` events keep it current. Re-fetched after every reconnect.
 */
export function useEnsureTaskDetails(ids: readonly string[]) {
  const api = useApi();
  const store = useStore();
  const epoch = useAppState((s) => s.connection.connectedAt);
  const requested = useRef(new Set<string>());
  const lastEpoch = useRef(epoch);
  const key = ids.join(',');
  useEffect(() => {
    if (lastEpoch.current !== epoch) {
      lastEpoch.current = epoch;
      requested.current.clear();
    }
    for (const id of ids) {
      if (requested.current.has(id)) continue;
      requested.current.add(id);
      api
        .getTask(id)
        .then((detail) => store.dispatch({ type: 'task.detail', detail }))
        .catch(() => requested.current.delete(id));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, epoch, api, store]);
}

/** Agent controls (start/stop/restart/kill/release) that update the store with the answer. */
export function useAgentActions() {
  const api = useApi();
  const dispatch = useDispatch();
  const { t } = useI18n();
  const runner = useRunner(t('common.requestFailed'));
  const act = useCallback(
    (id: AgentId, action: AgentAction) =>
      runner.run(`${id}:${action}`, async () => {
        const agent = await api.agentAction(id, action);
        if (agent && typeof agent === 'object' && 'id' in agent) dispatch({ type: 'event', event: { type: 'agent.status', data: agent }, at: Date.now() });
        return agent;
      }),
    [api, dispatch, runner],
  );
  return { act, isPending: runner.isPending };
}

export function useSetMode() {
  const api = useApi();
  const dispatch = useDispatch();
  const { t } = useI18n();
  const runner = useRunner(t('common.requestFailed'));
  const setMode = useCallback(
    (mode: Mode) =>
      runner.run('mode', async () => {
        dispatch({ type: 'event', event: { type: 'mode', data: { mode } }, at: Date.now() });
        const result = await api.setMode(mode);
        dispatch({ type: 'event', event: { type: 'mode', data: { mode: result?.mode ?? mode } }, at: Date.now() });
        return result;
      }),
    [api, dispatch, runner],
  );
  return { setMode, pending: runner.isPending('mode') };
}

/** Send a chat command with an optimistic "sending…" bubble. */
export function useSendCommand() {
  const api = useApi();
  const dispatch = useDispatch();
  return useCallback(
    async (text: string) => {
      const clean = text.trim();
      if (!clean) return false;
      const clientId = `c-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
      dispatch({ type: 'chat.pending', pending: { clientId, text: clean, at: Date.now(), failed: false } });
      try {
        const res = await api.command(clean, 'text');
        const at = Date.now();
        if (res?.message) dispatch({ type: 'event', event: { type: 'chat.message', data: res.message }, at });
        if (res?.reply) dispatch({ type: 'event', event: { type: 'chat.message', data: res.reply }, at });
        dispatch({ type: 'chat.pendingDone', clientId, failed: false });
        return true;
      } catch {
        dispatch({ type: 'chat.pendingDone', clientId, failed: true });
        return false;
      }
    },
    [api, dispatch],
  );
}

/** Keep a scroll container pinned to the bottom unless the user scrolled up or hovers it. */
export function useStickToBottom<T extends HTMLElement>(deps: readonly unknown[], paused: boolean) {
  const ref = useRef<T | null>(null);
  const atBottom = useRef(true);
  const [pinned, setPinned] = useState(true);
  const onScroll = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const bottom = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
    atBottom.current = bottom;
    setPinned(bottom);
  }, []);
  useEffect(() => {
    const el = ref.current;
    if (!el || paused || !atBottom.current) return;
    el.scrollTop = el.scrollHeight;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, paused]);
  const scrollToBottom = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    atBottom.current = true;
    setPinned(true);
    el.scrollTop = el.scrollHeight;
  }, []);
  return { ref, onScroll, scrollToBottom, pinned };
}
