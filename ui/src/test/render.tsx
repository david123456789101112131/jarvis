import { render } from '@testing-library/react';
import type { ReactElement, ReactNode } from 'react';
import { vi } from 'vitest';
import type { JarvisApi } from '../api/client';
import type { BootConfig } from '../api/config';
import type { Transport } from '../api/transport';
import type { Lang } from '../api/types';
import { I18nProvider } from '../i18n';
import { ServicesProvider } from '../state/context';
import { createStore, type Store } from '../state/store';

export type RequestFn = (method: string, path: string, body?: unknown) => Promise<unknown>;

/** Render a screen or component with the app's providers, a fresh store and a stand-in API. `request` answers
 *  the work routes (`api.request`); `api` adds any of the typed calls. */
export function renderWith(
  ui: ReactElement,
  { request, api, lang = 'en', mock = false, store }: { request?: RequestFn; api?: Partial<JarvisApi>; lang?: Lang; mock?: boolean; store?: Store } = {},
) {
  const requestFn = vi.fn(request ?? (async () => ({})));
  const services = {
    store: store ?? createStore(),
    transport: { api: { request: requestFn, ...api } as unknown as JarvisApi, reconnectNow: () => undefined } as unknown as Transport,
    boot: { mock } as BootConfig,
  };
  const wrapper = ({ children }: { children: ReactNode }) => (
    <ServicesProvider services={services}>
      <I18nProvider initial={lang}>{children}</I18nProvider>
    </ServicesProvider>
  );
  const view = render(ui, { wrapper });
  return { ...view, request: requestFn, store: services.store };
}
