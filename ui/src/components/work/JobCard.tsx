import type { ReactNode } from 'react';
import { jobSeconds, work, type Job, type JobStatus } from '../../api/work';
import { useI18n, type MessageKey } from '../../i18n';
import { formatDuration, formatMs, formatRelative } from '../../lib/format';
import { useApi, useNow, useRunner } from '../../state/context';
import { BidiText } from '../BidiText';
import { Icon } from '../Icon';
import type { Tone } from '../tones';
import { Chip, ProgressBar, StatusChip } from '../ui';

export const JOB_TONE: Record<JobStatus, Tone> = { running: 'work', done: 'ok', failed: 'danger', cancelled: 'muted' };

const STEP_ICON: Record<string, string> = { pending: 'clock', running: 'bolt', done: 'check', failed: 'x', skipped: 'minus' };

/** "Real action" or "Demonstration": every work screen says which one it is doing. */
export function RealBadge({ real, testId }: { real: boolean; testId?: string }) {
  const { t } = useI18n();
  return (
    <Chip tone={real ? 'accent' : 'warn'} title={t(real ? 'job.realHint' : 'job.demoHint')} testId={testId ?? 'job-real'}>
      <Icon name={real ? 'bolt' : 'eye'} size={12} />
      {t(real ? 'job.real' : 'job.demo')}
    </Chip>
  );
}

/** What leaves the computer (and where to), or that nothing does. */
export function SendsLine({ sends, cost }: { sends: string[]; cost?: string | null }) {
  const { t } = useI18n();
  return (
    <div className="job__sends" data-testid="job-sends">
      <Icon name={sends.length ? 'send' : 'lock'} size={13} />
      {sends.length ? (
        <span>
          <strong>{t('job.sends')}: </strong>
          <BidiText text={sends.join(' · ')} />
        </span>
      ) : (
        <span>{t('job.sendsNothing')}</span>
      )}
      {cost ? (
        <span className="job__cost">
          {t('job.cost')}: <span className="num">{cost}</span>
        </span>
      ) : null}
    </div>
  );
}

/**
 * One action of a work screen, live: what JARVIS understood, what it is doing, whether it is real, what leaves the
 * computer, its steps with their times, and its result or a clear error, with Stop and Try again where they apply.
 * `children` renders the area's own result (a diff, an image, a screenshot) under the card.
 */
export function JobCard({ job, children, compact, actions }: { job: Job; children?: ReactNode; compact?: boolean; actions?: ReactNode }) {
  const { t, lang } = useI18n();
  const api = useApi();
  const running = job.status === 'running';
  const now = useNow(1000, running);
  const { run, isPending } = useRunner(t('job.actionFailed'));
  const stopping = running && isPending('cancel');
  return (
    <article className={`job ${compact ? 'job--compact' : ''}`} data-testid="job-card" data-status={job.status} data-kind={job.kind} aria-busy={running || undefined}>
      <header className="job__head">
        <StatusChip status={job.status} tone={JOB_TONE[job.status]} label={t(`job.status.${job.status}` as MessageKey)} testId="job-status" />
        <h3 className="job__title">
          <span className="job__label">{t('job.understood')}: </span>
          <BidiText text={job.title} />
        </h3>
        <RealBadge real={job.real} />
        <span className="job__time num" title={job.created_at}>
          {running ? formatDuration(jobSeconds(job, now), lang) : t('job.took', { s: formatMs(jobSeconds(job) * 1000, lang) })}
        </span>
      </header>

      {!compact && job.plan.length ? (
        <div className="job__plan" data-testid="job-plan">
          <span className="job__label">{t('job.plan')}</span>
          <ol>
            {job.plan.map((line, i) => (
              <li key={i}>
                <BidiText text={line} />
              </li>
            ))}
          </ol>
        </div>
      ) : null}

      {!compact ? <SendsLine sends={job.sends} cost={job.cost} /> : null}

      {running ? (
        <div className="job__progress">
          <ProgressBar value={job.progress ?? 0} indeterminate={job.progress === null} tone="work" label={job.message || t('job.status.running')} />
          {job.message ? <span className="job__message" data-testid="job-message"><BidiText text={job.message} /></span> : null}
        </div>
      ) : null}

      {job.steps.length && !compact ? (
        <ol className="job__steps" aria-label={t('job.steps')} data-testid="job-steps">
          {job.steps.map((step) => (
            <li key={step.key} className="job__step" data-status={step.status} data-testid={`job-step-${step.key}`}>
              <Icon name={STEP_ICON[step.status] ?? 'clock'} size={12} />
              <BidiText text={step.label} />
              {step.detail ? <span className="job__step-detail"><BidiText text={step.detail} /></span> : null}
              <span className="job__step-status">{t(`job.step.${step.status}` as MessageKey)}</span>
              {step.ms !== null && step.ms !== undefined ? <span className="num job__step-ms">{formatMs(step.ms, lang)}</span> : null}
            </li>
          ))}
        </ol>
      ) : null}

      {job.status === 'failed' && job.error ? (
        <div className="job__error" role="alert" data-testid="job-error">
          <Icon name="alert" size={14} />
          <span>
            <strong>{t('job.error')}: </strong>
            <BidiText text={job.error} />
          </span>
        </div>
      ) : null}
      {job.status === 'cancelled' && job.message ? (
        <div className="job__note" data-testid="job-note">
          <Icon name="octagon" size={14} />
          <BidiText text={job.message} />
        </div>
      ) : null}

      {children ? <div className="job__result">{children}</div> : null}

      <footer className="job__foot">
        {job.retry_of ? <span className="job__retry-of">{t('job.retryOf')}</span> : null}
        <span className="job__when">{formatRelative(job.created_at, lang, now)}</span>
        <span className="job__actions">
          {actions}
          {running ? (
            <button type="button" className="btn btn--sm btn--danger" disabled={stopping} onClick={() => void run('cancel', () => work.cancel(api, job.id))} data-testid="job-stop">
              <Icon name="stop" size={13} />
              {stopping ? t('job.stopping') : t('job.stop')}
            </button>
          ) : null}
          {job.can_retry ? (
            <button type="button" className="btn btn--sm" disabled={isPending('retry')} onClick={() => void run('retry', () => work.retry(api, job.id))} data-testid="job-retry">
              <Icon name="replay" size={13} />
              {t('job.retry')}
            </button>
          ) : null}
        </span>
      </footer>
    </article>
  );
}

/** A short explanation for a screen whose service is not connected: what is missing and how to turn it on. */
export function Unavailable({ title, reason, needs, testId, children }: { title?: string; reason: string; needs?: string[]; testId?: string; children?: ReactNode }) {
  const { t } = useI18n();
  return (
    <div className="unavailable" role="status" data-testid={testId ?? 'unavailable'}>
      <Icon name="info" size={18} />
      <div className="unavailable__body">
        <strong>{title ?? t('unavailable.title')}</strong>
        <BidiText text={reason} />
        {needs && needs.length ? (
          <div className="unavailable__needs">
            <span className="job__label">{t('unavailable.needs')}</span>
            <ul>
              {needs.map((line, i) => (
                <li key={i}>
                  <BidiText text={line} />
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        {children}
      </div>
    </div>
  );
}
