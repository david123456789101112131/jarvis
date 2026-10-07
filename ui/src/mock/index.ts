import type { BootConfig } from '../api/config';
import type { Transport } from '../api/transport';
import type { Lang, UISettings } from '../api/types';
import { MockApi } from './mockApi';
import { Simulator } from './simulator';

const UI_KEY = 'jarvis.mock.ui';

function loadUi(): Partial<UISettings> | null {
  try {
    const raw = window.localStorage.getItem(UI_KEY);
    return raw ? (JSON.parse(raw) as Partial<UISettings>) : null;
  } catch {
    return null;
  }
}

function saveUi(ui: UISettings) {
  try {
    window.localStorage.setItem(UI_KEY, JSON.stringify(ui));
  } catch {
    /* ignore */
  }
}

export interface MockTransport extends Transport {
  readonly sim: Simulator;
}

/** Transport backed by the in-browser simulator (`?mock=1`, or no core attached). */
export function createMockTransport(boot: BootConfig, getLang: () => Lang, options: { autoplay?: boolean } = {}): MockTransport {
  const sim = new Simulator({ lang: getLang, scene: boot.scene, autoplay: options.autoplay, loadUi, saveUi });
  const api = new MockApi(sim);
  let unsubscribe: (() => void) | null = null;
  let openTimer: ReturnType<typeof setTimeout> | null = null;
  return {
    kind: 'mock',
    api,
    sim,
    connect(handlers) {
      handlers.onStatus({ status: 'connecting', attempt: 0, nextRetryAt: null, lastError: null, connectedAt: null });
      sim.start();
      unsubscribe?.();
      unsubscribe = sim.subscribe(handlers.onEvent);
      openTimer = setTimeout(() => handlers.onStatus({ status: 'open', attempt: 0, nextRetryAt: null, lastError: null, connectedAt: Date.now() }), 40);
    },
    disconnect() {
      if (openTimer) clearTimeout(openTimer);
      unsubscribe?.();
      unsubscribe = null;
      sim.stop();
    },
    send(message) {
      if (message.type === 'command') sim.command(message.text, 'text');
      else if (message.type === 'confirm') sim.answerConfirmation(message.id, message.approved, !!message.remember);
      return true;
    },
    reconnectNow() {
      /* always connected */
    },
  };
}
