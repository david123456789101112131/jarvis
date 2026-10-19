import { useEffect, type ReactNode } from 'react';
import type { Lang } from '../../api/types';
import { useI18n, type MessageKey } from '../../i18n';
import { formatClock } from '../../lib/format';
import { lastHubTab, RAIL, railOf, type RailId, type ScreenId } from '../../lib/router';
import { useApi, useAppState, useNow, useRunner, useServices } from '../../state/context';
import { Icon } from '../Icon';
import { VOICE_TONE } from '../tones';
import { Chip, StatusChip } from '../ui';

const NAV_ICONS: Record<RailId, string> = {
  command: 'core',
  code: 'code',
  images: 'image',
  video: 'film',
  apps: 'grid',
  hub: 'history',
};

/** Where a rail item leads: the hub opens the tab the user was on last. */
function railTarget(item: RailId): ScreenId {
  return item === 'hub' ? lastHubTab() : item;
}

/** The JARVIS mark: a small arc reactor drawn in SVG. */
export function Logo({ size = 34 }: { size?: number }) {
  return (
    <svg className="logo" width={size} height={size} viewBox="0 0 40 40" aria-hidden="true">
      <circle cx="20" cy="20" r="17.5" className="logo__outer" />
      <circle cx="20" cy="20" r="12.5" className="logo__mid" />
      <path d="M20 4.5v5M20 30.5v5M4.5 20h5M30.5 20h5" className="logo__ticks" />
      <circle cx="20" cy="20" r="6" className="logo__core" />
    </svg>
  );
}

export function Rail({ current, onNavigate, approvals }: { current: ScreenId; onNavigate: (screen: ScreenId) => void; approvals: number }) {
  const { t } = useI18n();
  const { boot } = useServices();
  const version = useAppState((s) => s.version || s.serverVersion || '');
  const active = railOf(current);
  return (
    <nav className="rail" aria-label={t('nav.main')} data-testid="rail">
      <a className="rail__logo" href="#/" onClick={() => onNavigate('command')} aria-label="JARVIS">
        <Logo />
      </a>
      <ul className="rail__nav">
        {RAIL.map((item, index) => {
          const target = railTarget(item);
          return (
            <li key={item}>
              <a
                href={target === 'command' ? '#/' : `#/${target}`}
                className="rail__item"
                aria-current={active === item ? 'page' : undefined}
                data-testid={`nav-${item}`}
                title={`${t(`nav.${item}` as MessageKey)} (Alt+${index + 1})`}
                onClick={(e) => {
                  if (item !== 'hub') return;
                  e.preventDefault();
                  onNavigate(railTarget('hub'));
                }}
              >
                <Icon name={NAV_ICONS[item]} size={20} />
                <span className="rail__label">{t(`nav.short.${item}` as MessageKey)}</span>
                {item === 'hub' && approvals > 0 ? <span className="rail__badge">{approvals}</span> : null}
              </a>
            </li>
          );
        })}
      </ul>
      <div className="rail__foot">
        {boot.mock ? <span className="rail__sim" title={t('app.simulationHint')}>SIM</span> : null}
        <span className="rail__version num">{version ? `v${version.replace(/^v/, '').split('-')[0]}` : ''}</span>
      </div>
    </nav>
  );
}

function ConnectionIndicator() {
  const { t } = useI18n();
  const { boot } = useServices();
  const conn = useAppState((s) => s.connection);
  const status = boot.mock && conn.status === 'open' ? 'mock' : conn.status;
  const tone = status === 'open' || status === 'mock' ? 'ok' : status === 'closed' ? 'danger' : 'warn';
  return (
    <span className="conn" data-status={status} data-tone={tone} data-testid="connection-indicator" role="status" title={conn.lastError ?? undefined}>
      <span className="conn__dot" aria-hidden="true" />
      <span className="conn__label">{t(`conn.${status}` as MessageKey)}</span>
    </span>
  );
}

function Clock() {
  const now = useNow(1000);
  const { lang } = useI18n();
  const date = new Date(now);
  const day = new Intl.DateTimeFormat(lang === 'he' ? 'he-IL' : 'en-GB', { weekday: 'short', day: 'numeric', month: 'short' }).format(date);
  return (
    <div className="clock" data-testid="clock">
      <span className="clock__time num">{formatClock(date)}</span>
      <span className="clock__date">{day}</span>
    </div>
  );
}

function LangToggle() {
  const { lang, setLang, t } = useI18n();
  const options: Lang[] = ['he', 'en'];
  return (
    <div className="lang" role="radiogroup" aria-label={t('lang.toggle')} data-testid="lang-toggle">
      {options.map((value) => (
        <button key={value} type="button" role="radio" aria-checked={lang === value} className="lang__item" onClick={() => setLang(value)} data-testid={`lang-${value}`} lang={value}>
          {value === 'he' ? 'עב' : 'EN'}
        </button>
      ))}
    </div>
  );
}

