import { ApiError, HttpApi, type JarvisApi } from './client';
import type { BootConfig } from './config';
import type { ClientMessage, ServerEvent } from './types';
import { WsClient, withToken, type ConnectionInfo } from './ws';

export interface TransportHandlers {
  onEvent: (event: ServerEvent) => void;
  onStatus: (info: ConnectionInfo) => void;
}

/** What the app talks to: a typed REST API plus a live event stream. Real core or simulator. */
export interface Transport {
  readonly kind: 'live' | 'mock';
  readonly api: JarvisApi;
  connect(handlers: TransportHandlers): void;
  disconnect(): void;
  send(message: ClientMessage): boolean;
  reconnectNow(): void;
}

/** False only when the core answers and refuses this page's session; network errors say nothing. */
export async function sessionValid(api: JarvisApi): Promise<boolean> {
  try {
    await api.getConfirmations();
    return true;
  } catch (err) {
    return !(err instanceof ApiError && err.status === 403);
  }
}

export function createLiveTransport(config: BootConfig): Transport {
  const api = new HttpApi({ baseUrl: config.apiBase, token: config.token });
  let client: WsClient | null = null;
  let probing = false;
  return {
    kind: 'live',
    api,
    connect(handlers) {
      client?.stop();
      const ws: WsClient = new WsClient({
        url: withToken(config.wsUrl, config.token),
        onEvent: handlers.onEvent,
        onStatus: (info) => {
          handlers.onStatus(info);
          // The browser reports a refused handshake like any network error. From the second failure in a
          // row, ask the REST API whether this page's session is still good: a restarted JARVIS knows no
          // earlier sessions, and retrying with the old cookie can never work.
          if (info.status !== 'reconnecting' || info.attempt < 2 || info.nextRetryAt === null || probing) return;
          probing = true;
          void sessionValid(api).then((valid) => {
            probing = false;
            if (valid || client !== ws) return;
            ws.stop();
            handlers.onStatus({ ...ws.status, status: 'closed', nextRetryAt: null, fatal: 'forbidden', lastError: 'forbidden' });
          });
        },
      });
      client = ws;
      ws.start();
    },
    disconnect() {
      client?.stop();
      client = null;
    },
    send(message) {
      return client?.send(message) ?? false;
    },
    reconnectNow() {
      client?.reconnectNow();
    },
  };
}
