import { useRef } from 'react';
import { useI18n } from '../i18n';
import { useApi, useAppState, useRunner } from '../state/context';
import { CoreStage } from '../components/core/CoreStage';

/** How far (px) the pointer may travel between press and release for it to count as a click, not a drag. */
export const CLICK_SLOP = 5;

/**
 * The always-on-top mini orb window (`?view=orb`): a transparent page with only the orb and its voice
 * overlays, no buttons around it. A click on the orb opens the command center; the window is moved by
 * dragging anywhere on it (pywebview easy_drag), and a drag is not taken for a click. No toasts here: while
 * the main window is hidden the core shows notifications (approvals too) as Windows toasts.
 */
export function OrbView() {
  const { t } = useI18n();
  const api = useApi();
  const hydrated = useAppState((s) => s.hydrated);
  const conn = useAppState((s) => s.connection);
  const pending = useAppState((s) => s.confirmations.some((c) => c.status === 'pending'));
  const { run } = useRunner(t('common.requestFailed'));
  const pressedAt = useRef<{ x: number; y: number } | null>(null);
  const offline = hydrated && conn.status !== 'open';

  const openApp = (e: React.MouseEvent) => {
    const from = pressedAt.current;
    pressedAt.current = null;
    if (from && Math.hypot(e.screenX - from.x, e.screenY - from.y) > CLICK_SLOP) return;      // the orb was dragged
    void run('show', () => api.windowAction('show'));
  };

  return (
    <div
      className="orb-view"
      data-testid="orb-view"
      data-offline={offline ? 'true' : undefined}
      onMouseDown={(e) => {
        pressedAt.current = { x: e.screenX, y: e.screenY };
      }}
    >
      {hydrated ? (
        <CoreStage variant="mini" onOrbClick={openApp} orbLabel={pending ? t('orbView.approvals') : t('orbView.openApp')} />
      ) : (
        <div className="orb-view__boot" role="status">
          <span className="spinner" />
        </div>
      )}
    </div>
  );
}
