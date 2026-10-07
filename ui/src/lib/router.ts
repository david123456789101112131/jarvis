import { useCallback, useSyncExternalStore } from 'react';

export type ScreenId = 'command' | 'agents' | 'tasks' | 'memory' | 'security' | 'settings';

export const SCREENS: readonly ScreenId[] = ['command', 'agents', 'tasks', 'memory', 'security', 'settings'];

export interface Route {
  screen: ScreenId;
  /** Optional sub-route, e.g. the selected task id on the tasks screen. */
  param: string | null;
}

/** `#/tasks/task_123` → {screen: 'tasks', param: 'task_123'}; anything unknown → command center. */
export function parseHash(hash: string): Route {
  const clean = hash.replace(/^#\/?/, '');
  const [head, ...rest] = clean.split('/');
  const screen = (SCREENS as readonly string[]).includes(head) ? (head as ScreenId) : 'command';
  const param = rest.length ? decodeURIComponent(rest.join('/')) || null : null;
  return { screen, param: screen === 'command' ? null : param };
}

export function routeHash(route: Route): string {
  if (route.screen === 'command' && !route.param) return '#/';
  return `#/${route.screen}${route.param ? `/${encodeURIComponent(route.param)}` : ''}`;
}

function subscribe(listener: () => void): () => void {
  window.addEventListener('hashchange', listener);
  return () => window.removeEventListener('hashchange', listener);
}

const getHash = () => window.location.hash;

/** Hash-based routing (works from file:// and any base path, survives reloads). */
export function useRoute(): [Route, (screen: ScreenId, param?: string | null) => void] {
  const hash = useSyncExternalStore(subscribe, getHash, () => '');
  const route = parseHash(hash);
  const navigate = useCallback((screen: ScreenId, param: string | null = null) => {
    const next = routeHash({ screen, param });
    if (window.location.hash !== next) window.location.hash = next;
  }, []);
  return [route, navigate];
}
