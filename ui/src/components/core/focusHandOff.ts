import { useSyncExternalStore } from 'react';

// Whether the voice view has taken the command center's orb out (the panel's own orb then steps aside and sleeps,
// so only one of the two is ever drawn).
let out = false;
const listeners = new Set<() => void>();

export function setFocusHandOff(value: boolean): void {
  if (out === value) return;
  out = value;
  for (const listener of [...listeners]) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function useFocusHandOff(): boolean {
  return useSyncExternalStore(subscribe, () => out, () => false);
}
