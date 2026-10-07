import { useState } from 'react';
import { useI18n } from '../i18n';
import { useApi, useAppState, useRunner } from '../state/context';
import { CoreStage } from '../components/core/CoreStage';
import { MicButton } from '../components/core/MicButton';
import { Icon } from '../components/Icon';

/**
 * The always-on-top mini orb window (`?view=orb`): a transparent page with the orb and its voice
 * overlays. Click the orb for quick tools; double-click (or the expand button) opens the command center.
 * The window itself is moved by dragging anywhere on it (pywebview easy_drag). No toasts here: while
 * the main window is hidden the core shows notifications as Windows toasts.
 */
export function OrbView() {
  const { t } = useI18n();
  const api = useApi();
  const [toolsOpen, setToolsOpen] = useState(false);
  const hydrated = useAppState((s) => s.hydrated);
  const conn = useAppState((s) => s.connection);
  const pending = useAppState((s) => s.confirmations.some((c) => c.status === 'pending'));
  const { run } = useRunner(t('common.requestFailed'));
  const openApp = () => void run('show', () => api.windowAction('show'));
  const offline = hydrated && conn.status !== 'open';

  return (
    <div className="orb-view" data-testid="orb-view" data-offline={offline ? 'true' : undefined} onDoubleClick={openApp}>
      {hydrated ? (
        <CoreStage variant="mini" toolsOpen={toolsOpen} onToggleTools={() => setToolsOpen((open) => !open)} onCloseTools={() => setToolsOpen(false)} reserveBottom={34} />
      ) : (
        <div className="orb-view__boot" role="status">
          <span className="spinner" />
        </div>
      )}
      <div className="orb-view__bar" role="toolbar" aria-label="JARVIS">
        <MicButton size="md" />
        <button type="button" className="orb-view__btn" onClick={() => setToolsOpen((open) => !open)} aria-label={t('orbView.tools')} title={t('orbView.tools')} aria-expanded={toolsOpen} data-testid="orb-tools">
          <Icon name={toolsOpen ? 'x' : 'grid'} size={15} />
        </button>
        <button type="button" className={`orb-view__btn ${pending ? 'orb-view__btn--alert' : ''}`} onClick={openApp} aria-label={pending ? t('orbView.approvals') : t('orbView.openApp')} title={pending ? t('orbView.approvals') : t('orbView.openApp')} data-testid="orb-open">
          <Icon name={pending ? 'shield' : 'external'} size={15} />
        </button>
      </div>
    </div>
  );
}
