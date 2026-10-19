import { useState } from 'react';
import { work, type MetricSummary } from '../../api/work';
import { ConfirmButton, EmptyState, ErrorBox, Loading, Panel } from '../../components/ui';
import { Unavailable } from '../../components/work/JobCard';
import { useI18n } from '../../i18n';
import { formatMs } from '../../lib/format';
import { useApi, useResource, useRunner, useServices, useToast } from '../../state/context';

/** How a measurement's name reads to a person: "dev.inspect.scan" → "dev · inspect · scan". */
function prettyName(name: string): string {
  return name.replace(/^job\./, '').split('.').join(' · ');
}

/** The measured speed of every kind of work on this computer, against a saved "before" when there is one. */
export function SpeedPanel() {
  const { t, lang } = useI18n();
  const api = useApi();
  const toast = useToast();
  const { boot } = useServices();
  const metrics = useResource((a) => (boot.mock ? Promise.resolve(null) : work.metrics(a)), [boot.mock]);
  const { run, isPending } = useRunner(t('job.actionFailed'));
  const [label, setLabel] = useState('before');
  const snapshots = metrics.data?.snapshots ?? {};
  const names = Object.keys(snapshots);
  const [compare, setCompare] = useState<string | null>(null);
  const base = compare ?? (names.includes('before') ? 'before' : (names[0] ?? null));
  const baseline = base ? snapshots[base]?.summary : undefined;
  const rows = Object.entries(metrics.data?.summary ?? {});

  const change = (now: MetricSummary, before: MetricSummary | undefined) => {
    if (!before || !before.median_ms) return null;
    const pct = Math.round(((now.median_ms - before.median_ms) / before.median_ms) * 100);
    return { pct, better: pct < 0 };
  };

  return (
    <Panel
      title={t('speed.title')}
      icon="bolt"
      testId="speed-panel"
      actions={
        rows.length ? (
          <ConfirmButton confirmLabel={t('jobs.clearConfirm')} onConfirm={() => void run('clear', () => work.clearMetrics(api)).then(metrics.reload)} testId="speed-clear">
            {t('speed.clear')}
          </ConfirmButton>
        ) : null
      }
    >
      <p className="field__hint">{t('speed.subtitle')}</p>
      {metrics.loading && !metrics.data ? <Loading label={t('speed.title')} /> : null}
      {boot.mock ? <Unavailable reason={t('unavailable.simulation')} /> : null}
      {metrics.error ? <ErrorBox message={metrics.error} onRetry={metrics.reload} /> : null}
      {metrics.data && !rows.length ? <EmptyState icon="bolt" title={t('speed.empty')} /> : null}
      {rows.length ? (
        <>
          {names.length ? (
            <div className="actions-row">
              <span className="field__label">{t('speed.compare', { label: base ?? '' })}</span>
              <select className="select" value={base ?? ''} onChange={(e) => setCompare(e.target.value || null)} data-testid="speed-compare">
                {names.map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </select>
            </div>
          ) : null}
          <table className="speed-table" data-testid="speed-table">
            <thead>
              <tr>
                <th>{t('speed.name')}</th>
                <th>{t('speed.count')}</th>
                <th>{t('speed.median')}</th>
                <th>{t('speed.p90')}</th>
                <th>{t('speed.last')}</th>
                {baseline ? <th>{base}</th> : null}
              </tr>
            </thead>
            <tbody>
              {rows.map(([name, s]) => {
                const before = baseline?.[name];
                const diff = change(s, before);
                return (
                  <tr key={name} data-testid="speed-row" data-name={name}>
                    <td className="ltr" dir="ltr">
                      {prettyName(name)}
                    </td>
                    <td className="num">{s.count}</td>
                    <td className="num">{formatMs(s.median_ms, lang)}</td>
                    <td className="num">{formatMs(s.p90_ms, lang)}</td>
                    <td className="num">{formatMs(s.last_ms, lang)}</td>
                    {baseline ? (
                      <td className={`num ${diff ? (diff.better ? 'speed-better' : 'speed-worse') : ''}`}>
                        {before ? `${formatMs(before.median_ms, lang)}${diff ? ` (${diff.pct > 0 ? '+' : ''}${diff.pct}%)` : ''}` : '—'}
                      </td>
                    ) : null}
                  </tr>
                );
              })}
            </tbody>
          </table>
          <form
            className="actions-row"
            onSubmit={(e) => {
              e.preventDefault();
              const name = label.trim();
              if (!name) return;
              void run('snapshot', () => work.snapshot(api, name)).then((ok) => {
                if (ok) {
                  toast('success', t('speed.snapshotSaved', { label: name }));
                  metrics.reload();
                }
              });
            }}
          >
            <label className="field__label" htmlFor="speed-label">
              {t('speed.snapshot')}
            </label>
            <input id="speed-label" className="input ltr" dir="ltr" style={{ width: 140 }} value={label} maxLength={40} onChange={(e) => setLabel(e.target.value)} />
            <button type="submit" className="btn btn--sm" disabled={isPending('snapshot') || !label.trim()} data-testid="speed-snapshot">
              {t('speed.save')}
            </button>
          </form>
        </>
      ) : null}
    </Panel>
  );
}
