// Fonts are bundled from @fontsource (the app must work offline): Heebo for body text and
// Hebrew, Orbitron for HUD headings and Latin numbers, JetBrains Mono for logs.
import '@fontsource/heebo/hebrew-400.css';
import '@fontsource/heebo/hebrew-500.css';
import '@fontsource/heebo/hebrew-600.css';
import '@fontsource/heebo/hebrew-700.css';
import '@fontsource/heebo/latin-400.css';
import '@fontsource/heebo/latin-500.css';
import '@fontsource/heebo/latin-600.css';
import '@fontsource/heebo/latin-700.css';
import '@fontsource/orbitron/latin-500.css';
import '@fontsource/orbitron/latin-600.css';
import '@fontsource/orbitron/latin-700.css';
import '@fontsource/jetbrains-mono/latin-400.css';
import '@fontsource/jetbrains-mono/latin-500.css';

import './styles/tokens.css';
import './styles/base.css';
import './styles/components.css';
import './styles/core.css';
import './styles/layout.css';
import './styles/panels.css';
import './styles/screens.css';
import './styles/work.css';
import './styles/code.css';
import './styles/images.css';
import './styles/video.css';
import './styles/apps.css';

import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { readBootConfig } from './api/config';
import { createLiveTransport, type Transport } from './api/transport';
import type { Lang } from './api/types';
import { LANG_STORAGE_KEY, readStoredLang } from './i18n';
import type { Simulator } from './mock/simulator';
import { createStore } from './state/store';

declare global {
  interface Window {
    /** Development builds in mock mode only: drive the simulator from the console or from tests. */
    jarvisMock?: { sim: Simulator; scene: (name: string | null) => void; trigger: (name: string) => void };
  }
}

async function boot() {
  const config = readBootConfig(window.location, window.__JARVIS__, import.meta.env.DEV);
  if (config.lang) {
    try {
      window.localStorage.setItem(LANG_STORAGE_KEY, config.lang);
    } catch {
      /* storage may be unavailable */
    }
  }
  const lang: Lang = config.lang ?? readStoredLang() ?? 'he';
  document.documentElement.lang = lang;
  document.documentElement.dir = lang === 'he' ? 'rtl' : 'ltr';
  if (config.view === 'orb') document.documentElement.classList.add('view-orb');

  let currentLang = lang;
  const store = createStore();
  let transport: Transport;
  if (import.meta.env.DEV && config.mock) {
    // Development only: `import.meta.env.DEV` is false in `npm run build`, so the simulator is not even bundled.
    const { createMockTransport } = await import('./mock');
    const mock = createMockTransport(config, () => currentLang);
    window.jarvisMock = { sim: mock.sim, scene: (name) => mock.sim.setScene(name), trigger: (name) => mock.sim.trigger(name) };
    transport = mock;
  } else {
    transport = createLiveTransport(config);
  }

  transport.connect({
    onEvent: (event) => store.dispatch({ type: 'event', event, at: Date.now() }),
    onStatus: (info) => store.dispatch({ type: 'connection', info }),
  });

  const root = document.getElementById('root');
  if (!root) throw new Error('#root is missing');
  createRoot(root).render(
    <StrictMode>
      <App
        services={{ store, transport, boot: config }}
        initialLang={lang}
        onLangChange={(next) => {
          currentLang = next;
        }}
      />
    </StrictMode>,
  );
}

void boot();
