import { useMemo, useState } from 'react';
import { work, type JobArea } from '../../api/work';
import { ScreenHeader } from '../../components/shell/Shell';
import { ConfirmButton, EmptyState, Segmented } from '../../components/ui';
import { JobCard } from '../../components/work/JobCard';
import { useI18n, type MessageKey } from '../../i18n';
import { useApi, useAppState, useRunner, useServices, useToast } from '../../state/context';
import { SpeedPanel } from './SpeedPanel';

type Filter = 'all' | JobArea;
const FILTERS: Filter[] = ['all', 'code', 'images', 'video', 'apps'];

/** The work history: every action the code, image, video and app screens ran, plus the speed numbers. */
export function JobsScreen() {
  const { t } = useI18n();
  const api = useApi();
  const toast = useToast();
  const { boot } = useServices();
  const jobs = useAppState((s) => s.jobs);
  const [filter, setFilter] = useState<Filter>('all');
  const { run } = useRunner(t('job.actionFailed'));
  const shown = useMemo(() => (filter === 'all' ? jobs : jobs.filter((j) => j.area === filter)), [jobs, filter]);
  return (
    <div className="area" data-testid="jobs-screen">
      <ScreenHeader
        title={t('jobs.title')}
        subtitle={t('jobs.subtitle')}
        icon="history"
        actions={
          jobs.some((j) => j.status !== 'running') && !boot.mock ? (
            <ConfirmButton
              confirmLabel={t('jobs.clearConfirm')}
              onConfirm={() =>
                void run('clear', () => work.clear(api)).then((ok) => {
                  if (ok) toast('success', t('jobs.cleared'));
                })
              }
              testId="jobs-clear"
            >
              {t('jobs.clear')}
            </ConfirmButton>
          ) : null
        }
      >
        <Segmented value={filter} options={FILTERS.map((f) => ({ value: f, label: t(`jobs.filter.${f}` as MessageKey) }))} onChange={setFilter} label={t('jobs.filter')} testId="jobs-filter" fit />
      </ScreenHeader>
      <div className="area__cols">
        <div className="job-list" data-testid="jobs-list">
          {shown.length ? shown.map((job) => <JobCard key={job.id} job={job} />) : <EmptyState icon="history" title={t('jobs.empty')} hint={t('jobs.emptyHint')} />}
        </div>
        <SpeedPanel />
      </div>
    </div>
  );
}
