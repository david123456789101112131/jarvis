import { useI18n } from '../../i18n';
import type { ScreenId } from '../../lib/router';
import { ActivityPanel } from './ActivityPanel';
import { AgentsPanel } from './AgentsPanel';
import { ChatPanel } from './ChatPanel';
import { CorePanel } from './CorePanel';
import { FilesPanel } from './FilesPanel';
import { SystemPanel } from './SystemPanel';
import { TaskPanel } from './TaskPanel';

/**
 * Main screen: every panel on one canvas, no page scroll at the default 1480×920 window.
 * Three independent columns so each can size its panels to their content:
 * agents · system · activity | core · chat | tasks · files & system.
 */
export function CommandCenter({ onNavigate }: { onNavigate: (screen: ScreenId, param?: string | null) => void }) {
  const { t } = useI18n();
  return (
    <div className="cc" data-testid="command-center">
      <h1 className="sr-only">{t('nav.command')}</h1>
      <div className="cc__col cc__col--start">
        <AgentsPanel className="cc__agents" onNavigate={onNavigate} />
        <SystemPanel className="cc__system" />
        <ActivityPanel className="cc__activity" />
      </div>
      <div className="cc__col cc__col--center">
        <CorePanel className="cc__core" />
        <ChatPanel className="cc__chat" onNavigate={onNavigate} />
      </div>
      <div className="cc__col cc__col--end">
        <TaskPanel className="cc__tasks" onNavigate={onNavigate} />
        <FilesPanel className="cc__files" />
      </div>
    </div>
  );
}
