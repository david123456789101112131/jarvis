import { useState, type FormEvent } from 'react';
import { lookOf, type OpenWindow, type ScreenLook } from '../../api/apps';
import type { Job } from '../../api/work';
import { BidiText } from '../../components/BidiText';
import { Icon } from '../../components/Icon';
import { Chip, Panel, ProgressBar } from '../../components/ui';
import { Unavailable } from '../../components/work/JobCard';
import { useI18n } from '../../i18n';
import { GatedButton, type Gates } from './parts';

/** "What do you see?": one look on the user's click (approved first), what came back, and the correction box. */
export function LookPanel({ gates, target, onClearTarget, job, onLook, pending, corrections, canCorrect, onCorrect, correcting }: { gates: Gates; target: OpenWindow | null; onClearTarget: () => void; job: Job | undefined; onLook: () => void; pending: boolean; corrections: string[]; canCorrect: boolean; onCorrect: (text: string) => Promise<boolean>; correcting: boolean }) {
  const { t } = useI18n();
  const uia = gates.ways.uia;
  const vision = gates.ways.vision;
  const look = lookOf(job);
  return (
    <Panel title={t('apps.look.title')} icon="eye" testId="apps-look" bodyClassName="apps-panel">
      {gates.computerOn && gates.screen && uia ? <Unavailable title={t('apps.screen.title')} reason={gates.screen} needs={uia.needs} testId="apps-screen-unavailable" /> : null}

      <div className="apps-target" data-testid="apps-target">
        <span className="field__label">{t('apps.look.window')}</span>
        {target ? (
          <Chip tone="accent" className="apps-target__chip">
            <Icon name="window" size={12} />
            <BidiText text={target.window} />
            <button type="button" className="apps-target__clear" aria-label={t('apps.look.clearWindow')} title={t('apps.look.clearWindow')} onClick={onClearTarget} data-testid="apps-target-clear">
              <Icon name="x" size={11} />
            </button>
          </Chip>
        ) : (
          <span className="apps-target__default">{t('apps.look.windowDefault')}</span>
        )}
      </div>

      <GatedButton why={gates.screen ?? gates.busy} hint={t('apps.look.asks')} pending={pending} onClick={onLook} icon="eye" testId="apps-look-button">
        {t('apps.look.button')}
      </GatedButton>
      {!gates.screen ? (
        <p className="hint apps-look__how" data-testid="apps-look-how">
          <Icon name="lock" size={12} />
          <BidiText text={!gates.model && vision ? vision.detail : t('apps.look.hintFacts')} />
        </p>
      ) : null}

      {job?.status === 'running' ? (
        <div className="apps-live" data-testid="apps-look-live">
          <ProgressBar value={job.progress ?? 0} indeterminate={job.progress === null} tone="work" label={job.message || t('apps.look.running')} />
          <span className="hint">
            <BidiText text={job.message || t('apps.look.running')} />
          </span>
        </div>
      ) : null}
      {job && (job.status === 'failed' || job.status === 'cancelled') ? (
        <p className="apps-why" data-testid="apps-look-failed">
          <Icon name="alert" size={12} />
          <BidiText text={t('apps.look.failed', { reason: job.error || job.message || job.status })} />
        </p>
      ) : null}
      {look ? <Seen look={look} /> : !job || job.status === 'done' ? <p className="apps-empty-line" data-testid="apps-look-none">{t('apps.look.none')}</p> : null}

      {canCorrect ? <CorrectionBox corrections={corrections} onCorrect={onCorrect} pending={correcting} /> : null}
    </Panel>
  );
}

