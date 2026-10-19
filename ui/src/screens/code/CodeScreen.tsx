import { useMemo } from 'react';
import { ScreenHeader } from '../../components/shell/Shell';
import { EmptyState } from '../../components/ui';
import { JobCard } from '../../components/work/JobCard';
import { useI18n } from '../../i18n';
import { useAppState } from '../../state/context';

/** The "Code & development" area. */
export function CodeScreen() {
  const { t } = useI18n();
  const jobs = useAppState((s) => s.jobs);
  const mine = useMemo(() => jobs.filter((j) => j.area === 'code'), [jobs]);
  return (
    <div className="area" data-testid="code-screen">
      <ScreenHeader title={t('code.title')} subtitle={t('code.subtitle')} icon="code" />
      <div className="job-list">{mine.length ? mine.map((job) => <JobCard key={job.id} job={job} />) : <EmptyState icon="code" title={t('job.none')} />}</div>
    </div>
  );
}