function KillAlert() {
  const { t } = useI18n();
  const api = useApi();
  const engaged = useAppState((s) => s.killSwitch.global);
  const { run, isPending } = useRunner(t('common.requestFailed'));
  if (!engaged) return null;
  return (
    <div className="topbar__alert topbar__alert--danger" role="alert" data-testid="kill-banner">
      <Icon name="octagon" size={16} />
      <span className="truncate">
        <strong>{t('kill.engaged')}</strong>
        <span className="topbar__alert-sub"> · {t('kill.engagedHint')}</span>
      </span>
      <button type="button" className="btn btn--sm btn--danger" disabled={isPending('release')} onClick={() => void run('release', () => api.killSwitch(false))} data-testid="kill-release-top">
        {t('kill.release')}
      </button>
    </div>
  );
}

function ConnectionAlert() {
  const { t } = useI18n();
  const { transport } = useServices();
  const conn = useAppState((s) => s.connection);
  const hydrated = useAppState((s) => s.hydrated);
  const now = useNow(1000, conn.status === 'reconnecting');
  if (!hydrated || conn.status === 'open' || conn.status === 'connecting' || conn.status === 'idle') return null;
  if (conn.fatal === 'forbidden') {
    return (
      <div className="topbar__alert topbar__alert--danger" role="alert" data-testid="session-banner">
        <Icon name="lock" size={16} />
        <span className="truncate" title={t('app.sessionEndedHint')}>
          <strong>{t('app.sessionEnded')}</strong>
          <span className="topbar__alert-sub"> · {t('app.sessionEndedHint')}</span>
        </span>
      </div>
    );
  }
  const seconds = conn.nextRetryAt ? Math.max(0, Math.ceil((conn.nextRetryAt - now) / 1000)) : null;
  return (
    <div className="topbar__alert topbar__alert--warn" role="status" data-testid="connection-banner">
      <Icon name="network" size={16} />
      <span className="truncate">
        <strong>{t('conn.lost')}</strong>
        {seconds !== null ? <span className="topbar__alert-sub"> · {t('app.retryIn', { s: seconds })}</span> : null}
      </span>
      <button type="button" className="btn btn--sm" onClick={() => transport.reconnectNow()}>
        {t('app.retryNow')}
      </button>
    </div>
  );
}

function ApprovalsButton({ count, onOpen }: { count: number; onOpen: () => void }) {
  const { t } = useI18n();
  if (count <= 0) return null;
  return (
    <button type="button" className="approvals-btn" onClick={onOpen} data-testid="approvals-button" title={t('confirm.badge', { n: count })}>
      <Icon name="shield" size={16} />
      <span className="approvals-btn__label">{t('confirm.badge', { n: count })}</span>
      <span className="approvals-btn__count num">{count}</span>
    </button>
  );
}

export function TopBar({ approvals, onOpenApprovals }: { approvals: number; onOpenApprovals: () => void }) {
  const { t } = useI18n();
  const { boot } = useServices();
  const mode = useAppState((s) => s.mode);
  const voice = useAppState((s) => s.voice);
  const name = useAppState((s) => s.assistantName);
  const voiceKey = voice.enabled ? voice.state : 'disabled';
  return (
    <header className="topbar" data-testid="topbar">
      <div className="topbar__brand">
        <span className="topbar__name ltr">{(name || 'JARVIS').toUpperCase()}</span>
        <span className="topbar__sep" aria-hidden="true" />
        <span className="topbar__tagline">{t('app.tagline')}</span>
      </div>
      <div className="topbar__chips">
        <Chip tone="accent" className="chip--plain" testId="mode-chip" title={t(`mode.desc.${mode}` as MessageKey)}>
          <Icon name="layers" size={12} />
          {t('topbar.mode', { mode: t(`mode.${mode}` as MessageKey) })}
        </Chip>
        <StatusChip status={voiceKey} tone={VOICE_TONE[voiceKey]} label={t(`voice.state.${voiceKey}` as MessageKey)} testId="voice-chip" />
      </div>
      <div className="topbar__center">
        <KillAlert />
        <ConnectionAlert />
      </div>
      <div className="topbar__end">
        <ApprovalsButton count={approvals} onOpen={onOpenApprovals} />
        {boot.mock ? (
          <Chip tone="warn" className="topbar__sim" title={t('app.simulationHint')} testId="mock-badge">
            {t('app.simulation')}
          </Chip>
        ) : null}
        <ConnectionIndicator />
        <Clock />
        <LangToggle />
      </div>
    </header>
  );
}

/** Alt+1…6 switches between the rail's areas. */
export function useScreenShortcuts(onNavigate: (screen: ScreenId) => void) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!e.altKey || e.ctrlKey || e.metaKey) return;
      const index = Number(e.key) - 1;
      if (Number.isInteger(index) && index >= 0 && index < RAIL.length) {
        e.preventDefault();
        onNavigate(railTarget(RAIL[index]));
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onNavigate]);
}

export function ScreenHeader({ title, subtitle, icon, actions, children }: { title: string; subtitle?: string; icon?: string; actions?: ReactNode; children?: ReactNode }) {
  return (
    <div className="screen-head">
      <div className="screen-head__titles">
        <h1 className="screen-head__title">
          {icon ? (
            <span className="screen-head__icon">
              <Icon name={icon} size={18} />
            </span>
          ) : null}
          {title}
        </h1>
        {subtitle ? <p className="screen-head__sub">{subtitle}</p> : null}
      </div>
      {children}
      {actions ? <div className="screen-head__actions">{actions}</div> : null}
    </div>
  );
}