/** What one look found: Windows' own facts, and the local model's description when it looked. */
export function Seen({ look }: { look: ScreenLook }) {
  const { t } = useI18n();
  const facts = look.facts;
  const model = look.analysed === 'model';
  return (
    <div className="apps-seen" data-testid="apps-seen" data-analysed={look.analysed}>
      <div className="apps-seen__head">
        <Chip tone={model ? 'accent' : 'steel'} testId="apps-seen-analysed">
          <Icon name={model ? 'eye' : 'list'} size={12} />
          {t(model ? 'apps.look.analysedModel' : 'apps.look.analysedFacts')}
        </Chip>
        {look.screen ? <span className="hint num">{t('apps.look.screenSize', { width: look.screen.width, height: look.screen.height })}</span> : null}
      </div>
      {look.blocked ? (
        <div className="job__note" data-testid="apps-seen-blocked">
          <Icon name="lock" size={14} />
          <BidiText text={look.blocked} />
        </div>
      ) : null}
      <dl className="kv apps-seen__facts">
        <dt>{t('apps.look.window')}</dt>
        <dd data-testid="apps-seen-window">
          <BidiText text={facts.window || facts.title || '—'} />
        </dd>
        <dt>{t('apps.look.program')}</dt>
        <dd data-testid="apps-seen-program">
          <bdi dir="ltr">{facts.program || '—'}</bdi>
        </dd>
        <dt>{t('apps.look.element')}</dt>
        <dd data-testid="apps-seen-element">
          {facts.element ? <BidiText text={facts.element} /> : <span className="muted">{t('apps.look.noElement')}</span>}
          {facts.control ? (
            <Chip tone="steel" className="apps-seen__control">
              <bdi dir="ltr">{facts.control}</bdi>
            </Chip>
          ) : null}
        </dd>
      </dl>
      {facts.password ? (
        <p className="apps-warn" data-testid="apps-seen-password">
          <Icon name="key" size={12} />
          {t('apps.look.password')}
        </p>
      ) : null}
      {facts.sensitive && !look.blocked ? (
        <p className="apps-warn">
          <Icon name="lock" size={12} />
          <BidiText text={facts.sensitive} />
        </p>
      ) : null}
      {look.description ? (
        <div className="apps-seen__desc">
          <span className="job__label">{t('apps.look.description')}</span>
          <p dir="auto" data-testid="apps-seen-description">
            {look.description}
          </p>
        </div>
      ) : null}
      {look.model_error ? (
        <p className="apps-why" data-testid="apps-seen-model-error">
          <Icon name="alert" size={12} />
          <BidiText text={t('apps.look.modelError', { error: look.model_error })} />
        </p>
      ) : null}
      {model ? <p className="hint">{t('apps.look.notKept')}</p> : null}
    </div>
  );
}

/** What JARVIS got wrong, kept for the task's next look and suggestion (in memory only). */
function CorrectionBox({ corrections, onCorrect, pending }: { corrections: string[]; onCorrect: (text: string) => Promise<boolean>; pending: boolean }) {
  const { t } = useI18n();
  const [text, setText] = useState('');
  const clean = text.trim();
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!clean || pending) return;
    void onCorrect(clean).then((ok) => {
      if (ok) setText('');
    });
  };
  return (
    <form className="apps-correct" onSubmit={submit} data-testid="apps-correct">
      <label className="field">
        <span className="field__label">{t('apps.correct.label')}</span>
        <textarea className="textarea apps-correct__text" value={text} maxLength={300} placeholder={t('apps.correct.placeholder')} onChange={(e) => setText(e.target.value)} data-testid="apps-correct-text" />
      </label>
      <div className="apps-gated">
        <button type="submit" className="btn btn--sm" disabled={!clean || pending} data-testid="apps-correct-save">
          <Icon name="check" size={12} />
          {t('apps.correct.save')}
        </button>
        <span className={clean ? 'hint' : 'apps-why'} data-testid="apps-correct-why">
          {clean ? t('apps.correct.hint') : t('apps.correct.emptyWhy')}
        </span>
      </div>
      {corrections.length ? (
        <div className="apps-correct__list">
          <span className="job__label">{t('apps.correct.list')}</span>
          <ul data-testid="apps-corrections">
            {corrections.map((line, i) => (
              <li key={i}>
                <BidiText text={line} />
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </form>
  );
}
