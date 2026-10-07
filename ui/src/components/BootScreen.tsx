import { useI18n } from '../i18n';
import { useAppState, useNow, useServices } from '../state/context';
import { Icon } from './Icon';
import { Orb } from './orb/Orb';

/** Shown until the first `state` snapshot arrives (or while the core cannot be reached). */
export function BootScreen() {
  const { t } = useI18n();
  const { transport } = useServices();
  const conn = useAppState((s) => s.connection);
  const theme = useAppState((s) => s.ui.theme_color);
  const failing = conn.status === 'reconnecting' || conn.status === 'closed';
  const expired = conn.fatal === 'forbidden';
  const now = useNow(500, failing);
  const seconds = conn.nextRetryAt ? Math.max(0, Math.ceil((conn.nextRetryAt - now) / 1000)) : null;
  return (
    <div className={`boot ${failing ? 'boot--failing' : ''}`} data-testid="boot-screen" role="status" aria-live="polite">
      <div className="boot__orb">
        <Orb state={failing ? 'error' : 'idle'} theme={theme} size={300} testId="boot-orb" quality="low" />
      </div>
      <p className="boot__name ltr">J.A.R.V.I.S</p>
      {expired ? (
        <div className="boot__status boot__status--error" data-testid="boot-expired">
          <p className="boot__title">
            <Icon name="lock" size={16} /> {t('app.sessionEnded')}
          </p>
          <p className="boot__hint">{t('app.sessionEndedHint')}</p>
        </div>
      ) : failing ? (
        <div className="boot__status boot__status--error" data-testid="boot-error">
          <p className="boot__title">
            <Icon name="alert" size={16} /> {t('app.bootFailed')}
          </p>
          <p className="boot__hint">{t('app.bootFailedHint')}</p>
          {conn.lastError ? <p className="boot__detail mono ltr">{conn.lastError}</p> : null}
          <div className="boot__actions">
            {seconds !== null ? <span className="boot__retry">{t('app.retryIn', { s: seconds })}</span> : null}
            <button type="button" className="btn btn--sm" onClick={() => transport.reconnectNow()}>
              <Icon name="restart" size={14} />
              {t('app.retryNow')}
            </button>
          </div>
        </div>
      ) : (
        <p className="boot__status">
          <span className="spinner spinner--xs" />
          {t('app.booting')}
        </p>
      )}
    </div>
  );
}
