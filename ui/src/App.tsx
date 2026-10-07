import { useEffect, useMemo, useState } from 'react';
import type { Lang } from './api/types';
import { BootScreen } from './components/BootScreen';
import { ConfirmModal } from './components/ConfirmModal';
import { Rail, TopBar, useScreenShortcuts } from './components/shell/Shell';
import { Toasts } from './components/Toasts';
import { I18nProvider, useI18n } from './i18n';
import { SCREENS, useRoute, type Route, type ScreenId } from './lib/router';
import { AgentsScreen } from './screens/AgentsScreen';
import { CommandCenter } from './screens/command/CommandCenter';
import { MemoryScreen } from './screens/MemoryScreen';
import { OrbView } from './screens/OrbView';
import { SecurityScreen } from './screens/SecurityScreen';
import { SettingsScreen } from './screens/SettingsScreen';
import { TasksScreen } from './screens/TasksScreen';
import { ServicesProvider, useAppState, type AppServices } from './state/context';

/** Mirrors the theme color onto <html data-theme> (CSS accents) — the orb reads it from the store. */
function ThemeSync() {
  const theme = useAppState((s) => s.ui.theme_color);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);
  return null;
}

function LangSync({ onChange }: { onChange?: (lang: Lang) => void }) {
  const { lang } = useI18n();
  useEffect(() => {
    onChange?.(lang);
  }, [lang, onChange]);
  return null;
}

function ScreenSwitch({ route, navigate }: { route: Route; navigate: (screen: ScreenId, param?: string | null) => void }) {
  switch (route.screen) {
    case 'agents':
      return <AgentsScreen selected={route.param} onSelect={(id: string) => navigate('agents', id)} onNavigate={navigate} />;
    case 'tasks':
      return <TasksScreen selected={route.param} onSelect={(id: string | null) => navigate('tasks', id)} />;
    case 'memory':
      return <MemoryScreen onNavigate={navigate} />;
    case 'security':
      return <SecurityScreen onNavigate={navigate} />;
    case 'settings':
      return <SettingsScreen />;
    default:
      return <CommandCenter onNavigate={navigate} />;
  }
}

function AppShell() {
  const { t } = useI18n();
  const [route, navigate] = useRoute();
  const hydrated = useAppState((s) => s.hydrated);
  const confirmations = useAppState((s) => s.confirmations);
  const pending = useMemo(
    () => confirmations.filter((c) => c.status === 'pending').sort((a, b) => (a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : 0)),
    [confirmations],
  );
  const [deferred, setDeferred] = useState<ReadonlySet<string>>(() => new Set());
  const current = pending.find((c) => !deferred.has(c.id)) ?? null;
  useScreenShortcuts(navigate);

  // "Show me the agents" (by voice or chat): the core asks the UI to switch screens.
  const navigation = useAppState((s) => s.navigation);
  useEffect(() => {
    if (!navigation) return;
    const screen = (SCREENS as readonly string[]).includes(navigation.screen) ? (navigation.screen as ScreenId) : null;
    if (screen) navigate(screen, navigation.param);
  }, [navigation, navigate]);

  if (!hydrated) return <BootScreen />;

  return (
    <div className="app" data-screen={route.screen} data-testid="app-shell">
      <button type="button" className="skip-link" onClick={() => document.getElementById('main')?.focus()}>
        {t('app.skipToContent')}
      </button>
      <Rail current={route.screen} onNavigate={navigate} approvals={pending.length} />
      <TopBar approvals={pending.length} onOpenApprovals={() => setDeferred(new Set())} />
      <main id="main" tabIndex={-1} className={`main main--${route.screen}`} data-testid={`screen-${route.screen}`}>
        <ScreenSwitch route={route} navigate={navigate} />
      </main>
      {current ? <ConfirmModal key={current.id} request={current} waiting={pending.length - 1} onLater={() => setDeferred((prev) => new Set(prev).add(current.id))} /> : null}
      <Toasts />
    </div>
  );
}

export function App({ services, initialLang, onLangChange }: { services: AppServices; initialLang: Lang; onLangChange?: (lang: Lang) => void }) {
  return (
    <ServicesProvider services={services}>
      <I18nProvider initial={initialLang}>
        <ThemeSync />
        <LangSync onChange={onLangChange} />
        {services.boot.view === 'orb' ? <OrbView /> : <AppShell />}
      </I18nProvider>
    </ServicesProvider>
  );
}
