import type { AppsStatus } from '../../api/apps';
import { BidiText } from '../../components/BidiText';
import { Icon } from '../../components/Icon';
import { Chip, Panel } from '../../components/ui';
import { useI18n } from '../../i18n';
import { STOP_ICON, WAY_ICON } from './parts';

/** Which ways of working with apps exist here and, for each one that does not, why and what is needed; how to
 *  stop; and what JARVIS will not do. */
export function WaysPanel({ status }: { status: AppsStatus }) {
  const { t } = useI18n();
  return (
    <Panel title={t('apps.ways.title')} icon="sliders" testId="apps-ways" bodyClassName="apps-ways">
      <ul className="apps-ways__list">
        {status.ways.map((way) => (
          <li key={way.key} className="apps-way" data-available={way.available} data-testid={`apps-way-${way.key}`}>
            <span className="apps-way__icon">
              <Icon name={WAY_ICON[way.key] ?? 'app'} size={14} />
            </span>
            <div className="apps-way__body">
              <div className="apps-way__head">
                <strong>
                  <BidiText text={way.label} />
                </strong>
                <Chip tone={way.available ? 'ok' : 'warn'} dot testId={`apps-way-${way.key}-state`}>
                  {t(way.available ? 'apps.ways.available' : 'apps.ways.unavailable')}
                </Chip>
              </div>
              <span className="apps-way__detail">
                <BidiText text={way.detail} />
              </span>
              {!way.available && way.reason ? (
                <span className="apps-way__why" data-testid={`apps-way-${way.key}-why`}>
                  <BidiText text={way.reason} />
                </span>
              ) : null}
              {!way.available && way.needs.length ? (
                <ul className="apps-way__needs" aria-label={t('unavailable.needs')}>
                  {way.needs.map((line, i) => (
                    <li key={i}>
                      <BidiText text={line} />
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          </li>
        ))}
      </ul>

      <h3 className="apps-sub">{t('apps.stop.title')}</h3>
      <ul className="apps-stop" data-testid="apps-stop">
        {status.stop.map((way) => (
          <li key={way.key}>
            <Icon name={STOP_ICON[way.key] ?? 'stop'} size={13} />
            <strong>
              <BidiText text={way.label} />
            </strong>
            <span>
              <BidiText text={way.detail} />
            </span>
          </li>
        ))}
      </ul>

      <h3 className="apps-sub">{t('apps.limits.title')}</h3>
      <ul className="apps-limits" data-testid="apps-limits">
        {status.limits.map((line, i) => (
          <li key={i}>
            <BidiText text={line} />
          </li>
        ))}
      </ul>
    </Panel>
  );
}
