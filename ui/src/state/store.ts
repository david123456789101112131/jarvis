import { createInitialState, reducer } from './reducer';
import type { Action, AppState } from './types';

export interface Store {
  getState(): AppState;
  dispatch(action: Action): void;
  subscribe(listener: () => void): () => void;
}

/** Minimal external store (pure reducer + listeners) consumed with useSyncExternalStore. */
export function createStore(initial: AppState = createInitialState()): Store {
  let state = initial;
  const listeners = new Set<() => void>();
  return {
    getState: () => state,
    dispatch(action) {
      const next = reducer(state, action);
      if (next === state) return;
      state = next;
      for (const listener of [...listeners]) listener();
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}
