import { useEffect, useMemo, useRef, useState } from 'react';
import type { Lang, QuickTool } from '../../api/types';
import { useI18n } from '../../i18n';
import { useApi, useToast, errorMessage } from '../../state/context';
import { Icon } from '../Icon';

const BUILT_IN: Record<Lang, QuickTool[]> = {
  he: [
    { id: 'chrome', label: 'Chrome', label_he: 'כרום', icon: 'chrome', command: 'פתח את כרום' },
    { id: 'youtube', label: 'YouTube', label_he: 'יוטיוב', icon: 'youtube', command: 'פתח את יוטיוב' },
    { id: 'calculator', label: 'Calculator', label_he: 'מחשבון', icon: 'calculator', command: 'פתח את המחשבון' },
    { id: 'settings', label: 'Settings', label_he: 'הגדרות', icon: 'settings', command: 'פתח את ההגדרות' },
    { id: 'files', label: 'Files', label_he: 'קבצים', icon: 'folder', command: 'פתח את סייר הקבצים' },
  ],
  en: [
    { id: 'chrome', label: 'Chrome', label_he: 'כרום', icon: 'chrome', command: 'open Chrome' },
    { id: 'youtube', label: 'YouTube', label_he: 'יוטיוב', icon: 'youtube', command: 'open YouTube' },
    { id: 'calculator', label: 'Calculator', label_he: 'מחשבון', icon: 'calculator', command: 'open Calculator' },
    { id: 'settings', label: 'Settings', label_he: 'הגדרות', icon: 'settings', command: 'open Settings' },
    { id: 'files', label: 'Files', label_he: 'קבצים', icon: 'folder', command: 'open File Explorer' },
  ],
};

const cache = new Map<Lang, QuickTool[]>();

/** The core's quick tools (their commands are in the UI language); the built-in list until they load. */
export function useQuickTools(): QuickTool[] {
  const api = useApi();
  const { lang } = useI18n();
  const [, setLoaded] = useState(0);
  useEffect(() => {
    let alive = true;
    api
      .getQuickTools(lang)
      .then((list) => {
        if (!alive || !Array.isArray(list) || !list.length) return;
        cache.set(lang, list);
        setLoaded((n) => n + 1);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [api, lang]);
  return cache.get(lang) ?? BUILT_IN[lang];
}

const TOOL_ICONS: Record<string, string> = { files: 'folder', file_explorer: 'folder', explorer: 'folder', browser: 'globe', web: 'globe', calc: 'calculator' };

/** Radial quick-tools menu around the orb (reference state 11). Arrow keys move, Escape closes. */
export function QuickToolsRing({ open, radius, onClose, testId = 'quick-tools' }: { open: boolean; radius: number; onClose: () => void; testId?: string }) {
  const tools = useQuickTools();
  const api = useApi();
  const toast = useToast();
  const { t, lang } = useI18n();
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const items = useMemo(() => tools.slice(0, 8), [tools]);

  useEffect(() => {
    if (open) requestAnimationFrame(() => refs.current[0]?.focus());
  }, [open]);

  if (!open) return null;

  const run = async (tool: QuickTool) => {
    setBusy(tool.id);
    try {
      await api.command(tool.command, 'text');
      onClose();
    } catch (err) {
      toast('error', t('common.requestFailed'), errorMessage(err));
    } finally {
      setBusy(null);
    }
  };

  const onKeyDown = (e: React.KeyboardEvent, index: number) => {
    const n = items.length;
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') {
      e.preventDefault();
      refs.current[(index + 1) % n]?.focus();
    } else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
      e.preventDefault();
      refs.current[(index - 1 + n) % n]?.focus();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      onClose();
    }
  };

  return (
    <div className="qt" role="menu" aria-label={t('core.quickTools')} data-testid={testId} style={{ '--r': `${Math.round(radius)}px` } as React.CSSProperties}>
      {items.map((tool, i) => {
        const angle = -Math.PI / 2 + (i / items.length) * Math.PI * 2;
        const x = Math.cos(angle) * radius;
        const y = Math.sin(angle) * radius;
        const label = lang === 'he' ? tool.label_he || tool.label : tool.label;
        return (
          <button
            key={tool.id}
            ref={(el) => {
              refs.current[i] = el;
            }}
            type="button"
            role="menuitem"
            className="qt__item"
            style={{ '--x': `${x}px`, '--y': `${y}px`, '--i': i } as React.CSSProperties}
            onClick={() => void run(tool)}
            onKeyDown={(e) => onKeyDown(e, i)}
            disabled={busy !== null}
            data-tool={tool.id}
          >
            <span className="qt__icon" data-icon={tool.icon}>
              {busy === tool.id ? <span className="spinner" /> : <Icon name={TOOL_ICONS[tool.icon] ?? tool.icon} size={22} />}
            </span>
            <span className="qt__label">{label}</span>
          </button>
        );
      })}
    </div>
  );
}
