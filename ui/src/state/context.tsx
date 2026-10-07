import { createContext, useCallback, useContext, useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';
import { ApiError, type JarvisApi } from '../api/client';
import type { BootConfig } from '../api/config';
import type { Transport } from '../api/transport';
import type { Store } from './store';
import type { AppState, ToastLevel } from './types';

export interface AppServices {
  store: Store;
  transport: Transport;
  boot: BootConfig;
}

const ServicesContext = createContext<AppServices | null>(null);

export function ServicesProvider({ services, children }: { services: AppServices; children: ReactNode }) {
  return <ServicesContext.Provider value={services}>{children}</ServicesContext.Provider>;
}

export function useServices(): AppServices {
  const services = useContext(ServicesContext);
  if (!services) throw new Error('ServicesProvider is missing');
  return services;
}

export function useStore(): Store {
  return useServices().store;
}

export function useApi(): JarvisApi {
  return useServices().transport.api;
}

/** Subscribe to a slice of the app state. The selector must return a stable reference for unchanged data. */
export function useAppState<T>(selector: (state: AppState) => T): T {
  const store = useStore();
  const get = () => selector(store.getState());
  return useSyncExternalStore(store.subscribe, get, get);
}

export function useDispatch() {
  return useStore().dispatch;
}

let toastSeq = 0;

export function useToast() {
  const dispatch = useDispatch();
  return useCallback(
    (level: ToastLevel, title: string, message = '') => {
      toastSeq += 1;
      dispatch({ type: 'toast.push', toast: { id: `local-${Date.now()}-${toastSeq}`, level, title, message, source: 'local' } });
    },
    [dispatch],
  );
}

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return String(err);
}

/**
 * Runs API calls for buttons: tracks which keys are pending (to disable buttons) and turns
 * failures into error toasts. Returns the call's result, or undefined when it failed.
 */
export function useRunner(errorTitle: string) {
  const toast = useToast();
  const [pending, setPending] = useState<ReadonlySet<string>>(() => new Set());
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const run = useCallback(
    async <T,>(key: string, fn: () => Promise<T>): Promise<T | undefined> => {
      setPending((prev) => new Set(prev).add(key));
      try {
        return await fn();
      } catch (err) {
        toast('error', errorTitle, errorMessage(err));
        return undefined;
      } finally {
        if (mounted.current)
          setPending((prev) => {
            const next = new Set(prev);
            next.delete(key);
            return next;
          });
      }
    },
    [toast, errorTitle],
  );
  return { run, pending, isPending: (key: string) => pending.has(key) };
}

/** Re-render periodically (for clocks, countdowns and time-based overlays). */
export function useNow(intervalMs: number, enabled = true): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!enabled) return;
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs, enabled]);
  return now;
}

/** Re-render once at a given time (e.g. when an overlay should disappear). */
export function useWakeAt(at: number | null): void {
  const [, setTick] = useState(0);
  useEffect(() => {
    if (at === null) return;
    const delay = Math.max(0, at - Date.now()) + 16;
    const id = setTimeout(() => setTick((n) => n + 1), delay);
    return () => clearTimeout(id);
  }, [at]);
}

export interface Resource<T> {
  data: T | undefined;
  error: string | null;
  loading: boolean;
  reload: () => void;
  setData: (updater: (prev: T | undefined) => T | undefined) => void;
}

/** Load data from the API on mount (and when deps change), with loading/error state. */
export function useResource<T>(load: (api: JarvisApi) => Promise<T>, deps: readonly unknown[]): Resource<T> {
  const api = useApi();
  const [data, setDataState] = useState<T | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);
  const loadRef = useRef(load);
  loadRef.current = load;
  useEffect(() => {
    let alive = true;
    setLoading(true);
    loadRef
      .current(api)
      .then((value) => {
        if (!alive) return;
        setDataState(value);
        setError(null);
      })
      .catch((err) => {
        if (alive) setError(errorMessage(err));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, nonce, ...deps]);
  const reload = useCallback(() => setNonce((n) => n + 1), []);
  const setData = useCallback((updater: (prev: T | undefined) => T | undefined) => setDataState(updater), []);
  return { data, error, loading, reload, setData };
}
