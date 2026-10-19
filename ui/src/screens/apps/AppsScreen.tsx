import { useMemo } from 'react';
import { ScreenHeader } from '../../components/shell/Shell';
import { EmptyState } from '../../components/ui';
import { JobCard } from '../../components/work/JobCard';
import { useI18n } from '../../i18n';
import { useAppState } from '../../state/context';

/** The "Apps & tools" area. */
export function AppsScreen() {
  const { t } = useI18n();
  const jobs = useAppState((s) => s.jobs);
  const mine = useMemo(() => jobs.filter((j) => j.area === 'apps'), [jobs]);
  return (
    <div className="area" data-testid="apps-screen">
      <ScreenHeader title={t('apps.title')} subtitle={t('apps.subtitle')} icon="grid" />
      <div className="job-list">{mine.length ? mine.map((job) => <JobCard key={job.id} job={job} />) : <EmptyState icon="grid" title={t('job.none')} />}</div>
    </div>
  );
}
