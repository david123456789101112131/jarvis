import { describe, expect, it } from 'vitest';
import { readBootConfig } from './config';
import { backoffDelay, withToken } from './ws';

const at = (search = '') => ({ search, protocol: 'http:', host: '127.0.0.1:8765' });
const injected = { version: '1.0.0', wsUrl: 'ws://127.0.0.1:8765/ws', apiBase: '' };

describe('readBootConfig', () => {
  it('talks to the core that served the page, with no credentials of its own', () => {
    const config = readBootConfig(at(), injected, false);
    expect(config).toMatchObject({ mock: false, token: null, wsUrl: 'ws://127.0.0.1:8765/ws', view: 'app' });
  });

  it('never simulates or takes a token from the address in a production build', () => {
    expect(readBootConfig(at('?mock=1&token=abc'), injected, false)).toMatchObject({ mock: false, token: null });
    expect(readBootConfig(at('?mock=1'), undefined, false).mock).toBe(false);
  });

  it('runs the simulator in development unless a core is asked for', () => {
    expect(readBootConfig(at(), undefined, true).mock).toBe(true);
    expect(readBootConfig(at('?token=abc'), undefined, true)).toMatchObject({ mock: false, token: 'abc' });
    expect(readBootConfig(at('?mock=0'), undefined, true).mock).toBe(false);
    expect(readBootConfig(at('?mock=1&scene=listening'), injected, true)).toMatchObject({ mock: true, scene: 'listening' });
  });

  it('reads the view and the language', () => {
    expect(readBootConfig(at('?view=orb&lang=en'), injected, false)).toMatchObject({ view: 'orb', lang: 'en', scene: null });
    expect(readBootConfig(at('?lang=fr'), injected, false).lang).toBeNull();
  });
});

describe('websocket helpers', () => {
  it('adds a development token only when there is one', () => {
    expect(withToken('ws://127.0.0.1:8765/ws', null)).toBe('ws://127.0.0.1:8765/ws');
    expect(withToken('ws://127.0.0.1:8765/ws', 'a b')).toBe('ws://127.0.0.1:8765/ws?token=a+b');
    expect(withToken('ws://127.0.0.1:8765/ws?token=x', 'y')).toBe('ws://127.0.0.1:8765/ws?token=x');
  });

  it('backs off exponentially within bounds', () => {
    const mid = () => 0.5;
    expect(backoffDelay(1, undefined, mid)).toBe(500);
    expect(backoffDelay(2, undefined, mid)).toBe(900);
    expect(backoffDelay(50, undefined, mid)).toBe(15000);
    for (let i = 1; i < 30; i += 1) {
      const d = backoffDelay(i);
      expect(d).toBeGreaterThanOrEqual(250);
      expect(d).toBeLessThanOrEqual(15000 * 1.25);
    }
  });
});
