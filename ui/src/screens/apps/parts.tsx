import { useId, type ReactNode } from 'react';
import type { AppsWay } from '../../api/apps';
import { BidiText } from '../../components/BidiText';
import { Icon } from '../../components/Icon';
import { useI18n } from '../../i18n';

/** Why each kind of action cannot run right now (null: it can), worked out once from the status for every panel. */
export interface Gates {
  ways: Record<string, AppsWay | undefined>;
  /** Reading the screen (looks, steps). */
  screen: string | null;
  /** The local model (descriptions, suggestions). */
  model: string | null;
  /** Opening apps. */
  launch: string | null;
  /** Something else holds the screen or stops everything: computer control running, an action here, the kill switch. */
  busy: string | null;
  computerOn: boolean;
}

/**
 * A button that says why it is disabled, in words next to it (not only in a tooltip), linked for screen readers.
 * `why` null: enabled, and `hint` (if any) is shown instead.
 */
export function GatedButton({ why, hint, pending, onClick, className, icon, children, testId }: { why: string | null; hint?: ReactNode; pending?: boolean; onClick: () => void; className?: string; icon?: string; children: ReactNode; testId: string }) {
  const id = useId();
  return (
    <div className="apps-gated">
      <button type="button" className={className ?? 'btn btn--primary'} disabled={Boolean(why) || pending} aria-describedby={why || hint ? id : undefined} onClick={onClick} data-testid={testId}>
        {icon ? <Icon name={icon} size={14} /> : null}
        {children}
      </button>
      {why ? (
        <span className="apps-why" id={id} data-testid={`${testId}-why`}>
          <Icon name="info" size={12} />
          <BidiText text={why} />
        </span>
      ) : hint ? (
        <span className="hint" id={id}>
          {hint}
        </span>
      ) : null}
    </div>
  );
}

/** Turning computer control on (the setting `computer.enabled`): every look and step still asks first. */
export function TurnOn({ onClick, pending }: { onClick: () => void; pending: boolean }) {
  const { t } = useI18n();
  return (
    <div className="apps-turnon">
      <button type="button" className="btn btn--sm btn--primary" disabled={pending} onClick={onClick} data-testid="apps-turn-on">
        <Icon name="power" size={13} />
        {t('apps.turnOn')}
      </button>
      <span className="hint">{t('apps.turnOnHint')}</span>
    </div>
  );
}

export const WAY_ICON: Record<string, string> = { launch: 'play', windows: 'window', uia: 'pointer', vision: 'eye', connectors: 'link' };
export const STOP_ICON: Record<string, string> = { stop_button: 'stop', esc_twice: 'key', kill_switch: 'octagon' };
