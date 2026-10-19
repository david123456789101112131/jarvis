import { useCallback, useSyncExternalStore } from 'react';

/** Every screen the address can name. The work areas have their own rail item; the others live in the hub
 *  ("history & settings") as its tabs. */
export type ScreenId =
  | 'command'
  | 'code'
  | 'images'
  | 'video'
  | 'apps'
  | 'tasks'
  | 'jobs'
  | 'agents'
  | 'memory'
  | 'security'
  | 'settings'
  | 'privacy';

export const SCREENS: readonly ScreenId[] = [
  'command',
  'code',
  'images',
  'video',
  'apps',
  'tasks',
  'jobs',
  'agents',
  'memory',
  'security',
  'settings',
  'privacy',
];

/** The hub's tabs, in order: the task history first, then the settings-like screens. */
export const HUB_TABS: readonly ScreenId[] = ['tasks', 'jobs', 'agents', 'memory', 'security', 'settings', 'privacy'];

/** What the rail shows (Alt+1…6): the command center, the four work areas, and the hub. */
export type RailId = 'command' | 'code' | 'images' | 'video' | 'apps' | 'hub';
export const RAIL: readonly RailId[] = ['command', 'code', 'images', 'video', 'apps', 'hub'];

export function isHubTab(screen: ScreenId): boolean {
  return HUB_TABS.includes(screen);
}

/** The rail item a screen belongs to. */
export function railOf(screen: ScreenId): RailId {
  return isHubTab(screen) ? 'hub' : (screen as RailId);
}

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

const HUB_KEY = 'jarvis.hubTab';

/** The hub tab the user was on last (the rail's hub item opens it again). */
export function lastHubTab(): ScreenId {
  try {
    const value = window.localStorage.getItem(HUB_KEY) as ScreenId | null;
    return value && HUB_TABS.includes(value) ? value : 'tasks';
  } catch {
    return 'tasks';
  }
}

export function rememberHubTab(screen: ScreenId): void {
  if (!isHubTab(screen)) return;
  try {
    window.localStorage.setItem(HUB_KEY, screen);
  } catch {
    /* storage may be unavailable */
  }
}
