import { describe, expect, it } from 'vitest';
import { AGENT_IDS, FACT_CATEGORIES } from '../api/types';
import { DETAIL_KEYS } from '../components/ConfirmModal';
import { SCREENS } from '../lib/router';
import { PIPELINE_ORDER } from '../state/reducer';
import { en, he } from './dict';
import { translate } from './index';

const placeholders = (s: string) => [...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();

describe('dictionaries', () => {
  it('translate the same keys, none of them empty', () => {
    expect(Object.keys(he).sort()).toEqual(Object.keys(en).sort());
    for (const [key, value] of [...Object.entries(en), ...Object.entries(he)]) {
      expect(value.trim(), key).not.toBe('');
    }
  });

  it('use the same placeholders in both languages', () => {
    for (const key of Object.keys(en) as (keyof typeof en)[]) {
      expect(placeholders(he[key]), key).toEqual(placeholders(en[key]));
    }
  });

  // Keys built at run time (`t(\`taskStatus.${status}\`)`) are invisible to the type checker.
  const families: Record<string, readonly string[]> = {
    'agent.name': AGENT_IDS,
    agentStatus: ['offline', 'idle', 'thinking', 'working', 'waiting', 'completed', 'error'],
    taskStatus: ['pending', 'planning', 'queued', 'running', 'waiting', 'verifying', 'completed', 'failed', 'cancelled', 'interrupted', 'skipped'],
    mode: ['chat', 'normal', 'task', 'autonomous', 'debug'],
    'mode.desc': ['chat', 'normal', 'task', 'autonomous', 'debug'],
    'mode.short': ['chat', 'normal', 'task', 'autonomous', 'debug'],
    priority: ['critical', 'high', 'normal', 'low'],
    risk: ['safe', 'low', 'medium', 'high', 'critical'],
    'voice.state': ['disabled', 'idle', 'wake', 'listening', 'processing', 'speaking', 'error'],
    theme: ['blue', 'green', 'orange', 'red', 'purple', 'white'],
    'theme.desc': ['blue', 'green', 'orange', 'red', 'purple', 'white'],
    decision: ['allowed', 'denied', 'confirmed', 'rejected', 'blocked', 'error'],
    outcome: ['pending', 'ok', 'error', 'skipped'],
    source: ['text', 'voice', 'api', 'system'],
    'chat.kind': ['ack', 'plan', 'result', 'error', 'info'], // an answer shows no label
    'confirm.detail': DETAIL_KEYS,
    conn: ['idle', 'connecting', 'open', 'reconnecting', 'closed'],
    step: PIPELINE_ORDER,
    nav: SCREENS,
    'nav.short': SCREENS,
    'memory.cat': FACT_CATEGORIES,
    'memory.layer': ['long_term', 'short_term', 'conversation', 'project', 'task', 'file', 'command'],
    'memory.projectStatus': ['active', 'archived', 'completed'],
    'settings.src': ['keyring', 'env', 'file', 'memory'],
    'tasksScreen.filter': ['all', 'active', 'completed', 'failed', 'cancelled', 'interrupted'],
  };

  it.each(Object.entries(families))('cover every value of %s', (family, values) => {
    for (const value of values) expect(`${family}.${value}` in en, `${family}.${value}`).toBe(true);
  });
});

describe('translate', () => {
  it('fills placeholders and falls back to the key', () => {
    expect(translate('en', 'app.retryIn', { s: 3 })).toBe('Retrying in 3s');
    expect(translate('he', 'nope.missing' as keyof typeof en)).toBe('nope.missing');
  });

  it('uses the singular form for a count of one', () => {
    expect(translate('en', 'core.activeTasks', { n: 1 })).toBe('1 active task');
    expect(translate('en', 'core.activeTasks', { n: 3 })).toBe('3 active tasks');
    expect(translate('he', 'core.activeTasks', { n: 1 })).toBe('משימה פעילה אחת');
    expect(translate('he', 'core.activeTasks', { n: 0 })).toBe('0 משימות פעילות');
  });
});
