import { useMemo } from 'react';
import { ScreenHeader } from '../../components/shell/Shell';
import { EmptyState } from '../../components/ui';
import { JobCard } from '../../components/work/JobCard';
import { useI18n } from '../../i18n';
import { useAppState } from '../../state/context';

/** The "Video creation" area. */
export function VideoScreen() {
  const { t } = useI18n();
  const jobs = useAppState((s) => s.jobs);
  const mine = useMemo(() => jobs.filter((j) => j.area === 'video'), [jobs]);
  return (
    <div className="area" data-testid="video-screen">
      <ScreenHeader title={t('video.title')} subtitle={t('video.subtitle')} icon="film" />
      <div className="job-list">{mine.length ? mine.map((job) => <JobCard key={job.id} job={job} />) : <EmptyState icon="film" title={t('job.none')} />}</div>
    </div>
  );
}
