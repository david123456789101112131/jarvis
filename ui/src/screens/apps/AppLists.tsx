import { useId, useState } from 'react';
import type { AppsListing, OpenWindow } from '../../api/apps';
import { BidiText } from '../../components/BidiText';
import { Icon } from '../../components/Icon';
import { Chip, EmptyState, ErrorBox, Loading, Panel } from '../../components/ui';
import { useI18n } from '../../i18n';
import type { Resource } from '../../state/context';
import type { Gates } from './parts';

const SHOWN = 40;

/** The installed apps, searchable, each with Open (or why it cannot be opened here). */
export function InstalledPanel({ listing, query, onQuery, gates, onOpen, opening, onRescan, rescanning }: { listing: Resource<AppsListing>; query: string; onQuery: (q: string) => void; gates: Gates; onOpen: (name: string) => void; opening: (name: string) => boolean; onRescan: () => void; rescanning: boolean }) {
  const { t } = useI18n();
  const whyId = useId();
  const [all, setAll] = useState(false);
  const data = listing.data;
  const rows = data?.installed ?? [];
  const shown = all ? rows : rows.slice(0, SHOWN);
  return (
    <Panel
      title={t('apps.installed.title')}
      icon="grid"
      testId="apps-installed"
      meta={data ? <span className="num" data-testid="apps-installed-count">{t('apps.installed.count', { matched: data.matched, total: data.total })}</span> : null}
      actions={
        <button type="button" className="btn btn--sm btn--ghost" disabled={rescanning} onClick={onRescan} title={t('apps.installed.rescan')} data-testid="apps-rescan">
          <Icon name="restart" size={13} />
          {t('apps.installed.rescan')}
        </button>
      }
    >
      <div className="apps-lists">
        <label className="search">
          <Icon name="search" size={14} />
          <input
            className="input"
            type="search"
            value={query}
            maxLength={100}
            placeholder={t('apps.installed.searchPlaceholder')}
            aria-label={t('apps.installed.search')}
            onChange={(e) => onQuery(e.target.value)}
            data-testid="apps-search"
          />
        </label>
        {gates.launch ? (
          <p className="apps-why" id={whyId} data-testid="apps-open-why">
            <Icon name="info" size={12} />
            <BidiText text={t('apps.openWhyNot', { reason: gates.launch })} />
          </p>
        ) : null}
        {data?.errors.map((error, i) => (
          <p key={i} className="apps-why">
            <Icon name="alert" size={12} />
            <BidiText text={error} />
          </p>
        ))}
        {listing.error && !data ? <ErrorBox message={listing.error} onRetry={listing.reload} retryLabel={t('job.retry')} /> : null}
        {!data && listing.loading ? <Loading label={t('apps.installed.title')} /> : null}
        {data && !rows.length ? <EmptyState icon="search" title={query.trim() ? t('apps.installed.none', { q: query.trim() }) : t('apps.installed.empty')} testId="apps-installed-empty" /> : null}
        {rows.length ? (
          <ul className="apps-list" data-testid="apps-installed-list">
            {shown.map((app) => {
              const why = app.can.includes('open') ? null : app.why_not ?? gates.launch;
              return (
                <li key={`${app.name}\u0000${app.source}`} className="apps-row" data-testid="apps-installed-row">
                  <span className="apps-row__main">
                    <span className="apps-row__name">
                      <BidiText text={app.name} />
                    </span>
                  </span>
                  <button
                    type="button"
                    className="btn btn--sm"
                    disabled={Boolean(why) || opening(app.name)}
                    title={why ?? undefined}
                    aria-describedby={why && gates.launch ? whyId : undefined}
                    onClick={() => onOpen(app.name)}
                    data-testid="apps-open"
                  >
                    <Icon name="play" size={12} />
                    {t('apps.open')}
                  </button>
                </li>
              );
            })}
          </ul>
        ) : null}
        {rows.length > shown.length ? (
          <div className="apps-more">
            <span className="hint">{t('apps.installed.shown', { shown: shown.length, n: rows.length })}</span>
            <button type="button" className="btn btn--sm btn--ghost" onClick={() => setAll(true)} data-testid="apps-installed-more">
              {t('apps.installed.more', { n: rows.length })}
            </button>
          </div>
        ) : null}
      </div>
    </Panel>
  );
}

/** The open windows (JARVIS's own left out): look at one, or choose it for the task. */
export function WindowsPanel({ listing, target, gates, onChoose, onLook }: { listing: Resource<AppsListing>; target: OpenWindow | null; gates: Gates; onChoose: (w: OpenWindow) => void; onLook: (w: OpenWindow) => void }) {
  const { t } = useI18n();
  const windows = listing.data?.windows ?? [];
  const lookWhy = gates.screen ?? gates.busy;
  return (
    <Panel
      title={t('apps.windows.title')}
      icon="window"
      testId="apps-windows"
      meta={listing.data ? <span className="num">{t('apps.windows.count', { n: windows.length })}</span> : null}
    >
      <div className="apps-lists">
        {!listing.data && listing.loading ? <Loading label={t('apps.windows.title')} rows={2} /> : null}
        {listing.data && !windows.length ? <EmptyState icon="window" title={t('apps.windows.empty')} testId="apps-windows-empty" /> : null}
        {windows.length && lookWhy && windows.some((w) => w.can.includes('look')) ? (
          <p className="apps-why">
            <Icon name="info" size={12} />
            <BidiText text={lookWhy} />
          </p>
        ) : null}
        {windows.length ? (
          <ul className="apps-list" data-testid="apps-windows-list">
            {windows.map((w) => {
              const chosen = target !== null && target.pid === w.pid && target.window === w.window;
              return (
                <li key={`${w.pid}\u0000${w.window}`} className="apps-row apps-row--window" data-chosen={chosen || undefined} data-testid="apps-window-row">
                  <span className="apps-row__main">
                    <span className="apps-row__name">
                      <BidiText text={w.title || w.program} />
                    </span>
                    <span className="apps-row__sub">
                      <bdi dir="ltr">{w.program}</bdi>
                      {w.windows > 1 ? <span className="num"> · {t('apps.windows.count', { n: w.windows })}</span> : null}
                    </span>
                    {w.note ? (
                      <span className="apps-row__note" data-testid="apps-window-note">
                        <Icon name="lock" size={11} />
                        <BidiText text={w.note} />
                      </span>
                    ) : null}
                  </span>
                  {w.can.includes('look') ? (
                    <span className="apps-row__actions">
                      {chosen ? (
                        <Chip tone="accent" testId="apps-window-chosen">
                          {t('apps.windows.chosen')}
                        </Chip>
                      ) : (
                        <button type="button" className="btn btn--sm btn--ghost" onClick={() => onChoose(w)} data-testid="apps-window-choose">
                          {t('apps.windows.choose')}
                        </button>
                      )}
                      <button type="button" className="btn btn--sm" disabled={Boolean(lookWhy)} title={lookWhy ?? undefined} onClick={() => onLook(w)} data-testid="apps-window-look">
                        <Icon name="eye" size={12} />
                        {t('apps.windows.look')}
                      </button>
                    </span>
                  ) : null}
                </li>
              );
            })}
          </ul>
        ) : null}
      </div>
    </Panel>
  );
}
