import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import type { AgentId, AgentView, Lang } from '../api/types';
import { dictionaries, type MessageKey } from './dict';

export type { MessageKey } from './dict';

export const LANG_STORAGE_KEY = 'jarvis.lang';

export type Params = Record<string, string | number>;

/** A count of one uses the key's `_one` variant when the dictionary has it ("1 active task",
 *  "משימה פעילה אחת"); every other count uses the key itself. */
function pick(dict: Record<string, string>, key: string, one: boolean): string | undefined {
  return (one ? dict[`${key}_one`] : undefined) ?? dict[key];
}

export function translate(lang: Lang, key: MessageKey, params?: Params): string {
  const one = params?.n === 1;
  const template = pick(dictionaries[lang], key, one) ?? pick(dictionaries.en, key, one) ?? key;
  if (!params) return template;
  return template.replace(/\{(\w+)\}/g, (match, name: string) => (name in params ? String(params[name]) : match));
}

export function readStoredLang(): Lang | null {
  try {
    const value = window.localStorage.getItem(LANG_STORAGE_KEY);
    return value === 'he' || value === 'en' ? value : null;
  } catch {
    return null;
  }
}

function storeLang(lang: Lang) {
  try {
    window.localStorage.setItem(LANG_STORAGE_KEY, lang);
  } catch {
    /* storage may be unavailable */
  }
}

export interface I18n {
  lang: Lang;
  dir: 'rtl' | 'ltr';
  t: (key: MessageKey, params?: Params) => string;
  setLang: (lang: Lang) => void;
  toggleLang: () => void;
  agentName: (agent: AgentView | AgentId | string | null | undefined) => string;
  agentRole: (agent: AgentView) => string;
}

const I18nContext = createContext<I18n | null>(null);

export function I18nProvider({ initial, children }: { initial: Lang; children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(initial);
  const dir = lang === 'he' ? 'rtl' : 'ltr';

  useEffect(() => {
    const root = document.documentElement;
    root.lang = lang;
    root.dir = dir;
  }, [lang, dir]);

  const setLang = useCallback((next: Lang) => {
    setLangState(next);
    storeLang(next);
  }, []);

  const value = useMemo<I18n>(() => {
    const t = (key: MessageKey, params?: Params) => translate(lang, key, params);
    return {
      lang,
      dir,
      t,
      setLang,
      toggleLang: () => setLang(lang === 'he' ? 'en' : 'he'),
      agentName: (agent) => {
        if (!agent) return 'JARVIS';
        if (typeof agent === 'string') {
          const key = `agent.name.${agent}` as MessageKey;
          return key in dictionaries.en ? t(key) : agent;
        }
        return lang === 'he' ? agent.name_he || agent.name : agent.name;
      },
      agentRole: (agent) => (lang === 'he' ? agent.role_he || agent.role : agent.role),
    };
  }, [lang, dir, setLang]);

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18n {
  const value = useContext(I18nContext);
  if (!value) throw new Error('I18nProvider is missing');
  return value;
}
