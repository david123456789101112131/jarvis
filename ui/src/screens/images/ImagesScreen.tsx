import { useMemo } from 'react';
import { ScreenHeader } from '../../components/shell/Shell';
import { EmptyState } from '../../components/ui';
import { JobCard } from '../../components/work/JobCard';
import { useI18n } from '../../i18n';
import { useAppState } from '../../state/context';

/** The "Image creation" area. */
export function ImagesScreen() {
  const { t } = useI18n();
  const jobs = useAppState((s) => s.jobs);
  const mine = useMemo(() => jobs.filter((j) => j.area === 'images'), [jobs]);
  return (
    <div className="area" data-testid="images-screen">
      <ScreenHeader title={t('images.title')} subtitle={t('images.subtitle')} icon="image" />
      <div className="job-list">{mine.length ? mine.map((job) => <JobCard key={job.id} job={job} />) : <EmptyState icon="image" title={t('job.none')} />}</div>
    </div>
  );
}
