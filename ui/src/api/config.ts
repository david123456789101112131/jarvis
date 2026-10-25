import type { Lang } from './types';

/** What the core writes into the page. It holds no credentials: the page's requests (REST and the
 *  WebSocket) are authorised by the core's HttpOnly session cookie, which scripts cannot read. */
export interface InjectedConfig {
  version?: string;
  wsUrl?: string;
  apiBase?: string;
}

declare global {
  interface Window {
    __JARVIS__?: InjectedConfig;
  }
}

export type AppView = 'app' | 'orb' | 'focus';

export interface BootConfig {
  /** Run the in-browser simulator instead of talking to the core. */
  mock: boolean;
  /** Development only: `?token=` for talking to a running core through the Vite proxy (sent as a header). */
  token: string | null;
  version: string;
  wsUrl: string;
  apiBase: string;
  view: AppView;
  /** Mock only: freeze the voice overlay on a scene (idle, wake, listening, processing, action, speaking, error). */
  scene: string | null;
  /** `?lang=he|en` overrides (and persists) the UI language. */
  lang: Lang | null;
}

interface LocationLike {
  search: string;
  protocol: string;
  host: string;
}

/**
 * Reads how this page was started. The core injects `window.__JARVIS__ = {version, wsUrl}` into
 * index.html. Development builds (`npm run dev`) can also run the built-in simulator: when the page
 * did not come from the core and no `?token=` is given, or with `?mock=1`; `?token=` talks to a
 * running core through the dev proxy. Production builds never simulate anything (and ignore
 * `?token=`): without the core they say it cannot be reached.
 */
export function readBootConfig(loc: LocationLike, injected: InjectedConfig | undefined, dev = false): BootConfig {
  const params = new URLSearchParams(loc.search);
  const mockParam = params.get('mock');
  const token = dev ? params.get('token') || null : null;
  const forcedMock = mockParam === '1' || mockParam === 'true';
  const forcedLive = mockParam === '0' || mockParam === 'false';
  const mock = dev && (forcedMock || (!injected && !token && !forcedLive));
  const wsProto = loc.protocol === 'https:' ? 'wss:' : 'ws:';
  const langParam = params.get('lang');
  return {
    mock,
    token,
    version: injected?.version ?? '',
    wsUrl: injected?.wsUrl ?? `${wsProto}//${loc.host}/ws`,
    apiBase: injected?.apiBase ?? '',
    view: params.get('view') === 'orb' ? 'orb' : params.get('view') === 'focus' ? 'focus' : 'app',
    scene: mock ? params.get('scene') : null,
    lang: langParam === 'he' || langParam === 'en' ? langParam : null,
  };
}
