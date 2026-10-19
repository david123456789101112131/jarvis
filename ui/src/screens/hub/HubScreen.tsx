import { useEffect, type ReactNode } from 'react';
import { Icon } from '../../components/Icon';
import { useI18n, type MessageKey } from '../../i18n';
import { HUB_TABS, rememberHubTab, type ScreenId } from '../../lib/router';

const TAB_ICONS: Partial<Record<ScreenId, string>> = {
  tasks: 'tasks',
  jobs: 'history',
  agents: 'agents',
  memory: 'memory',
  security: 'security',
  settings: 'settings',
  privacy: 'lock',
};

/** "History & settings": the task history, the work history, and the settings-like screens, as tabs. */
export function HubScreen({ current, approvals, children }: { current: ScreenId; approvals: number; children: ReactNode }) {
  const { t } = useI18n();
  useEffect(() => rememberHubTab(current), [current]);
  return (
    <div className="hub" data-testid="hub">
      <nav className="hub__tabs" role="tablist" aria-label={t('hub.tabs')}>
        {HUB_TABS.map((tab) => (
          <a
            key={tab}
            href={`#/${tab}`}
            role="tab"
            className="hub__tab"
            aria-selected={tab === current}
            data-testid={`hub-tab-${tab}`}
          >
            <Icon name={TAB_ICONS[tab] ?? 'app'} size={14} />
            {t(`nav.${tab}` as MessageKey)}
            {tab === 'security' && approvals > 0 ? <span className="hub__badge num">{approvals}</span> : null}
          </a>
        ))}
      </nav>
      <div className="hub__body" role="tabpanel">
        {children}
      </div>
    </div>
  );
}
