import { isServerEvent, type ClientMessage, type ServerEvent } from './types';

export type ConnectionStatus = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'closed';

export interface ConnectionInfo {
  status: ConnectionStatus;
  attempt: number;
  nextRetryAt: number | null;
  lastError: string | null;
  connectedAt: number | null;
  /** Set when retrying cannot help: the core refuses this page's session (JARVIS restarted, or a stale tab). */
  fatal?: 'forbidden' | null;
}

export interface BackoffOptions {
  initialMs: number;
  maxMs: number;
  factor: number;
  jitter: number; // 0..1 share of the delay that is randomised
}

export const DEFAULT_BACKOFF: BackoffOptions = { initialMs: 500, maxMs: 15000, factor: 1.8, jitter: 0.25 };

/** Delay before reconnect attempt `attempt` (1-based): exponential, capped, with jitter. */
export function backoffDelay(attempt: number, options: BackoffOptions = DEFAULT_BACKOFF, random: () => number = Math.random): number {
  const n = Math.max(1, attempt);
  const raw = Math.min(options.maxMs, options.initialMs * Math.pow(options.factor, n - 1));
  const spread = raw * options.jitter;
  return Math.round(Math.max(options.initialMs * 0.5, raw - spread + random() * spread * 2));
}

/** Appends the development `token` to a WebSocket URL unless it is already there (the real page has none). */
export function withToken(url: string, token: string | null): string {
  if (!token) return url;
  try {
    const parsed = new URL(url);
    if (!parsed.searchParams.has('token')) parsed.searchParams.set('token', token);
    return parsed.toString();
  } catch {
    return url.includes('token=') ? url : `${url}${url.includes('?') ? '&' : '?'}token=${encodeURIComponent(token)}`;
  }
}

export interface WsClientOptions {
  url: string;
  onEvent: (event: ServerEvent) => void;
  onStatus: (info: ConnectionInfo) => void;
  backoff?: Partial<BackoffOptions>;
  pingIntervalMs?: number;
  staleAfterMs?: number;
  WebSocketImpl?: typeof WebSocket;
}

/**
 * WebSocket client for `/ws`: reconnects with exponential backoff, pings to detect stale
 * connections, and reconnects at once when the network or the page comes back.
 */
export class WsClient {
  private readonly opts: WsClientOptions;
  private readonly backoff: BackoffOptions;
  private ws: WebSocket | null = null;
  private stopped = true;
  private attempt = 0;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private lastMessageAt = 0;
  private info: ConnectionInfo = { status: 'idle', attempt: 0, nextRetryAt: null, lastError: null, connectedAt: null };

  constructor(opts: WsClientOptions) {
    this.opts = opts;
    this.backoff = { ...DEFAULT_BACKOFF, ...opts.backoff };
  }

  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    if (typeof window !== 'undefined') {
      window.addEventListener('online', this.handleWake);
      document.addEventListener('visibilitychange', this.handleVisibility);
    }
    this.open();
  }

  stop(): void {
    this.stopped = true;
    if (typeof window !== 'undefined') {
      window.removeEventListener('online', this.handleWake);
      document.removeEventListener('visibilitychange', this.handleVisibility);
    }
    this.clearTimers();
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      ws.onopen = ws.onclose = ws.onerror = ws.onmessage = null;
      try {
        ws.close(1000, 'client stop');
      } catch {
        /* ignore */
      }
    }
    this.setInfo({ status: 'closed', nextRetryAt: null });
  }

  send(message: ClientMessage): boolean {
    const Impl = this.opts.WebSocketImpl ?? WebSocket;
    if (!this.ws || this.ws.readyState !== Impl.OPEN) return false;
    this.ws.send(JSON.stringify(message));
    return true;
  }

  /** Skip the backoff wait and try again now. */
  reconnectNow(): void {
    if (this.stopped || this.info.status === 'open' || this.info.status === 'connecting') return;
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = null;
    this.open();
  }

  get status(): ConnectionInfo {
    return this.info;
  }

  private handleWake = () => this.reconnectNow();
  private handleVisibility = () => {
    if (document.visibilityState === 'visible') this.reconnectNow();
  };

  private setInfo(patch: Partial<ConnectionInfo>) {
    this.info = { ...this.info, ...patch };
    this.opts.onStatus(this.info);
  }

  private clearTimers() {
    if (this.retryTimer) clearTimeout(this.retryTimer);
    if (this.pingTimer) clearInterval(this.pingTimer);
    this.retryTimer = null;
    this.pingTimer = null;
  }

  private open() {
    if (this.stopped) return;
    const Impl = this.opts.WebSocketImpl ?? WebSocket;
    this.setInfo({ status: this.attempt > 0 ? 'reconnecting' : 'connecting', nextRetryAt: null, attempt: this.attempt });
    let ws: WebSocket;
    try {
      ws = new Impl(this.opts.url);
    } catch (err) {
      this.setInfo({ lastError: err instanceof Error ? err.message : String(err) });
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      if (this.ws !== ws) return;
      this.attempt = 0;
      this.lastMessageAt = Date.now();
      this.setInfo({ status: 'open', attempt: 0, lastError: null, connectedAt: Date.now(), nextRetryAt: null });
      this.startHeartbeat();
    };
    ws.onmessage = (msg: MessageEvent) => {
      if (this.ws !== ws) return;
      this.lastMessageAt = Date.now();
      if (typeof msg.data !== 'string') return;
      let parsed: unknown;
      try {
        parsed = JSON.parse(msg.data);
      } catch {
        return;
      }
      if (isServerEvent(parsed)) this.opts.onEvent(parsed);
    };
    ws.onerror = () => {
      if (this.ws !== ws) return;
      this.setInfo({ lastError: 'connection error' });
    };
    ws.onclose = (ev: CloseEvent) => {
      if (this.ws !== ws) return;
      this.ws = null;
      if (this.pingTimer) clearInterval(this.pingTimer);
      this.pingTimer = null;
      if (ev.code !== 1000 && ev.code !== 1005) this.setInfo({ lastError: ev.reason || `closed (${ev.code})` });
      this.scheduleReconnect();
    };
  }

  private startHeartbeat() {
    const interval = this.opts.pingIntervalMs ?? 20000;
    const stale = this.opts.staleAfterMs ?? 50000;
    if (this.pingTimer) clearInterval(this.pingTimer);
    this.pingTimer = setInterval(() => {
      if (!this.ws) return;
      if (Date.now() - this.lastMessageAt > stale) {
        try {
          this.ws.close(4000, 'stale');
        } catch {
          /* ignore */
        }
        return;
      }
      this.send({ type: 'ping' });
    }, interval);
  }

  private scheduleReconnect() {
    if (this.stopped) return;
    this.attempt += 1;
    const delay = backoffDelay(this.attempt, this.backoff);
    this.setInfo({ status: 'reconnecting', attempt: this.attempt, nextRetryAt: Date.now() + delay });
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      this.open();
    }, delay);
  }
}
